"""
agents/report.py — a short, plain-language report summarizing everything
uploaded into one case, for a case handler who doesn't want to click
through every document individually.

Deliberately ONE model call per case, not one per document. Every other
agent in this pipeline already ran (extraction per document, reasoning per
linked pair) by the time this is called, so all the facts this needs —
document types, key fields, discrepancies — are already sitting in Exasol.
This agent's whole job is to turn those rows into a few sentences a human
can read in five seconds; it never re-reads source files or re-extracts
anything, which keeps it to exactly one extra Gemini call regardless of
whether the case has 2 documents or 20.
"""

from agents.llm_client import call_tool
from config import Settings
from database.audit import log_event
from database.cases import list_case_documents, save_report
from database.db import Database
from database.queries import get_discrepancies_for_document, get_fields

_REPORT_TOOL = {
    "name": "record_case_report",
    "description": (
        "Record a short, plain-language report summarizing this case's "
        "uploaded documents for a case handler who hasn't read them yet."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-5 sentences in plain language: what documents were "
                    "uploaded, what the case appears to be about, and "
                    "whether anything needs attention (missing documents, "
                    "low-confidence fields, open discrepancies). No markdown."
                ),
            }
        },
        "required": ["summary"],
    },
}

_SYSTEM_PROMPT = """You write short status reports for a case handler in a document \
intelligence system. You're given a list of documents uploaded into one case, the key \
fields already extracted from each, and any discrepancies already found between them. \
Summarize what's in the case and flag anything that needs a human's attention. Be \
concrete (name the document types and the specific issue) but brief — this is a status \
line, not a full write-up. Do not invent documents, fields, or issues beyond what's given."""

# Cap how many fields per document go into the prompt — a case handler's
# report needs the gist, not every field, and keeping this bounded also
# keeps the report call cheap (small, fixed-size input) no matter how many
# fields a document produced.
_MAX_FIELDS_PER_DOC = 6


def _build_case_context(db: Database, case_id: str) -> str:
    documents = list_case_documents(db, case_id)
    if not documents:
        return "No documents have been uploaded to this case yet."

    lines = []
    for doc_id, filename, document_type, vendor, status, _uploaded_at in documents:
        lines.append(f"- {filename} (type={document_type or 'unclassified'}, status={status}, vendor={vendor or 'unknown'})")
        fields = get_fields(db, doc_id)[:_MAX_FIELDS_PER_DOC]
        for _field_id, field_name, field_value, confidence, _source in fields:
            lines.append(f"    {field_name}: {field_value} (confidence={confidence})")
        discrepancies = get_discrepancies_for_document(db, doc_id)
        for _did, _d1, _d2, field_name, value_1, value_2, severity, dstatus, explanation in discrepancies:
            lines.append(
                f"    DISCREPANCY[{severity}/{dstatus}] {field_name}: '{value_1}' vs '{value_2}' — {explanation}"
            )
    return "\n".join(lines)


def generate_case_report(db: Database, settings: Settings, case_id: str) -> str:
    """Generate, persist, and return the case's report summary.

    Safe to call more than once (e.g. a "regenerate" button, or automatically
    each time compare_case() finishes) — it always overwrites the previous
    report_summary/report_generated_at rather than appending.
    """
    context = _build_case_context(db, case_id)

    payload = call_tool(
        api_key=settings.llm_api_key,
        model=settings.reasoning_model,
        system_prompt=_SYSTEM_PROMPT,
        tool_name="record_case_report",
        tool_description=_REPORT_TOOL["description"],
        tool_schema=_REPORT_TOOL["input_schema"],
        user_content=f"Documents and findings for this case:\n\n{context}",
        max_output_tokens=512,
    )

    summary = payload.get("summary", "").strip()
    save_report(db, case_id, summary)

    log_event(
        db,
        agent_name="report",
        action="generated_case_report",
        input_summary=f"case_id={case_id}, context_chars={len(context)}",
        output_summary=summary,
    )

    return summary
