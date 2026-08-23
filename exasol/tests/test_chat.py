"""
tests/test_chat.py — agents/chat.ask() mocked at the call_tool boundary.
Complements tests/test_chat_sql_validation.py, which tests validate_sql()
in isolation; this file tests the full ask() flow including the
reject-before-hitting-the-database path and the columns/rows shape the
frontend depends on.
"""

from unittest.mock import patch

from agents.chat import ask


class FakeAuditDB:
    def __init__(self):
        self.executes = []

    def execute(self, sql, params=None):
        self.executes.append((sql, params))


class FakeReadOnlyDB:
    def __init__(self, columns=None, rows=None):
        self._columns = columns or []
        self._rows = rows or []
        self.fetchall_with_columns_calls = []

    def fetchall_with_columns(self, sql, params=None):
        self.fetchall_with_columns_calls.append(sql)
        return self._columns, self._rows


class FakeSettings:
    llm_api_key = "fake-key"
    chat_model = "gemini-3.6-flash"


@patch("agents.chat.call_tool")
def test_ask_returns_sql_columns_and_rows_for_valid_query(mock_call_tool):
    mock_call_tool.return_value = {
        "sql": "SELECT doc_id, filename FROM DOCUMENTS",
        "explanation": "Lists every document.",
    }
    ro_db = FakeReadOnlyDB(
        columns=["doc_id", "filename"],
        rows=[("doc-1", "invoice.pdf"), ("doc-2", "po.pdf")],
    )
    audit_db = FakeAuditDB()

    result = ask(audit_db, ro_db, FakeSettings(), "list all documents")

    assert result["sql"] == "SELECT doc_id, filename FROM DOCUMENTS"
    assert result["columns"] == ["doc_id", "filename"]
    assert result["rows"] == [("doc-1", "invoice.pdf"), ("doc-2", "po.pdf")]
    assert "error" not in result
    assert len(ro_db.fetchall_with_columns_calls) == 1


@patch("agents.chat.call_tool")
def test_ask_rejects_unsafe_sql_before_hitting_database(mock_call_tool):
    mock_call_tool.return_value = {
        "sql": "DELETE FROM DOCUMENTS",
        "explanation": "attempted delete",
    }
    ro_db = FakeReadOnlyDB()
    audit_db = FakeAuditDB()

    result = ask(audit_db, ro_db, FakeSettings(), "delete everything")

    assert result["sql"] is None
    assert "error" in result
    # The unsafe statement must never reach the read-only database.
    assert ro_db.fetchall_with_columns_calls == []
    # But the rejection itself must still be audited.
    audit_actions = [p["action"] for _, p in audit_db.executes if p]
    assert "query_rejected" in audit_actions


@patch("agents.chat.call_tool")
def test_ask_logs_successful_query_to_audit(mock_call_tool):
    mock_call_tool.return_value = {"sql": "SELECT 1", "explanation": "trivial"}
    ro_db = FakeReadOnlyDB(columns=["1"], rows=[(1,)])
    audit_db = FakeAuditDB()

    ask(audit_db, ro_db, FakeSettings(), "anything")

    audit_actions = [p["action"] for _, p in audit_db.executes if p]
    assert "query_executed" in audit_actions
