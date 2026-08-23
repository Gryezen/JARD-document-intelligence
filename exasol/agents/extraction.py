"""
agents/extraction.py — turns document text into structured fields with a
confidence score per field.

Uses forced tool use (tool_choice pinned to one tool) so the model cannot
return free text here — every response is a parseable field list or the
call raises. That guarantee matters because downstream code (confidence
gate, reasoning) assumes EXTRACTED_FIELDS rows always exist in this shape.
"""

import uuid

from agents.llm_client import call_tool

from config import Settings
from database.audit import log_event
from database.db import Database

_EXTRACT_TOOL = {
    "name": "record_extracted_fields",
    "description": (
        "Record the structured fields extracted from a document, each with "
        "a confidence score reflecting how certain the extraction is."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "description": (
                    "Best guess at the document's type, e.g. 'invoice', "
                    "'purchase_order', 'contract', 'land_record', "
                    "'income_certificate'. Lowercase, underscore-separated."
                ),
            },
            "vendor": {
                "type": "string",
                "description": "The primary entity/vendor/citizen name on the document, if present.",
            },
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_name": {"type": "string"},
                        "value": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "description": "0.0-1.0. Lower this for illegible, ambiguous, or inferred values.",
                        },
                    },
                    "required": ["field_name", "value", "confidence"],
                },
            },
        },
        "required": ["document_type", "fields"],
    },
}

_SYSTEM_PROMPT = """You are a document extraction agent for a government/enterprise \
document intelligence pipeline. Given raw document text (which may contain OCR \
noise, stray characters, or broken line breaks), extract every field a case \
handler would need to act on this document: identifying numbers, names, dates, \
amounts, quantities, terms, and any other structured business fact.

Assign each field an honest confidence score. Lower confidence for anything \
ambiguous, partially illegible, or inferred rather than stated verbatim. Do not \
invent values that are not supported by the text — omit the field instead."""


INSERT_FIELD_SQL = """
    INSERT INTO EXTRACTED_FIELDS
        (field_id, doc_id, field_name, field_value, confidence, source_agent, extracted_at)
    VALUES
        ({field_id}, {doc_id}, {field_name}, {value}, {confidence!f}, {source_agent}, CURRENT_TIMESTAMP)
"""

UPDATE_DOCUMENT_TYPE_SQL = """
    UPDATE DOCUMENTS
    SET document_type = {document_type}, vendor = {vendor}, updated_at = CURRENT_TIMESTAMP
    WHERE doc_id = {doc_id}
"""


class ExtractedField:
    def __init__(self, field_name: str, value: str, confidence: float):
        self.field_name = field_name
        self.value = value
        self.confidence = confidence


def extract_fields(
    db: Database,
    settings: Settings,
    doc_id: str,
    document_text: str,
) -> list[ExtractedField]:
    """Call the extraction model to pull fields, persist them, and return them.

    Raises on API failure and agents.llm_client.LLMCallError if the model
    somehow doesn't call the tool (shouldn't happen with the function call
    forced, but the check is cheap and the failure mode is
    silent-and-wrong otherwise).
    """
    payload = call_tool(
        provider=settings.extraction_provider,
        api_key=settings.llm_api_key,
        ollama_host=settings.ollama_host,
        model=settings.extraction_model,
        system_prompt=_SYSTEM_PROMPT,
        tool_name="record_extracted_fields",
        tool_description=_EXTRACT_TOOL["description"],
        tool_schema=_EXTRACT_TOOL["input_schema"],
        user_content=f"Extract structured fields from this document text:\n\n{document_text}",
        max_output_tokens=4096,
    )

    fields_raw = payload.get("fields", [])
    document_type = payload.get("document_type")
    vendor = payload.get("vendor")

    db.execute(
        UPDATE_DOCUMENT_TYPE_SQL,
        {"doc_id": doc_id, "document_type": document_type, "vendor": vendor},
    )

    extracted: list[ExtractedField] = []
    for f in fields_raw:
        field_id = str(uuid.uuid4())
        confidence = float(f["confidence"])
        db.execute(
            INSERT_FIELD_SQL,
            {
                "field_id": field_id,
                "doc_id": doc_id,
                "field_name": f["field_name"],
                "value": f["value"],
                "confidence": confidence,
                "source_agent": "extraction",
            },
        )
        extracted.append(ExtractedField(f["field_name"], f["value"], confidence))

    avg_confidence = (
        sum(f.confidence for f in extracted) / len(extracted) if extracted else None
    )
    log_event(
        db,
        agent_name="extraction",
        action="extracted_fields",
        doc_id=doc_id,
        input_summary=f"document_text_chars={len(document_text)}",
        output_summary=f"document_type={document_type}, field_count={len(extracted)}",
        confidence=avg_confidence,
    )

    return extracted
