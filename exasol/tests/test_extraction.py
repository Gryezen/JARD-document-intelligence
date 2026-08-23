"""
tests/test_extraction.py — agents/extraction.extract_fields() mocked at the
call_tool boundary (agents/llm_client.call_tool), so these tests exercise
the persistence/aggregation logic around the model call without needing a
real Gemini key or network access.
"""

from unittest.mock import patch

from agents.extraction import extract_fields


class FakeDatabase:
    def __init__(self):
        self.inserts = []
        self.updates = []

    def execute(self, sql, params=None):
        if "INSERT INTO EXTRACTED_FIELDS" in sql:
            self.inserts.append(params)
        elif "UPDATE DOCUMENTS" in sql:
            self.updates.append(params)
        elif "INSERT INTO AUDIT_LOG" in sql:
            pass
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")


class FakeSettings:
    llm_api_key = "fake-key"
    extraction_model = "gemini-3.6-flash"


@patch("agents.extraction.call_tool")
def test_extract_fields_persists_each_field_and_document_type(mock_call_tool):
    mock_call_tool.return_value = {
        "document_type": "invoice",
        "vendor": "Acme Co",
        "fields": [
            {"field_name": "invoice_amount", "value": "1000.00", "confidence": 0.95},
            {"field_name": "invoice_date", "value": "2026-01-15", "confidence": 0.6},
        ],
    }
    db = FakeDatabase()

    result = extract_fields(db, FakeSettings(), doc_id="doc-1", document_text="raw ocr text")

    assert len(result) == 2
    assert result[0].field_name == "invoice_amount"
    assert result[0].confidence == 0.95
    assert len(db.inserts) == 2
    assert db.inserts[0]["field_name"] == "invoice_amount"
    assert db.inserts[0]["value"] == "1000.00"
    assert db.updates == [{"doc_id": "doc-1", "document_type": "invoice", "vendor": "Acme Co"}]


@patch("agents.extraction.call_tool")
def test_extract_fields_handles_no_fields_gracefully(mock_call_tool):
    # A document the model can't classify usefully should not raise —
    # zero extracted fields is a valid, if uninteresting, outcome.
    mock_call_tool.return_value = {"document_type": None, "vendor": None, "fields": []}
    db = FakeDatabase()

    result = extract_fields(db, FakeSettings(), doc_id="doc-2", document_text="illegible")

    assert result == []
    assert db.inserts == []


@patch("agents.extraction.call_tool")
def test_extract_fields_passes_document_text_to_model(mock_call_tool):
    mock_call_tool.return_value = {"document_type": "invoice", "fields": []}
    db = FakeDatabase()

    extract_fields(db, FakeSettings(), doc_id="doc-3", document_text="the quick brown fox")

    _, kwargs = mock_call_tool.call_args
    assert "the quick brown fox" in kwargs["user_content"]
    assert kwargs["tool_name"] == "record_extracted_fields"
    assert kwargs["model"] == "gemini-3.6-flash"
    assert kwargs["api_key"] == "fake-key"
