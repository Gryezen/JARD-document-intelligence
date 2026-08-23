"""
tests/test_relationships_model_fallback.py — agents/relationships.link_document()
model-fallback path, mocked at the call_tool boundary. The rule-based
matching logic itself is already covered in tests/test_relationships.py;
this file covers what happens when no rule matches and the model is
consulted instead.
"""

from unittest.mock import patch

from agents.relationships import link_document


class FakeDatabase:
    """Enough of Database to drive link_document() for one doc_id against
    a fixed set of unlinked candidates, with no pre-existing relationships.
    """

    def __init__(self, doc_info, candidates):
        self._doc_info = doc_info  # (document_type, vendor)
        self._candidates = candidates  # list of (doc_id, document_type, vendor)
        self.inserted_relationships = []

    def fetchall(self, sql, params=None):
        if "SELECT document_type, vendor FROM DOCUMENTS" in sql:
            return [self._doc_info]
        if "FROM DOCUMENTS" in sql and "WHERE doc_id !=" in sql:
            return self._candidates
        if "FROM DOCUMENT_RELATIONSHIPS" in sql:
            return []  # no existing links, ever, for this test
        raise AssertionError(f"Unexpected fetchall SQL: {sql}")

    def execute(self, sql, params=None):
        if "INSERT INTO DOCUMENT_RELATIONSHIPS" in sql:
            self.inserted_relationships.append(params)
        elif "INSERT INTO AUDIT_LOG" in sql:
            pass
        else:
            raise AssertionError(f"Unexpected execute SQL: {sql}")


class FakeSettings:
    llm_api_key = "fake-key"
    reasoning_model = "gemini-3.6-flash"


@patch("agents.relationships.call_tool")
def test_model_fallback_links_related_documents_with_unknown_type_pair(mock_call_tool):
    # "quote" <-> "invoice" isn't in the rule table, so this should fall
    # through to the model, which says they're related.
    mock_call_tool.return_value = {
        "related": True,
        "relationship_type": "quote_to_invoice",
        "confidence": 0.82,
    }
    db = FakeDatabase(
        doc_info=("quote", "Acme Co"),
        candidates=[("doc-2", "invoice", "Acme Co")],
    )

    created = link_document(db, FakeSettings(), doc_id="doc-1", use_model_fallback=True)

    assert len(created) == 1
    assert created[0].relationship_type == "quote_to_invoice"
    assert created[0].confidence == 0.82
    assert len(db.inserted_relationships) == 1


@patch("agents.relationships.call_tool")
def test_model_fallback_does_not_link_when_model_says_unrelated(mock_call_tool):
    mock_call_tool.return_value = {"related": False, "relationship_type": "", "confidence": 0.9}
    db = FakeDatabase(
        doc_info=("quote", "Acme Co"),
        candidates=[("doc-2", "birth_certificate", "Acme Co")],
    )

    created = link_document(db, FakeSettings(), doc_id="doc-1", use_model_fallback=True)

    assert created == []
    assert db.inserted_relationships == []


@patch("agents.relationships.call_tool")
def test_no_vendor_overlap_skips_model_call_entirely(mock_call_tool):
    # Cost control: a candidate with no vendor overlap at all should never
    # trigger a model call, regardless of use_model_fallback.
    db = FakeDatabase(
        doc_info=("quote", "Acme Co"),
        candidates=[("doc-2", "invoice", "Totally Different Vendor")],
    )

    created = link_document(db, FakeSettings(), doc_id="doc-1", use_model_fallback=True)

    assert created == []
    mock_call_tool.assert_not_called()


@patch("agents.relationships.call_tool")
def test_use_model_fallback_false_skips_model_call(mock_call_tool):
    db = FakeDatabase(
        doc_info=("quote", "Acme Co"),
        candidates=[("doc-2", "invoice", "Acme Co")],
    )

    created = link_document(db, FakeSettings(), doc_id="doc-1", use_model_fallback=False)

    assert created == []
    mock_call_tool.assert_not_called()


@patch("agents.relationships.call_tool")
def test_rule_based_match_takes_priority_over_model_no_call_made(mock_call_tool):
    # invoice <-> purchase_order is a known rule pair; the model should
    # never be consulted for it.
    db = FakeDatabase(
        doc_info=("invoice", "Acme Co"),
        candidates=[("doc-2", "purchase_order", "Acme Co")],
    )

    created = link_document(db, FakeSettings(), doc_id="doc-1", use_model_fallback=True)

    assert len(created) == 1
    assert created[0].relationship_type == "invoice_to_po"
    assert created[0].confidence == 1.0
    mock_call_tool.assert_not_called()
