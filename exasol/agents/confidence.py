"""
agents/confidence.py — the confidence gate. Deliberately NOT a model call:
routing decisions should be reproducible and auditable as plain application
logic, per the architecture doc's reliability rules.
"""

from database.audit import log_event
from database.db import Database
from orchestration.state import set_status

GET_FIELDS_SQL = """
    SELECT field_id, field_name, field_value, confidence
    FROM EXTRACTED_FIELDS
    WHERE doc_id = {doc_id}
"""


class GateResult:
    def __init__(self, decision: str, low_confidence_fields: list[dict]):
        self.decision = decision  # "AUTO_APPROVE" or "HUMAN_REVIEW"
        self.low_confidence_fields = low_confidence_fields


def run_confidence_gate(db: Database, doc_id: str, threshold: float) -> GateResult:
    rows = db.fetchall(GET_FIELDS_SQL, {"doc_id": doc_id})
    low_conf = [
        {"field_id": r[0], "field_name": r[1], "value": r[2], "confidence": r[3]}
        for r in rows
        if r[3] is not None and float(r[3]) < threshold
    ]

    decision = "HUMAN_REVIEW" if low_conf else "AUTO_APPROVE"
    next_status = "review" if low_conf else "reasoning"

    set_status(db, doc_id, next_status, current_status="extracting")

    log_event(
        db,
        agent_name="confidence_gate",
        action="routed_document",
        doc_id=doc_id,
        input_summary=f"threshold={threshold}, field_count={len(rows)}",
        output_summary=f"decision={decision}, low_confidence_count={len(low_conf)}",
    )

    return GateResult(decision=decision, low_confidence_fields=low_conf)
