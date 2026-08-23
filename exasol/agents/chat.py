"""
agents/chat.py — natural-language querying over DOC_INTEL.

Defense in depth, in order:
  1. Connects with ReadOnlyDatabase — an Exasol identity that only has
     SELECT on DOC_INTEL (see docs/mcp-grants.sql). A write statement fails
     at the database no matter what the model generates.
  2. validate_sql() rejects anything that isn't a single SELECT statement
     before it's ever sent to the database, so failures are fast and
     legible instead of relying on the grant alone.
  3. The model is told the exact schema and warned not to guess columns.
"""

import re

from agents.llm_client import call_tool

from config import Settings
from database.audit import log_event
from database.db import Database, ReadOnlyDatabase

_SCHEMA_DESCRIPTION = """\
Tables in DOC_INTEL (read-only):

DOCUMENTS(doc_id, filename, document_type, vendor, status, source_path, page_count, uploaded_by, uploaded_at, updated_at)
EXTRACTED_FIELDS(field_id, doc_id, field_name, field_value, confidence, source_agent, extracted_at)  -- note: column is "field_value", not "value" ("value" is a reserved word in Exasol SQL)
DOCUMENT_RELATIONSHIPS(relationship_id, doc_id_1, doc_id_2, relationship_type, confidence, created_at)
DISCREPANCIES(discrepancy_id, doc_id_1, doc_id_2, field_name, value_1, value_2, severity, status, explanation, detected_at)
ACTIONS(action_id, discrepancy_id, doc_id, action_type, content, status, created_at, decided_at, decided_by)
HUMAN_REVIEWS(review_id, doc_id, field_id, field_name, ai_value, human_value, status, reviewed_by, reviewed_at)
AUDIT_LOG(log_id, doc_id, agent_name, action_name, input_summary, output_summary, confidence, logged_at)  -- note: columns are "action_name" and "logged_at", not "action"/"timestamp" (both reserved words in Exasol SQL)
"""

_SQL_TOOL = {
    "name": "run_query",
    "description": "Run a single read-only SQL SELECT statement against DOC_INTEL and explain the result.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single SELECT statement. No DML, no DDL, no semicolon-chaining."},
            "explanation": {"type": "string", "description": "Plain-language explanation of what this query answers."},
        },
        "required": ["sql", "explanation"],
    },
}

_SYSTEM_PROMPT = f"""You translate natural-language questions into a single \
read-only SQL SELECT statement over the DOC_INTEL schema.

{_SCHEMA_DESCRIPTION}

Rules:
- Only use the tables and columns listed above. Never guess a column name.
- Write exactly one SELECT statement. No INSERT/UPDATE/DELETE/DROP/ALTER, no \
multiple statements, no semicolon-separated chaining.
- Prefer explicit column lists over SELECT *.
- If the question can't be answered with these tables, explain why in the \
explanation field and return a trivial query like 'SELECT 1 WHERE FALSE'."""

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CREATE|MERGE|CALL|EXEC)\b",
    re.IGNORECASE,
)


class SQLValidationError(Exception):
    pass


def validate_sql(sql: str) -> str:
    """Defense-in-depth check before a generated query ever reaches the
    database. Raises SQLValidationError if the statement looks unsafe.
    """
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise SQLValidationError("Multiple statements are not allowed.")
    if not re.match(r"^\s*SELECT\b", stripped, re.IGNORECASE):
        raise SQLValidationError("Only SELECT statements are allowed.")
    if _FORBIDDEN_KEYWORDS.search(stripped):
        raise SQLValidationError("Statement contains a forbidden keyword.")
    return stripped


def ask(
    audit_db: Database,
    ro_db: ReadOnlyDatabase,
    settings: Settings,
    question: str,
) -> dict:
    """Answer a natural-language question about the document corpus.

    Returns {"sql": ..., "explanation": ..., "rows": ..., "columns": ...}
    or {"sql": None, "explanation": ..., "error": ...} on validation failure.
    audit_db is a normal (read-write) Database used ONLY to write the
    AUDIT_LOG row for this query — the query itself always runs on ro_db.
    """
    payload = call_tool(
        provider=settings.chat_provider,
        api_key=settings.llm_api_key,
        ollama_host=settings.ollama_host,
        model=settings.chat_model,
        system_prompt=_SYSTEM_PROMPT,
        tool_name="run_query",
        tool_description=_SQL_TOOL["description"],
        tool_schema=_SQL_TOOL["input_schema"],
        user_content=question,
        max_output_tokens=1024,
    )

    generated_sql = payload["sql"]
    explanation = payload["explanation"]

    try:
        safe_sql = validate_sql(generated_sql)
    except SQLValidationError as e:
        log_event(
            audit_db,
            agent_name="chat",
            action="query_rejected",
            input_summary=question,
            output_summary=f"rejected_sql={generated_sql!r}, reason={e}",
        )
        return {"sql": None, "explanation": explanation, "error": str(e)}

    columns, rows = ro_db.fetchall_with_columns(safe_sql)

    log_event(
        audit_db,
        agent_name="chat",
        action="query_executed",
        input_summary=question,
        output_summary=f"sql={safe_sql}, row_count={len(rows)}",
    )

    return {"sql": safe_sql, "explanation": explanation, "columns": columns, "rows": rows}
