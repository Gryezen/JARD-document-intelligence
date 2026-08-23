"""
agents/action.py — drafts a next-step proposal for a discrepancy. Per the
architecture's reliability rules, this NEVER sends real email or performs
a real action; it only writes an ACTIONS row with status='proposed' for a
human to approve or reject.
"""

import uuid

from agents.llm_client import call_tool

from config import Settings
from database.audit import log_event
from database.db import Database

_ACTION_TOOL = {
    "name": "draft_action",
    "description": (
        "Draft a next-step proposal for a discrepancy: a short clarification "
        "email and/or a task description for a case handler. This is a draft "
        "only — it will be shown to a human for approval, never sent automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "email_subject": {"type": "string"},
            "email_body": {"type": "string"},
            "task_description": {
                "type": "string",
                "description": "A short actionable task for the case handler, independent of the email.",
            },
        },
        "required": ["email_subject", "email_body", "task_description"],
    },
}

_SYSTEM_PROMPT = """You are an action-drafting agent. Given a discrepancy found \
between two related documents, draft:
1. A short, professional clarification email to the relevant party (vendor or \
citizen) asking them to resolve or explain the discrepancy.
2. A one-line task description for the internal case handler.

Be specific about the discrepancy (name the field and both values) and keep the \
tone neutral and non-accusatory — the goal is clarification, not blame. Do not \
claim any action has already been taken."""

_GROUPED_ACTION_TOOL = {
    "name": "draft_action",
    "description": (
        "Draft ONE next-step proposal that covers every discrepancy found "
        "between a single pair of related documents: one short clarification "
        "email addressing all of them together, and one task description for "
        "a case handler. This is a draft only — it will be shown to a human "
        "for approval, never sent automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "email_subject": {"type": "string"},
            "email_body": {"type": "string"},
            "task_description": {
                "type": "string",
                "description": "A single short actionable task covering all listed discrepancies.",
            },
        },
        "required": ["email_subject", "email_body", "task_description"],
    },
}

_GROUPED_SYSTEM_PROMPT = """You are an action-drafting agent. You are given every \
discrepancy found between ONE pair of related documents. Draft exactly ONE \
clarification email and ONE task covering all of them together — never a separate \
email or task per discrepancy, even if there are several.

1. One short, professional clarification email to the relevant party (vendor or \
citizen) listing each discrepancy (field + both values) and asking them to resolve \
or explain the set of them together. A short list inside the email is fine; do not \
write a separate paragraph per item.
2. One one-line task description for the internal case handler summarizing all open \
items for this document pair.

Keep the tone neutral and non-accusatory — the goal is clarification, not blame. Do \
not claim any action has already been taken."""

INSERT_ACTION_SQL = """
    INSERT INTO ACTIONS
        (action_id, discrepancy_id, doc_id, action_type, content, status, created_at)
    VALUES
        ({action_id}, {discrepancy_id}, {doc_id}, {action_type}, {content}, 'proposed', CURRENT_TIMESTAMP)
"""

GET_DISCREPANCY_SQL = """
    SELECT field_name, value_1, value_2, severity, explanation
    FROM DISCREPANCIES
    WHERE discrepancy_id = {discrepancy_id}
"""


def draft_action_for_discrepancy(
    db: Database,
    settings: Settings,
    discrepancy_id: str,
    doc_id: str,
) -> tuple[str, str]:
    """Draft an email + task for one discrepancy. Returns (email_action_id, task_action_id)."""
    rows = db.fetchall(GET_DISCREPANCY_SQL, {"discrepancy_id": discrepancy_id})
    if not rows:
        raise ValueError(f"No discrepancy found with id {discrepancy_id}")
    field_name, value_1, value_2, severity, explanation = rows[0]

    draft = call_tool(
        provider=settings.reasoning_provider,
        api_key=settings.llm_api_key,
        ollama_host=settings.ollama_host,
        model=settings.reasoning_model,
        system_prompt=_SYSTEM_PROMPT,
        tool_name="draft_action",
        tool_description=_ACTION_TOOL["description"],
        tool_schema=_ACTION_TOOL["input_schema"],
        user_content=(
            f"Discrepancy: field '{field_name}' disagrees "
            f"(value 1: {value_1!r}, value 2: {value_2!r}). "
            f"Severity: {severity}. Explanation: {explanation}"
        ),
        max_output_tokens=1024,
    )

    email_content = f"Subject: {draft['email_subject']}\n\n{draft['email_body']}"

    email_action_id = str(uuid.uuid4())
    task_action_id = str(uuid.uuid4())

    db.execute(
        INSERT_ACTION_SQL,
        {
            "action_id": email_action_id,
            "discrepancy_id": discrepancy_id,
            "doc_id": doc_id,
            "action_type": "email_draft",
            "content": email_content,
        },
    )
    db.execute(
        INSERT_ACTION_SQL,
        {
            "action_id": task_action_id,
            "discrepancy_id": discrepancy_id,
            "doc_id": doc_id,
            "action_type": "task_proposal",
            "content": draft["task_description"],
        },
    )

    log_event(
        db,
        agent_name="action",
        action="drafted_action",
        doc_id=doc_id,
        input_summary=f"discrepancy_id={discrepancy_id}, field={field_name}",
        output_summary=f"email_action_id={email_action_id}, task_action_id={task_action_id}",
    )

    return email_action_id, task_action_id


def draft_action_for_pair(
    db: Database,
    settings: Settings,
    discrepancies: list,
    doc_id: str,
) -> tuple[str, str]:
    """Draft ONE combined email + ONE combined task covering every
    discrepancy found for a single document pair, instead of one email/task
    per discrepancy (what draft_action_for_discrepancy does). This is what
    orchestration/workflow.py now calls after a reasoning pass, so a pair
    with several mismatched fields produces one grouped clarification
    request rather than a flood of near-duplicate drafts.

    discrepancies: list of reasoning.Discrepancy, all from the same
    document pair (as returned by reasoning_agent.compare_documents).
    Returns (email_action_id, task_action_id).
    """
    if not discrepancies:
        raise ValueError("draft_action_for_pair requires at least one discrepancy")

    findings = "\n".join(
        f"- {d.field_name}: {d.value_1!r} vs {d.value_2!r} (severity: {d.severity}) — {d.explanation}"
        for d in discrepancies
    )

    draft = call_tool(
        provider=settings.reasoning_provider,
        api_key=settings.llm_api_key,
        ollama_host=settings.ollama_host,
        model=settings.reasoning_model,
        system_prompt=_GROUPED_SYSTEM_PROMPT,
        tool_name="draft_action",
        tool_description=_GROUPED_ACTION_TOOL["description"],
        tool_schema=_GROUPED_ACTION_TOOL["input_schema"],
        user_content=(
            f"{len(discrepancies)} discrepancies found between this document pair:\n"
            f"{findings}\n\n"
            "Draft one combined clarification email and one combined task covering all of them."
        ),
        max_output_tokens=1024,
    )

    email_content = f"Subject: {draft['email_subject']}\n\n{draft['email_body']}"
    # Every action row still needs a single discrepancy_id per the schema
    # (nullable FK, but not a list) — the first discrepancy in the group is
    # kept as the representative link for traceability; the email/task
    # content itself covers the full set.
    representative_id = discrepancies[0].discrepancy_id

    email_action_id = str(uuid.uuid4())
    task_action_id = str(uuid.uuid4())

    db.execute(
        INSERT_ACTION_SQL,
        {
            "action_id": email_action_id,
            "discrepancy_id": representative_id,
            "doc_id": doc_id,
            "action_type": "email_draft",
            "content": email_content,
        },
    )
    db.execute(
        INSERT_ACTION_SQL,
        {
            "action_id": task_action_id,
            "discrepancy_id": representative_id,
            "doc_id": doc_id,
            "action_type": "task_proposal",
            "content": draft["task_description"],
        },
    )

    log_event(
        db,
        agent_name="action",
        action="drafted_grouped_action",
        doc_id=doc_id,
        input_summary=f"discrepancy_ids={[d.discrepancy_id for d in discrepancies]}",
        output_summary=f"email_action_id={email_action_id}, task_action_id={task_action_id}",
    )

    return email_action_id, task_action_id


def decide_action(db: Database, action_id: str, decision: str, decided_by: str) -> None:
    """Human approves or rejects a drafted action. decision: 'approved' | 'rejected'."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    db.execute(
        """
        UPDATE ACTIONS
        SET status = {status}, decided_at = CURRENT_TIMESTAMP, decided_by = {decided_by}
        WHERE action_id = {action_id}
        """,
        {"status": decision, "decided_by": decided_by, "action_id": action_id},
    )
    log_event(
        db,
        agent_name="human",
        action=f"action_{decision}",
        input_summary=f"action_id={action_id}",
        output_summary=f"decided_by={decided_by}",
    )
