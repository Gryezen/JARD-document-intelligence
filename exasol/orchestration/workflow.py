"""
orchestration/workflow.py — drives the pipeline described in the
architecture doc, calling agents between orchestration/state.py transitions.

    ingest -> extract -> confidence gate -> (human review, out of band)
           -> reasoning (per related-document pair) -> action (per discrepancy)

This module intentionally does NOT handle the human-review or
action-approval steps itself — those happen out of band via
agents/human_review.submit_review() and agents/action.decide_action(),
triggered by the frontend/API when a person acts. What it does own is the
straight-line automated path and the fan-out to reasoning/action once a
document is unblocked.
"""

from agents import action as action_agent
from agents import confidence as confidence_agent
from agents import extraction as extraction_agent
from agents import ingestion as ingestion_agent
from agents import reasoning as reasoning_agent
from agents import relationships as relationships_agent
from agents import report as report_agent
from config import Settings
from database.audit import log_event
from database.db import Database
from orchestration.state import set_status

GET_RELATED_DOCS_SQL = """
    SELECT doc_id_2 FROM DOCUMENT_RELATIONSHIPS WHERE doc_id_1 = {doc_id}
    UNION
    SELECT doc_id_1 FROM DOCUMENT_RELATIONSHIPS WHERE doc_id_2 = {doc_id}
"""

# Same idea as GET_RELATED_DOCS_SQL but only returns pairs reasoning
# hasn't run for yet (compared_at IS NULL). compare_related_documents()
# uses this instead of GET_RELATED_DOCS_SQL so that re-processing a
# document — or processing both sides of a mutual relationship
# independently — doesn't re-run (and re-call the model for) a pair
# that's already been compared. Mirrors the dedup compare_case() already
# had; compare_related_documents() was missing it, which is what produced
# duplicate DISCREPANCIES/ACTIONS rows for documents with several
# cross-references.
GET_UNCOMPARED_RELATED_DOCS_SQL = """
    SELECT doc_id_2 FROM DOCUMENT_RELATIONSHIPS WHERE doc_id_1 = {doc_id} AND compared_at IS NULL
    UNION
    SELECT doc_id_1 FROM DOCUMENT_RELATIONSHIPS WHERE doc_id_2 = {doc_id} AND compared_at IS NULL
"""

# Case-scoped variant: same pair-finding as above, but also returns
# compared_at so compare_case() can skip a pair reasoning has already run
# for instead of re-comparing (and re-calling the model for) it every time
# a new document is added to the case.
GET_RELATIONSHIPS_FOR_DOC_SQL = """
    SELECT doc_id_1, doc_id_2, compared_at FROM DOCUMENT_RELATIONSHIPS
    WHERE doc_id_1 = {doc_id} OR doc_id_2 = {doc_id}
"""

MARK_PAIR_COMPARED_SQL = """
    UPDATE DOCUMENT_RELATIONSHIPS SET compared_at = CURRENT_TIMESTAMP
    WHERE (doc_id_1 = {doc_a} AND doc_id_2 = {doc_b}) OR (doc_id_1 = {doc_b} AND doc_id_2 = {doc_a})
"""

GET_CASE_DOCS_SQL = "SELECT doc_id, status FROM DOCUMENTS WHERE case_id = {case_id}"

GET_DOCUMENT_FOR_RETRY_SQL = """
    SELECT case_id, source_path, status FROM DOCUMENTS WHERE doc_id = {doc_id}
"""

GET_FAILED_CASE_DOCS_SQL = """
    SELECT doc_id FROM DOCUMENTS WHERE case_id = {case_id} AND status = 'failed'
"""


def process_new_document(
    db: Database,
    settings: Settings,
    file_path: str,
    filename: str,
    uploaded_by: str | None = None,
    case_id: str | None = None,
) -> dict:
    """Run ingestion -> extraction -> confidence gate for one file.

    Stops at the confidence gate: if the gate returns HUMAN_REVIEW, the
    document sits in 'review' status until a person acts (see
    agents/human_review.py). If it returns AUTO_APPROVE, the document is
    already in 'reasoning' status and ready for compare_case().

    case_id ties the document to a CASES row (see database/cases.py) —
    cross-document reasoning only ever runs between documents that share
    a case, never across the whole registry.
    """
    ingestion_result = ingestion_agent.ingest_document(
        db, file_path=file_path, filename=filename, uploaded_by=uploaded_by, case_id=case_id
    )

    set_status(db, ingestion_result.doc_id, "extracting", current_status="uploaded")

    # Everything past this point can throw (most commonly extraction's LLM
    # call hitting a Gemini quota/rate-limit error — see agents/llm_client's
    # LLMRateLimitError). Previously an exception here just bubbled up as a
    # bare 500 and left the DOCUMENTS row stuck in 'extracting' forever,
    # with nothing in the UI explaining why. Now it's marked 'failed' (a
    # legal transition from 'extracting', see orchestration/state.py) before
    # re-raising, so the document shows up correctly and 'failed' -> a
    # future manual retry stays possible.
    try:
        fields = extraction_agent.extract_fields(
            db, settings, doc_id=ingestion_result.doc_id, document_text=ingestion_result.text
        )

        # Link to other documents in the same case before the gate, so that by
        # the time a document reaches 'reasoning' its relationships are already
        # known and compare_case() has something to do.
        relationships = relationships_agent.link_document(
            db, settings, doc_id=ingestion_result.doc_id, case_id=case_id
        )

        gate_result = confidence_agent.run_confidence_gate(
            db, doc_id=ingestion_result.doc_id, threshold=settings.confidence_threshold
        )
    except Exception:
        set_status(db, ingestion_result.doc_id, "failed", current_status="extracting")
        raise

    return {
        "doc_id": ingestion_result.doc_id,
        "case_id": case_id,
        "field_count": len(fields),
        "linked_documents": [
            {"other_doc_id": r.other_doc_id, "relationship_type": r.relationship_type, "confidence": r.confidence}
            for r in relationships
        ],
        "gate_decision": gate_result.decision,
        "low_confidence_fields": gate_result.low_confidence_fields,
    }


def retry_document(db: Database, settings: Settings, doc_id: str) -> dict:
    """Re-run extraction -> relationship linking -> confidence gate for one
    document that's currently in 'failed' status, reusing the file already
    saved at DOCUMENTS.source_path — no re-upload needed. This is the
    "Retry" button's backend: most failures are a transient LLM-provider
    issue (Gemini quota, Ollama momentarily unreachable), not a problem
    with the file itself, so simply running the same steps again is
    usually enough.

    Any partial state left over from the failed attempt is cleared first —
    extraction may have inserted EXTRACTED_FIELDS rows before a later step
    (relationship linking or the confidence gate) is what actually threw,
    and re-running extract_fields() on top of that would duplicate them
    rather than replace them. DOCUMENT_RELATIONSHIPS rows created before a
    later failure are cleared the same way, since link_document() would
    otherwise create a second link to the same document.

    Raises ValueError if the document doesn't exist or isn't currently
    'failed' (retrying a document that's still processing or already
    complete isn't a meaningful operation, so this is treated as a caller
    error rather than silently no-op'ing).
    """
    rows = db.fetchall(GET_DOCUMENT_FOR_RETRY_SQL, {"doc_id": doc_id})
    if not rows:
        raise ValueError(f"no such document: {doc_id}")
    case_id, source_path, status = rows[0]
    if status != "failed":
        raise ValueError(f"document {doc_id} is not in 'failed' status (currently '{status}')")

    db.execute("DELETE FROM EXTRACTED_FIELDS WHERE doc_id = {doc_id}", {"doc_id": doc_id})
    db.execute(
        "DELETE FROM DOCUMENT_RELATIONSHIPS WHERE doc_id_1 = {doc_id} OR doc_id_2 = {doc_id}",
        {"doc_id": doc_id},
    )

    set_status(db, doc_id, "extracting", current_status="failed")

    try:
        text, _page_count, _ocr_confidence = ingestion_agent.extract_text_from_file(source_path)

        fields = extraction_agent.extract_fields(db, settings, doc_id=doc_id, document_text=text)

        relationships = relationships_agent.link_document(
            db, settings, doc_id=doc_id, case_id=case_id
        )

        gate_result = confidence_agent.run_confidence_gate(
            db, doc_id=doc_id, threshold=settings.confidence_threshold
        )
    except Exception:
        set_status(db, doc_id, "failed", current_status="extracting")
        raise

    log_event(
        db,
        agent_name="ingestion",
        action="retried_document",
        doc_id=doc_id,
        output_summary=f"retry succeeded, gate_decision={gate_result.decision}, field_count={len(fields)}",
    )

    return {
        "doc_id": doc_id,
        "case_id": case_id,
        "field_count": len(fields),
        "linked_documents": [
            {"other_doc_id": r.other_doc_id, "relationship_type": r.relationship_type, "confidence": r.confidence}
            for r in relationships
        ],
        "gate_decision": gate_result.decision,
        "low_confidence_fields": gate_result.low_confidence_fields,
    }


def retry_failed_documents_for_case(db: Database, settings: Settings, case_id: str) -> dict:
    """Retry every document currently in 'failed' status within one case.

    Each document is retried independently — one still failing (e.g. its
    file is genuinely corrupt, vs. another that just hit a transient
    rate-limit) doesn't stop the rest from being attempted. This is the
    "Retry all failed" button's backend.
    """
    failed_doc_ids = [r[0] for r in db.fetchall(GET_FAILED_CASE_DOCS_SQL, {"case_id": case_id})]

    succeeded = []
    still_failed = []
    for doc_id in failed_doc_ids:
        try:
            retry_document(db, settings, doc_id)
            succeeded.append(doc_id)
        except Exception as e:
            still_failed.append({"doc_id": doc_id, "error": str(e)})

    return {
        "case_id": case_id,
        "attempted": len(failed_doc_ids),
        "succeeded": succeeded,
        "still_failed": still_failed,
    }


def compare_related_documents(db: Database, settings: Settings, doc_id: str) -> dict:
    """For a document now in 'reasoning' status, compare it against every
    *not-yet-compared* document linked via DOCUMENT_RELATIONSHIPS, draft one
    grouped action per pair for any discrepancies found, and mark the
    document complete.

    Only uncompared pairs are considered (GET_UNCOMPARED_RELATED_DOCS_SQL),
    and each pair is marked compared_at immediately after — otherwise
    processing both sides of a mutual relationship (or re-processing a
    document) re-runs reasoning on a pair that already has a result,
    producing duplicate discrepancies and duplicate drafted actions.

    A document with no (uncompared) relationships simply has nothing left
    to compare against and moves straight to 'complete' — that's a valid
    outcome, not an error, since not every document type has a counterpart
    to check against (e.g. a standalone land record).
    """
    related_doc_ids = [r[0] for r in db.fetchall(GET_UNCOMPARED_RELATED_DOCS_SQL, {"doc_id": doc_id})]

    all_discrepancies = []
    drafted_actions = []
    for related_id in related_doc_ids:
        discrepancies = reasoning_agent.compare_documents(db, settings, doc_id, related_id)
        db.execute(MARK_PAIR_COMPARED_SQL, {"doc_a": doc_id, "doc_b": related_id})
        all_discrepancies.extend(discrepancies)

        if discrepancies:
            # One grouped email + one grouped task per document *pair*,
            # not one per discrepancy — a pair with several mismatched
            # fields should read as a single clarification request, not a
            # flood of near-identical drafts.
            email_id, task_id = action_agent.draft_action_for_pair(
                db, settings, discrepancies=discrepancies, doc_id=doc_id
            )
            drafted_actions.append(
                {
                    "discrepancy_ids": [d.discrepancy_id for d in discrepancies],
                    "email_action_id": email_id,
                    "task_action_id": task_id,
                }
            )

    set_status(db, doc_id, "complete", current_status="reasoning")

    return {
        "doc_id": doc_id,
        "related_documents_compared": len(related_doc_ids),
        "discrepancies_found": len(all_discrepancies),
        "actions_drafted": drafted_actions,
    }


def compare_case(db: Database, settings: Settings, case_id: str) -> dict:
    """Run cross-document reasoning across every linked pair of documents
    inside one case, and mark any document that was waiting on it as
    'complete'.

    This is the case-scoped counterpart to compare_related_documents():
    instead of comparing one document against its relationships in
    isolation (which misses a pair if the *other* document had already
    finished before the relationship existed — see the "Known limitation"
    in README.md), it re-evaluates the whole case each time it's called,
    so it's safe to call again after a document is added to an
    already-processed case. Already-compared pairs (DOCUMENT_RELATIONSHIPS
    .compared_at is set) are skipped rather than re-run, so this doesn't
    re-call the model or duplicate DISCREPANCIES rows on a repeat call.
    """
    case_docs = db.fetchall(GET_CASE_DOCS_SQL, {"case_id": case_id})
    doc_ids = {row[0] for row in case_docs}
    status_by_doc = {row[0]: row[1] for row in case_docs}

    # Collect each undirected pair of case documents that are linked and
    # not yet compared. A dict keyed by frozenset naturally dedupes since
    # relationships are undirected and agents/relationships.py never
    # creates a second row for the same pair.
    pending_pairs: dict[frozenset, tuple[str, str]] = {}
    for doc_id in doc_ids:
        for doc_a, doc_b, compared_at in db.fetchall(GET_RELATIONSHIPS_FOR_DOC_SQL, {"doc_id": doc_id}):
            if compared_at is not None:
                continue
            if doc_a not in doc_ids or doc_b not in doc_ids:
                continue  # relationship reaches outside this case; not our concern here
            pending_pairs[frozenset({doc_a, doc_b})] = (doc_a, doc_b)

    all_discrepancies = []
    drafted_actions = []
    for doc_a, doc_b in pending_pairs.values():
        discrepancies = reasoning_agent.compare_documents(db, settings, doc_a, doc_b)
        all_discrepancies.extend(discrepancies)
        db.execute(MARK_PAIR_COMPARED_SQL, {"doc_a": doc_a, "doc_b": doc_b})

        if discrepancies:
            # One grouped email + one grouped task per pair, not one per
            # discrepancy — see compare_related_documents() for the same
            # rationale.
            email_id, task_id = action_agent.draft_action_for_pair(
                db, settings, discrepancies=discrepancies, doc_id=doc_a
            )
            drafted_actions.append(
                {
                    "discrepancy_ids": [d.discrepancy_id for d in discrepancies],
                    "email_action_id": email_id,
                    "task_action_id": task_id,
                }
            )

    completed = []
    for doc_id, status in status_by_doc.items():
        if status == "reasoning":
            set_status(db, doc_id, "complete", current_status="reasoning")
            completed.append(doc_id)

    # Best-effort: refresh the case's LLM report now that reasoning/action
    # have run. This is one extra model call, not one per document, but it
    # still shouldn't take down an otherwise-successful comparison call if
    # the model is rate-limited or briefly unavailable — the case's actual
    # findings above already committed, so a report failure is logged and
    # swallowed rather than turning a 200 into a 500 for the caller. The
    # frontend just shows the previous report_summary (or none) until the
    # next successful call regenerates it.
    report_summary = None
    try:
        report_summary = report_agent.generate_case_report(db, settings, case_id)
    except Exception as e:
        log_event(
            db,
            agent_name="report",
            action="report_generation_failed",
            input_summary=f"case_id={case_id}",
            output_summary=str(e)[:2000],
        )

    return {
        "case_id": case_id,
        "pairs_compared": len(pending_pairs),
        "discrepancies_found": len(all_discrepancies),
        "actions_drafted": drafted_actions,
        "documents_completed": completed,
        "report_summary": report_summary,
    }
