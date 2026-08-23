"""
database/queries.py — read helpers shared by api/routes.py and any future
dashboard code. Kept separate from database/db.py (connection plumbing)
and agents/*.py (write paths owned by a specific agent).
"""

from database.db import Database

GET_DOCUMENT_SQL = """
    SELECT doc_id, filename, document_type, vendor, status, page_count, uploaded_at, case_id
    FROM DOCUMENTS WHERE doc_id = {doc_id}
"""

LIST_DOCUMENTS_SQL = """
    SELECT doc_id, filename, document_type, vendor, status, uploaded_at, case_id
    FROM DOCUMENTS ORDER BY uploaded_at DESC
"""

GET_FIELDS_SQL = """
    SELECT field_id, field_name, field_value, confidence, source_agent
    FROM EXTRACTED_FIELDS WHERE doc_id = {doc_id}
"""

GET_DISCREPANCIES_FOR_DOC_SQL = """
    SELECT discrepancy_id, doc_id_1, doc_id_2, field_name, value_1, value_2, severity, status, explanation
    FROM DISCREPANCIES WHERE doc_id_1 = {doc_id} OR doc_id_2 = {doc_id}
"""

GET_ACTIONS_FOR_DISCREPANCY_SQL = """
    SELECT action_id, action_type, content, status, created_at, decided_at, decided_by
    FROM ACTIONS WHERE discrepancy_id = {discrepancy_id}
"""

GET_AUDIT_TIMELINE_SQL = """
    SELECT log_id, agent_name, action_name, input_summary, output_summary, confidence, logged_at
    FROM AUDIT_LOG WHERE doc_id = {doc_id} ORDER BY logged_at ASC
"""

GET_OPEN_DISCREPANCIES_SQL = """
    SELECT discrepancy_id, doc_id_1, doc_id_2, field_name, severity, status
    FROM DISCREPANCIES WHERE status = 'open' ORDER BY detected_at DESC
"""

# Actions are keyed by discrepancy_id, but ACTIONS also carries doc_id
# directly (see schema.sql), so this can join straight off ACTIONS.doc_id
# rather than going through DISCREPANCIES first.
GET_ACTIONS_FOR_DOCUMENT_SQL = """
    SELECT action_id, discrepancy_id, action_type, content, status, created_at, decided_at, decided_by
    FROM ACTIONS WHERE doc_id = {doc_id} ORDER BY created_at ASC
"""

# A citizen's case is usually several linked documents (e.g. income
# certificate <-> welfare application), so this pulls the *other* side of
# every relationship this document participates in, joined against
# DOCUMENTS for display — regardless of which side of doc_id_1/doc_id_2
# this document happens to sit on.
GET_RELATED_DOCUMENTS_SQL = """
    SELECT d.doc_id, d.filename, d.document_type, d.vendor, d.status,
           r.relationship_type, r.confidence
    FROM DOCUMENT_RELATIONSHIPS r
    JOIN DOCUMENTS d
      ON d.doc_id = CASE WHEN r.doc_id_1 = {doc_id} THEN r.doc_id_2 ELSE r.doc_id_1 END
    WHERE r.doc_id_1 = {doc_id} OR r.doc_id_2 = {doc_id}
    ORDER BY r.created_at ASC
"""


def get_document(db: Database, doc_id: str) -> tuple | None:
    rows = db.fetchall(GET_DOCUMENT_SQL, {"doc_id": doc_id})
    return rows[0] if rows else None


def list_documents(db: Database) -> list[tuple]:
    return db.fetchall(LIST_DOCUMENTS_SQL)


def get_fields(db: Database, doc_id: str) -> list[tuple]:
    return db.fetchall(GET_FIELDS_SQL, {"doc_id": doc_id})


def get_discrepancies_for_document(db: Database, doc_id: str) -> list[tuple]:
    return db.fetchall(GET_DISCREPANCIES_FOR_DOC_SQL, {"doc_id": doc_id})


def get_actions_for_discrepancy(db: Database, discrepancy_id: str) -> list[tuple]:
    return db.fetchall(GET_ACTIONS_FOR_DISCREPANCY_SQL, {"discrepancy_id": discrepancy_id})


def get_audit_timeline(db: Database, doc_id: str) -> list[tuple]:
    return db.fetchall(GET_AUDIT_TIMELINE_SQL, {"doc_id": doc_id})


def get_open_discrepancies(db: Database) -> list[tuple]:
    return db.fetchall(GET_OPEN_DISCREPANCIES_SQL)


def get_actions_for_document(db: Database, doc_id: str) -> list[tuple]:
    return db.fetchall(GET_ACTIONS_FOR_DOCUMENT_SQL, {"doc_id": doc_id})


def get_related_documents(db: Database, doc_id: str) -> list[tuple]:
    return db.fetchall(GET_RELATED_DOCUMENTS_SQL, {"doc_id": doc_id})
