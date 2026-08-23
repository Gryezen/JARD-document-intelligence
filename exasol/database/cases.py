"""
database/cases.py — CRUD for CASES, the container a user explicitly groups
uploaded documents into. A "case" is the unit cross-document reasoning is
scoped to (see agents/relationships.py's case_id path and
orchestration/workflow.compare_case): documents only get compared against
other documents in the *same* case, never against the whole registry.

Kept separate from database/queries.py (which is read-only helpers) because
this module also owns writes — create/rename/delete — same split the rest
of the codebase uses (agents own writes, database/queries.py owns reads).
"""

import os
import uuid
from datetime import datetime, timezone

from database.db import Database

CREATE_CASE_SQL = """
    INSERT INTO CASES (case_id, name, created_by, created_at, updated_at)
    VALUES ({case_id}, {name}, {created_by}, {timestamp}, {timestamp})
"""

GET_CASE_SQL = """
    SELECT case_id, name, created_by, created_at, updated_at, report_summary, report_generated_at
    FROM CASES WHERE case_id = {case_id}
"""

LIST_CASES_SQL = """
    SELECT case_id, name, created_by, created_at, updated_at, report_summary, report_generated_at
    FROM CASES ORDER BY updated_at DESC
"""

SAVE_REPORT_SQL = """
    UPDATE CASES SET report_summary = {report_summary}, report_generated_at = {timestamp}
    WHERE case_id = {case_id}
"""

LIST_CASE_DOC_SUMMARY_SQL = """
    SELECT case_id, doc_id, status
    FROM DOCUMENTS WHERE case_id IS NOT NULL
"""

RENAME_CASE_SQL = """
    UPDATE CASES SET name = {name}, updated_at = {timestamp} WHERE case_id = {case_id}
"""

TOUCH_CASE_SQL = """
    UPDATE CASES SET updated_at = {timestamp} WHERE case_id = {case_id}
"""

DELETE_CASE_SQL = "DELETE FROM CASES WHERE case_id = {case_id}"

LIST_CASE_DOCUMENT_IDS_SQL = "SELECT doc_id, source_path FROM DOCUMENTS WHERE case_id = {case_id}"

GET_CASE_DOCUMENTS_SQL = """
    SELECT doc_id, filename, document_type, vendor, status, uploaded_at
    FROM DOCUMENTS WHERE case_id = {case_id} ORDER BY uploaded_at ASC
"""

GET_DOCUMENT_CASE_SQL = "SELECT case_id, source_path FROM DOCUMENTS WHERE doc_id = {doc_id}"

_DELETE_STATEMENTS_FOR_DOC = [
    "DELETE FROM AUDIT_LOG WHERE doc_id = {doc_id}",
    "DELETE FROM HUMAN_REVIEWS WHERE doc_id = {doc_id}",
    "DELETE FROM ACTIONS WHERE doc_id = {doc_id}",
    "DELETE FROM DISCREPANCIES WHERE doc_id_1 = {doc_id} OR doc_id_2 = {doc_id}",
    "DELETE FROM DOCUMENT_RELATIONSHIPS WHERE doc_id_1 = {doc_id} OR doc_id_2 = {doc_id}",
    "DELETE FROM EXTRACTED_FIELDS WHERE doc_id = {doc_id}",
    "DELETE FROM DOCUMENTS WHERE doc_id = {doc_id}",
]


def _now() -> str:
    # Same explicit-format convention database/audit.py uses — Exasol's
    # default TIMESTAMP parser rejects a raw tz-aware datetime's "+00:00".
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def create_case(db: Database, name: str, created_by: str | None = None) -> str:
    case_id = str(uuid.uuid4())
    db.execute(
        CREATE_CASE_SQL,
        {"case_id": case_id, "name": name, "created_by": created_by, "timestamp": _now()},
    )
    return case_id


def get_case(db: Database, case_id: str) -> tuple | None:
    rows = db.fetchall(GET_CASE_SQL, {"case_id": case_id})
    return rows[0] if rows else None


def list_cases(db: Database) -> list[tuple]:
    return db.fetchall(LIST_CASES_SQL)


def list_case_doc_summary(db: Database) -> list[tuple]:
    """(case_id, doc_id, status) for every document that belongs to a case —
    used to build the per-case document count / rollup status shown on the
    Cases list page without an N+1 query per case.
    """
    return db.fetchall(LIST_CASE_DOC_SUMMARY_SQL)


def list_case_documents(db: Database, case_id: str) -> list[tuple]:
    return db.fetchall(GET_CASE_DOCUMENTS_SQL, {"case_id": case_id})


def rename_case(db: Database, case_id: str, name: str) -> None:
    db.execute(RENAME_CASE_SQL, {"case_id": case_id, "name": name, "timestamp": _now()})


def touch_case(db: Database, case_id: str) -> None:
    db.execute(TOUCH_CASE_SQL, {"case_id": case_id, "timestamp": _now()})


def save_report(db: Database, case_id: str, report_summary: str) -> None:
    db.execute(
        SAVE_REPORT_SQL,
        {"case_id": case_id, "report_summary": report_summary, "timestamp": _now()},
    )


def _delete_document_rows(db: Database, doc_id: str, source_path: str | None) -> None:
    for stmt in _DELETE_STATEMENTS_FOR_DOC:
        db.execute(stmt, {"doc_id": doc_id})
    if source_path:
        try:
            os.remove(source_path)
        except OSError:
            pass  # best-effort; a missing/already-removed file isn't fatal


def remove_document(db: Database, case_id: str, doc_id: str) -> bool:
    """Remove one document from a case: deletes the document and every row
    that references it (fields, relationships, discrepancies, actions,
    reviews, audit log), plus its stored file. Returns False if the
    document doesn't belong to this case.
    """
    rows = db.fetchall(GET_DOCUMENT_CASE_SQL, {"doc_id": doc_id})
    if not rows or rows[0][0] != case_id:
        return False
    _, source_path = rows[0]
    _delete_document_rows(db, doc_id, source_path)
    touch_case(db, case_id)
    return True


def delete_case(db: Database, case_id: str) -> None:
    """Delete a case and every document (and its dependent rows) inside it."""
    doc_rows = db.fetchall(LIST_CASE_DOCUMENT_IDS_SQL, {"case_id": case_id})
    for doc_id, source_path in doc_rows:
        _delete_document_rows(db, doc_id, source_path)
    db.execute(DELETE_CASE_SQL, {"case_id": case_id})
