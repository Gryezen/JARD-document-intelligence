"""
tests/test_report.py — agents/report.generate_case_report() mocked at the
call_tool boundary, same pattern as tests/test_extraction.py.
"""

from unittest.mock import patch

from agents.report import generate_case_report


class FakeDatabase:
    def __init__(self, documents, fields_by_doc=None, discrepancies_by_doc=None):
        self._documents = documents
        self._fields_by_doc = fields_by_doc or {}
        self._discrepancies_by_doc = discrepancies_by_doc or {}
        self.updates = []
        self.audit_events = []

    def fetchall(self, sql, params=None):
        if "FROM DOCUMENTS WHERE case_id" in sql:
            return self._documents
        if "FROM EXTRACTED_FIELDS" in sql:
            return self._fields_by_doc.get(params["doc_id"], [])
        if "FROM DISCREPANCIES" in sql:
            return self._discrepancies_by_doc.get(params["doc_id"], [])
        raise AssertionError(f"Unexpected fetchall SQL: {sql}")

    def execute(self, sql, params=None):
        if "UPDATE CASES" in sql:
            self.updates.append(params)
        elif "INSERT INTO AUDIT_LOG" in sql:
            self.audit_events.append(params)
        else:
            raise AssertionError(f"Unexpected execute SQL: {sql}")


class FakeSettings:
    llm_api_key = "fake-key"
    reasoning_provider = "gemini"
    reasoning_model = "gemini-3.6-flash"
    ollama_host = "http://localhost:11434"


@patch("agents.report.call_tool")
def test_generate_case_report_persists_summary(mock_call_tool):
    mock_call_tool.return_value = {"summary": "Two documents uploaded; one discrepancy needs review."}
    documents = [
        ("doc-1", "income_certificate.pdf", "income_certificate", "Ravi Kumar", "complete", "2026-01-01"),
        ("doc-2", "welfare_application.pdf", "welfare_application", "Ravi Kumar", "complete", "2026-01-02"),
    ]
    fields_by_doc = {
        "doc-1": [("f1", "annual_income", "45000", 0.9, "extraction")],
        "doc-2": [("f2", "annual_income", "50000", 0.85, "extraction")],
    }
    discrepancies_by_doc = {
        "doc-1": [("d1", "doc-1", "doc-2", "annual_income", "45000", "50000", "medium", "open", "Income mismatch")],
        "doc-2": [],
    }
    db = FakeDatabase(documents, fields_by_doc, discrepancies_by_doc)

    result = generate_case_report(db, FakeSettings(), case_id="case-1")

    assert result == "Two documents uploaded; one discrepancy needs review."
    assert db.updates == [{"case_id": "case-1", "report_summary": result, "timestamp": db.updates[0]["timestamp"]}]
    assert len(db.audit_events) == 1

    # One call regardless of document count — the context is built from
    # already-persisted rows, not a fresh model call per document.
    assert mock_call_tool.call_count == 1
    prompt = mock_call_tool.call_args.kwargs["user_content"]
    assert "annual_income" in prompt
    assert "DISCREPANCY" in prompt


@patch("agents.report.call_tool")
def test_generate_case_report_handles_empty_case(mock_call_tool):
    mock_call_tool.return_value = {"summary": "No documents uploaded yet."}
    db = FakeDatabase(documents=[])

    result = generate_case_report(db, FakeSettings(), case_id="case-empty")

    assert result == "No documents uploaded yet."
    prompt = mock_call_tool.call_args.kwargs["user_content"]
    assert "No documents have been uploaded" in prompt
