"""
tests/test_reasoning.py — agents/reasoning.compare_documents() mocked at
the call_tool boundary.
"""

from unittest.mock import patch

from agents.reasoning import compare_documents


class FakeDatabase:
    def __init__(self, fields_by_doc):
        self._fields_by_doc = fields_by_doc
        self.inserts = []

    def fetchall(self, sql, params=None):
        return self._fields_by_doc[params["doc_id"]]

    def execute(self, sql, params=None):
        if "INSERT INTO DISCREPANCIES" in sql:
            self.inserts.append(params)
        elif "INSERT INTO AUDIT_LOG" in sql:
            pass
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")


class FakeSettings:
    llm_api_key = "fake-key"
    reasoning_model = "gemini-3.6-flash"


@patch("agents.reasoning.call_tool")
def test_compare_documents_persists_each_discrepancy(mock_call_tool):
    mock_call_tool.return_value = {
        "discrepancies": [
            {
                "field_name": "amount",
                "value_1": "1000.00",
                "value_2": "1200.00",
                "severity": "high",
                "explanation": "Invoice amount exceeds the PO amount.",
            }
        ]
    }
    db = FakeDatabase(
        {
            "invoice-1": [("amount", "1000.00", 0.9)],
            "po-1": [("amount", "1200.00", 0.9)],
        }
    )

    results = compare_documents(db, FakeSettings(), "invoice-1", "po-1")

    assert len(results) == 1
    assert results[0].severity == "high"
    assert results[0].field_name == "amount"
    assert len(db.inserts) == 1
    assert db.inserts[0]["value_1"] == "1000.00"
    assert db.inserts[0]["value_2"] == "1200.00"


@patch("agents.reasoning.call_tool")
def test_compare_documents_returns_empty_when_no_discrepancies_found(mock_call_tool):
    mock_call_tool.return_value = {"discrepancies": []}
    db = FakeDatabase({"invoice-1": [], "po-1": []})

    results = compare_documents(db, FakeSettings(), "invoice-1", "po-1")

    assert results == []
    assert db.inserts == []


@patch("agents.reasoning.call_tool")
def test_compare_documents_sends_both_documents_fields_to_model(mock_call_tool):
    mock_call_tool.return_value = {"discrepancies": []}
    db = FakeDatabase(
        {
            "invoice-1": [("amount", "1000.00", 0.9)],
            "po-1": [("amount", "1200.00", 0.9)],
        }
    )

    compare_documents(db, FakeSettings(), "invoice-1", "po-1")

    _, kwargs = mock_call_tool.call_args
    assert "1000.00" in kwargs["user_content"]
    assert "1200.00" in kwargs["user_content"]
    assert kwargs["tool_name"] == "record_discrepancies"
