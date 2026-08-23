"""
agents/relationships.py — decides which documents belong to the same case
and should be compared by the reasoning agent (invoice<->PO<->contract,
income-certificate<->welfare-application, etc.).

Two-tier approach, cheapest first:
  1. Deterministic rule pass: same vendor/entity name + a known compatible
     document_type pair (see _COMPATIBLE_TYPE_PAIRS). This covers the
     demo scenario (one vendor, three related docs) with no model call at
     all, so it's fast, free, and fully reproducible.
  2. If no rule matches but both documents have a document_type the rules
     don't know about, fall back to a small model call that only answers
     yes/no + confidence on whether two specific documents are related —
     it never invents a relationship type outside what's asked.

This mirrors the confidence gate's philosophy: don't reach for a model
when deterministic logic is enough to explain the decision.
"""

import uuid

from agents.llm_client import call_tool, LLMCallError

from config import Settings
from database.audit import log_event
from database.db import Database

# (type_a, type_b) pairs considered related regardless of order.
# Extend this as new document types are added to the corpus.
_COMPATIBLE_TYPE_PAIRS: set[frozenset[str]] = {
    frozenset({"invoice", "purchase_order"}),
    frozenset({"purchase_order", "contract"}),
    frozenset({"invoice", "contract"}),
    frozenset({"income_certificate", "welfare_application"}),
    frozenset({"land_record", "property_tax_receipt"}),
    frozenset({"birth_certificate", "welfare_application"}),
}

_RELATIONSHIP_TYPE_NAMES = {
    frozenset({"invoice", "purchase_order"}): "invoice_to_po",
    frozenset({"purchase_order", "contract"}): "po_to_contract",
    frozenset({"invoice", "contract"}): "invoice_to_contract",
    frozenset({"income_certificate", "welfare_application"}): "income_cert_to_application",
    frozenset({"land_record", "property_tax_receipt"}): "land_record_to_tax_receipt",
    frozenset({"birth_certificate", "welfare_application"}): "birth_cert_to_application",
}

_CANDIDATE_TOOL = {
    "name": "assess_relationship",
    "description": (
        "State whether two documents plausibly belong to the same case and "
        "should be cross-checked against each other, based only on their "
        "document types and vendor/entity names."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "related": {"type": "boolean"},
            "relationship_type": {
                "type": "string",
                "description": "Short snake_case label, e.g. 'quote_to_invoice'. Empty string if not related.",
            },
            "confidence": {"type": "number"},
        },
        "required": ["related", "relationship_type", "confidence"],
    },
}

_SYSTEM_PROMPT = """You decide whether two documents belong to the same real-world \
case and should be cross-checked against each other (e.g. a quote and the invoice \
that followed it, or a permit application and its supporting ID document). You are \
only given each document's type and vendor/entity name — not its full content. Say \
related=true only when the document types would plausibly need their facts \
cross-checked against each other. A shared vendor name alone is not enough if the \
document types are unrelated (e.g. two unrelated invoices from the same vendor)."""

GET_UNLINKED_CANDIDATES_SQL = """
    SELECT doc_id, document_type, vendor
    FROM DOCUMENTS
    WHERE doc_id != {doc_id}
      AND document_type IS NOT NULL
      AND status != 'failed'
"""

# Case-scoped candidate pass: documents a user explicitly grouped into the
# same case are already a human-confirmed "these belong together" signal —
# stronger than the vendor-name heuristic above — so every other document
# in the case is a candidate, not just ones sharing a vendor string.
GET_CASE_CANDIDATES_SQL = """
    SELECT doc_id, document_type, vendor
    FROM DOCUMENTS
    WHERE doc_id != {doc_id}
      AND case_id = {case_id}
      AND status != 'failed'
"""

CHECK_EXISTING_RELATIONSHIP_SQL = """
    SELECT relationship_id FROM DOCUMENT_RELATIONSHIPS
    WHERE (doc_id_1 = {doc_a} AND doc_id_2 = {doc_b})
       OR (doc_id_1 = {doc_b} AND doc_id_2 = {doc_a})
"""

INSERT_RELATIONSHIP_SQL = """
    INSERT INTO DOCUMENT_RELATIONSHIPS
        (relationship_id, doc_id_1, doc_id_2, relationship_type, confidence, created_at)
    VALUES
        ({relationship_id}, {doc_id_1}, {doc_id_2}, {relationship_type}, {confidence!f}, CURRENT_TIMESTAMP)
"""

GET_DOC_INFO_SQL = "SELECT document_type, vendor FROM DOCUMENTS WHERE doc_id = {doc_id}"


class RelationshipCandidate:
    def __init__(self, other_doc_id: str, relationship_type: str, confidence: float):
        self.other_doc_id = other_doc_id
        self.relationship_type = relationship_type
        self.confidence = confidence


def _rule_based_match(type_a: str, type_b: str) -> str | None:
    pair = frozenset({type_a, type_b})
    if pair in _COMPATIBLE_TYPE_PAIRS:
        return _RELATIONSHIP_TYPE_NAMES[pair]
    return None


def _link_exists(db: Database, doc_a: str, doc_b: str) -> bool:
    rows = db.fetchall(CHECK_EXISTING_RELATIONSHIP_SQL, {"doc_a": doc_a, "doc_b": doc_b})
    return len(rows) > 0


def _create_relationship(
    db: Database, doc_id_1: str, doc_id_2: str, relationship_type: str, confidence: float
) -> str:
    relationship_id = str(uuid.uuid4())
    db.execute(
        INSERT_RELATIONSHIP_SQL,
        {
            "relationship_id": relationship_id,
            "doc_id_1": doc_id_1,
            "doc_id_2": doc_id_2,
            "relationship_type": relationship_type,
            "confidence": confidence,
        },
    )
    return relationship_id


def _link_within_case(db: Database, doc_id: str, doc_type: str, case_id: str) -> list[RelationshipCandidate]:
    candidates = db.fetchall(GET_CASE_CANDIDATES_SQL, {"doc_id": doc_id, "case_id": case_id})
    created: list[RelationshipCandidate] = []
    for other_id, other_type, _other_vendor in candidates:
        if _link_exists(db, doc_id, other_id):
            continue
        relationship_type = _rule_based_match(doc_type, other_type) or "case_document"
        relationship_id = _create_relationship(db, doc_id, other_id, relationship_type, confidence=1.0)
        created.append(RelationshipCandidate(other_id, relationship_type, 1.0))
        log_event(
            db,
            agent_name="relationships",
            action="linked_documents_same_case",
            doc_id=doc_id,
            input_summary=f"other_doc={other_id}, case_id={case_id}, types=({doc_type},{other_type})",
            output_summary=f"relationship_id={relationship_id}, type={relationship_type}",
            confidence=1.0,
        )
    return created


def link_document(
    db: Database,
    settings: Settings,
    doc_id: str,
    use_model_fallback: bool = True,
    case_id: str | None = None,
) -> list[RelationshipCandidate]:
    """Find and persist relationships between doc_id and existing documents.

    Called once a document has a document_type (i.e. after extraction).

    If case_id is given, matching is scoped to that case: every other
    document already in the case is linked (using the rule-based label
    when the type pair is known, otherwise a generic 'case_document'
    label) — no vendor check and no model call needed, since being placed
    in the same case by a person is already a stronger signal than either.
    This is the path orchestration/workflow.py uses for the normal
    upload-into-a-case flow.

    If case_id is None, the original whole-registry behavior applies:
    rule-based matches on (document_type, vendor) run first and are free;
    the model fallback only fires for a candidate whose type pair isn't in
    _COMPATIBLE_TYPE_PAIRS at all, and even then only checks vendor-matched
    candidates to keep the call count bounded. Kept for callers that don't
    have a case to scope to.
    """
    doc_rows = db.fetchall(GET_DOC_INFO_SQL, {"doc_id": doc_id})
    if not doc_rows or doc_rows[0][0] is None:
        return []  # not yet classified — nothing to link on
    doc_type, doc_vendor = doc_rows[0]

    if case_id:
        return _link_within_case(db, doc_id=doc_id, doc_type=doc_type, case_id=case_id)

    candidates = db.fetchall(GET_UNLINKED_CANDIDATES_SQL, {"doc_id": doc_id})
    created: list[RelationshipCandidate] = []

    for other_id, other_type, other_vendor in candidates:
        if _link_exists(db, doc_id, other_id):
            continue

        same_vendor = bool(doc_vendor) and bool(other_vendor) and doc_vendor.strip().lower() == other_vendor.strip().lower()

        rule_match = _rule_based_match(doc_type, other_type)
        if rule_match and same_vendor:
            relationship_id = _create_relationship(db, doc_id, other_id, rule_match, confidence=1.0)
            created.append(RelationshipCandidate(other_id, rule_match, 1.0))
            log_event(
                db,
                agent_name="relationships",
                action="linked_documents_rule_based",
                doc_id=doc_id,
                input_summary=f"other_doc={other_id}, types=({doc_type},{other_type}), vendor_match=True",
                output_summary=f"relationship_id={relationship_id}, type={rule_match}",
                confidence=1.0,
            )
            continue

        if not use_model_fallback or not same_vendor:
            continue  # don't spend a model call on candidates with no vendor overlap at all

        try:
            result = call_tool(
                api_key=settings.llm_api_key,
                model=settings.reasoning_model,
                system_prompt=_SYSTEM_PROMPT,
                tool_name="assess_relationship",
                tool_description=_CANDIDATE_TOOL["description"],
                tool_schema=_CANDIDATE_TOOL["input_schema"],
                user_content=(
                    f"Document A: type={doc_type}, vendor={doc_vendor}\n"
                    f"Document B: type={other_type}, vendor={other_vendor}"
                ),
                max_output_tokens=256,
            )
        except LLMCallError:
            continue

        log_event(
            db,
            agent_name="relationships",
            action="assessed_relationship_via_model",
            doc_id=doc_id,
            input_summary=f"other_doc={other_id}, types=({doc_type},{other_type})",
            output_summary=f"related={result['related']}, type={result.get('relationship_type')}",
            confidence=result.get("confidence"),
        )

        if result["related"] and result.get("relationship_type"):
            relationship_id = _create_relationship(
                db, doc_id, other_id, result["relationship_type"], float(result["confidence"])
            )
            created.append(
                RelationshipCandidate(other_id, result["relationship_type"], float(result["confidence"]))
            )

    return created
