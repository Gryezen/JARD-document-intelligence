"""
tests/test_action.py — agents/action.draft_action_for_discrepancy() mocked
at the call_tool boundary.
"""

from unittest.mock import patch

from agents.action import draft_action_for_discrepancy, decide_action


class FakeDatabase:
    def __init__(self, discrepancy_row):
        self._discrepancy_row = discrepancy_row
        self.inserts = []
        self.executes = []

    def fetchall(self, sql, params=None):
        return [self._discrepancy_row]

    def execute(self, sql, params=None):
        self.executes.append((sql, params))
        if "INSERT INTO ACTIONS" in sql:
            self.inserts.append(params)


class FakeSettings:
    llm_api_key = "fake-key"
    reasoning_provider = "gemini"
    reasoning_model = "gemini-3.6-flash"
    ollama_host = "http://localhost:11434"


@patch("agents.action.call_tool")
def test_draft_action_creates_email_and_task_rows(mock_call_tool):
    mock_call_tool.return_value = {
        "email_subject": "Discrepancy in invoice amount",
        "email_body": "We noticed a mismatch, please clarify.",
        "task_description": "Follow up with vendor about amount mismatch.",
    }
    db = FakeDatabase(("amount", "1000.00", "1200.00", "high", "amounts disagree"))

    email_id, task_id = draft_action_for_discrepancy(
        db, FakeSettings(), discrepancy_id="disc-1", doc_id="doc-1"
    )

    assert email_id != task_id
    assert len(db.inserts) == 2
    action_types = {row["action_type"] for row in db.inserts}
    assert action_types == {"email_draft", "task_proposal"}

    email_row = next(r for r in db.inserts if r["action_type"] == "email_draft")
    assert "Discrepancy in invoice amount" in email_row["content"]
    assert "mismatch, please clarify" in email_row["content"]

    task_row = next(r for r in db.inserts if r["action_type"] == "task_proposal")
    assert task_row["content"] == "Follow up with vendor about amount mismatch."


@patch("agents.action.call_tool")
def test_draft_action_raises_for_unknown_discrepancy(mock_call_tool):
    class EmptyDB(FakeDatabase):
        def fetchall(self, sql, params=None):
            return []

    db = EmptyDB(None)
    try:
        draft_action_for_discrepancy(db, FakeSettings(), discrepancy_id="missing", doc_id="doc-1")
        assert False, "expected ValueError"
    except ValueError:
        pass
    mock_call_tool.assert_not_called()


def test_decide_action_records_approval():
    db = FakeDatabase(None)
    decide_action(db, action_id="action-1", decision="approved", decided_by="alice")
    update_calls = [p for sql, p in db.executes if "UPDATE ACTIONS" in sql]
    assert len(update_calls) == 1
    assert update_calls[0]["status"] == "approved"
    assert update_calls[0]["decided_by"] == "alice"


def test_decide_action_rejects_invalid_decision():
    db = FakeDatabase(None)
    try:
        decide_action(db, action_id="action-1", decision="maybe", decided_by="alice")
        assert False, "expected ValueError"
    except ValueError:
        pass
