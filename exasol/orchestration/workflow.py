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
from config import Settings
from database.db import Database
from orchestration.state import set_status

GET_RELATED_DOCS_SQL = """
    SELECT doc_id_2 FROM DOCUMENT_RELATIONSHIPS WHERE doc_id_1 = {doc_id}
    UNION
    SELECT doc_id_1 FROM DOCUMENT_RELATIONSHIPS WHERE doc_id_2 = {doc_id}
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


def compare_related_documents(db: Database, settings: Settings, doc_id: str) -> dict:
    """For a document now in 'reasoning' status, compare it against every
    document linked via DOCUMENT_RELATIONSHIPS, draft actions for any
    discrepancy found, and mark the document complete.

    A document with no relationships defined simply has nothing to compare
    against and moves straight to 'complete' — that's a valid outcome, not
    an error, since not every document type has a counterpart to check
    against (e.g. a standalone land record).
    """
    related_doc_ids = [r[0] for r in db.fetchall(GET_RELATED_DOCS_SQL, {"doc_id": doc_id})]

    all_discrepancies = []
    for related_id in related_doc_ids:
        discrepancies = reasoning_agent.compare_documents(db, settings, doc_id, related_id)
        all_discrepancies.extend(discrepancies)

    drafted_actions = []
    for d in all_discrepancies:
        email_id, task_id = action_agent.draft_action_for_discrepancy(
            db, settings, discrepancy_id=d.discrepancy_id, doc_id=doc_id
        )
        drafted_actions.append({"discrepancy_id": d.discrepancy_id, "email_action_id": email_id, "task_action_id": task_id})

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
    for doc_a, doc_b in pending_pairs.values():
        discrepancies = reasoning_agent.compare_documents(db, settings, doc_a, doc_b)
        all_discrepancies.extend(discrepancies)
        db.execute(MARK_PAIR_COMPARED_SQL, {"doc_a": doc_a, "doc_b": doc_b})

    drafted_actions = []
    for d in all_discrepancies:
        email_id, task_id = action_agent.draft_action_for_discrepancy(
            db, settings, discrepancy_id=d.discrepancy_id, doc_id=d.doc_id_1
        )
        drafted_actions.append(
            {"discrepancy_id": d.discrepancy_id, "email_action_id": email_id, "task_action_id": task_id}
        )

    completed = []
    for doc_id, status in status_by_doc.items():
        if status == "reasoning":
            set_status(db, doc_id, "complete", current_status="reasoning")
            completed.append(doc_id)

    return {
        "case_id": case_id,
        "pairs_compared": len(pending_pairs),
        "discrepancies_found": len(all_discrepancies),
        "actions_drafted": drafted_actions,
        "documents_completed": completed,
    }
