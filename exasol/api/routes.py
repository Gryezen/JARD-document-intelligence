"""
api/routes.py — thin Flask surface over the orchestrator, agents, and
queries. This is intentionally not the full app (no auth, no file-upload
streaming, no pagination) — it's enough for the frontend/demo owner to
build the dashboard against real endpoints instead of mocks.

Run standalone with `python -m api.routes`, or `flask --app api.routes run`.
"""

import os
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from agents import chat as chat_agent
from agents import human_review as human_review_agent
from agents import action as action_agent
from config import load_settings
from database.db import Database, ReadOnlyDatabase
from database import queries
from database import cases as case_queries
from orchestration import workflow

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".txt"}

app = Flask(__name__, static_folder=str(_FRONTEND_DIR), static_url_path="")
settings = load_settings()
db = Database(settings)
ro_db = ReadOnlyDatabase(settings)

@app.route("/", methods=["GET"])
def index():
    """Serve the single-page dashboard. Kept as an explicit route (rather
    than relying solely on Flask's static handler for '/') so a fresh
    clone with no frontend/ directory still gives a clear 404 instead of
    Flask's default error page.
    """
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    """Expose the handful of settings the frontend needs to render
    correctly (e.g. which fields count as low-confidence) without
    hardcoding them client-side and risking drift from the real gate.
    """
    return jsonify(
        {
            "confidence_threshold": settings.confidence_threshold,
            "allowed_extensions": sorted(_ALLOWED_EXTENSIONS),
        }
    )


def _row_to_dict(row: tuple, columns: list[str]) -> dict:
    return dict(zip(columns, row))


@app.route("/api/documents", methods=["GET"])
def list_documents():
    rows = queries.list_documents(db)
    cols = ["doc_id", "filename", "document_type", "vendor", "status", "uploaded_at", "case_id"]
    return jsonify([_row_to_dict(r, cols) for r in rows])


@app.route("/api/documents/<doc_id>", methods=["GET"])
def get_document(doc_id: str):
    row = queries.get_document(db, doc_id)
    if row is None:
        return jsonify({"error": "not found"}), 404
    cols = ["doc_id", "filename", "document_type", "vendor", "status", "page_count", "uploaded_at", "case_id"]
    return jsonify(_row_to_dict(row, cols))


@app.route("/api/documents/<doc_id>/fields", methods=["GET"])
def get_fields(doc_id: str):
    rows = queries.get_fields(db, doc_id)
    cols = ["field_id", "field_name", "value", "confidence", "source_agent"]
    return jsonify([_row_to_dict(r, cols) for r in rows])


@app.route("/api/documents/<doc_id>/discrepancies", methods=["GET"])
def get_discrepancies(doc_id: str):
    rows = queries.get_discrepancies_for_document(db, doc_id)
    cols = ["discrepancy_id", "doc_id_1", "doc_id_2", "field_name", "value_1", "value_2", "severity", "status", "explanation"]
    return jsonify([_row_to_dict(r, cols) for r in rows])


@app.route("/api/documents/<doc_id>/audit", methods=["GET"])
def get_audit_timeline(doc_id: str):
    rows = queries.get_audit_timeline(db, doc_id)
    cols = ["log_id", "agent_name", "action", "input_summary", "output_summary", "confidence", "timestamp"]
    return jsonify([_row_to_dict(r, cols) for r in rows])


@app.route("/api/documents/<doc_id>/actions", methods=["GET"])
def get_actions(doc_id: str):
    rows = queries.get_actions_for_document(db, doc_id)
    cols = ["action_id", "discrepancy_id", "action_type", "content", "status", "created_at", "decided_at", "decided_by"]
    return jsonify([_row_to_dict(r, cols) for r in rows])


@app.route("/api/documents/<doc_id>/related", methods=["GET"])
def get_related_documents(doc_id: str):
    """The other documents in this citizen's / vendor's case — e.g. an
    income certificate linked to the welfare application it supports.
    Powers the case-file view so an officer isn't hunting through the
    whole registry to find documents that belong together.
    """
    rows = queries.get_related_documents(db, doc_id)
    cols = ["doc_id", "filename", "document_type", "vendor", "status", "relationship_type", "confidence"]
    return jsonify([_row_to_dict(r, cols) for r in rows])


def _case_summary_status(doc_statuses: list[str]) -> str:
    """Roll a case's documents' individual statuses up into one label the
    Cases list page can badge without the client re-deriving it per case.
    """
    if not doc_statuses:
        return "empty"
    if any(s == "review" for s in doc_statuses):
        return "needs_review"
    if any(s == "failed" for s in doc_statuses):
        return "failed"
    if any(s in ("uploaded", "extracting", "reasoning") for s in doc_statuses):
        return "processing"
    return "complete"


@app.route("/api/cases", methods=["GET"])
def list_cases():
    case_rows = case_queries.list_cases(db)
    doc_summary = case_queries.list_case_doc_summary(db)
    statuses_by_case: dict[str, list[str]] = {}
    for case_id, _doc_id, status in doc_summary:
        statuses_by_case.setdefault(case_id, []).append(status)

    cols = ["case_id", "name", "created_by", "created_at", "updated_at"]
    result = []
    for row in case_rows:
        case = _row_to_dict(row, cols)
        doc_statuses = statuses_by_case.get(case["case_id"], [])
        case["document_count"] = len(doc_statuses)
        case["status"] = _case_summary_status(doc_statuses)
        result.append(case)
    return jsonify(result)


@app.route("/api/cases", methods=["POST"])
def create_case():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip() or "Untitled case"
    created_by = body.get("created_by")
    case_id = case_queries.create_case(db, name=name, created_by=created_by)
    return jsonify({"case_id": case_id, "name": name}), 201


@app.route("/api/cases/<case_id>", methods=["GET"])
def get_case(case_id: str):
    case_row = case_queries.get_case(db, case_id)
    if case_row is None:
        return jsonify({"error": "not found"}), 404
    cols = ["case_id", "name", "created_by", "created_at", "updated_at"]
    case = _row_to_dict(case_row, cols)
    doc_cols = ["doc_id", "filename", "document_type", "vendor", "status", "uploaded_at"]
    documents = [_row_to_dict(r, doc_cols) for r in case_queries.list_case_documents(db, case_id)]
    case["documents"] = documents
    return jsonify(case)


@app.route("/api/cases/<case_id>/rename", methods=["POST"])
def rename_case(case_id: str):
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "missing 'name'"}), 400
    if case_queries.get_case(db, case_id) is None:
        return jsonify({"error": "not found"}), 404
    try:
        case_queries.rename_case(db, case_id, name)
    except Exception as e:
        return jsonify({"error": f"rename failed: {e}"}), 500
    return jsonify({"case_id": case_id, "name": name})


@app.route("/api/cases/<case_id>", methods=["DELETE"])
def delete_case(case_id: str):
    if case_queries.get_case(db, case_id) is None:
        return jsonify({"error": "not found"}), 404
    try:
        case_queries.delete_case(db, case_id)
    except Exception as e:
        return jsonify({"error": f"delete failed: {e}"}), 500
    return jsonify({"case_id": case_id, "deleted": True})


@app.route("/api/cases/<case_id>/documents/<doc_id>", methods=["DELETE"])
def remove_case_document(case_id: str, doc_id: str):
    try:
        removed = case_queries.remove_document(db, case_id, doc_id)
    except Exception as e:
        return jsonify({"error": f"remove failed: {e}"}), 500
    if not removed:
        return jsonify({"error": "not found"}), 404
    return jsonify({"doc_id": doc_id, "removed": True})


@app.route("/api/cases/<case_id>/process", methods=["POST"])
def process_case(case_id: str):
    """Run cross-document reasoning across every linked, not-yet-compared
    pair of documents in this case. Safe to call again after a document is
    added to an already-processed case — already-compared pairs are skipped.
    """
    if case_queries.get_case(db, case_id) is None:
        return jsonify({"error": "not found"}), 404
    try:
        result = workflow.compare_case(db, settings, case_id)
    except Exception as e:
        return jsonify({"error": f"comparison failed: {e}"}), 500
    return jsonify(result)


@app.route("/api/discrepancies/open", methods=["GET"])
def get_open_discrepancies():
    rows = queries.get_open_discrepancies(db)
    cols = ["discrepancy_id", "doc_id_1", "doc_id_2", "field_name", "severity", "status"]
    return jsonify([_row_to_dict(r, cols) for r in rows])


@app.route("/api/documents/upload", methods=["POST"])
def upload_document():
    """Accept a file, run ingestion -> extraction -> relationship linking ->
    confidence gate synchronously, and return the result.

    Every document belongs to a case (see database/cases.py): pass an
    existing `case_id` form field to add this file to that case, or omit
    it to have a new case created automatically (named after the file) —
    that's what happens the first time a batch of files is dropped on the
    Documents page. Cross-document reasoning only ever compares documents
    that share a case.

    Synchronous on purpose for the hackathon MVP: judges watching the demo
    should see the pipeline actually run, not poll a job queue. If document
    processing time becomes a problem during the demo, move this to a
    background task and add a /api/documents/<id>/status poll endpoint
    instead of faking progress client-side.
    """
    if "file" not in request.files:
        return jsonify({"error": "no file part named 'file' in request"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "empty filename"}), 400

    filename = secure_filename(file.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        return jsonify({"error": f"unsupported file type: {suffix}"}), 400

    os.makedirs(settings.upload_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4()}{suffix}"
    stored_path = os.path.join(settings.upload_dir, stored_name)
    file.save(stored_path)

    uploaded_by = request.form.get("uploaded_by")
    case_id = request.form.get("case_id") or None

    if case_id and case_queries.get_case(db, case_id) is None:
        return jsonify({"error": f"no such case: {case_id}"}), 404

    created_new_case = False
    if not case_id:
        case_id = case_queries.create_case(db, name=filename, created_by=uploaded_by)
        created_new_case = True

    try:
        result = workflow.process_new_document(
            db, settings, file_path=stored_path, filename=filename, uploaded_by=uploaded_by, case_id=case_id
        )
        case_queries.touch_case(db, case_id)
    except Exception as e:
        return jsonify({"error": f"processing failed: {e}"}), 500

    result["case_id"] = case_id
    result["case_created"] = created_new_case
    return jsonify(result), 201


@app.route("/api/documents/<doc_id>", methods=["DELETE"])
def delete_document(doc_id: str):
    """Remove a single document (and every row that references it) from
    whatever case it belongs to. A no-op-safe 404 if the document doesn't
    have a case (shouldn't happen for anything uploaded through this API,
    but guards against stale/partial data).
    """
    row = queries.get_document(db, doc_id)
    if row is None:
        return jsonify({"error": "not found"}), 404
    case_id = row[-1]  # case_id is the last column per queries.GET_DOCUMENT_SQL
    if not case_id:
        return jsonify({"error": "document has no case"}), 400
    try:
        case_queries.remove_document(db, case_id, doc_id)
    except Exception as e:
        return jsonify({"error": f"delete failed: {e}"}), 500
    return jsonify({"doc_id": doc_id, "deleted": True})


@app.route("/api/documents/<doc_id>/process", methods=["POST"])
def process_document(doc_id: str):
    """Trigger reasoning + action drafting for a document already past the
    confidence gate (status='reasoning'). Ingestion/extraction happen at
    upload time via orchestration.workflow.process_new_document, called
    from wherever file upload is handled (not in this minimal API).
    """
    try:
        result = workflow.compare_related_documents(db, settings, doc_id)
    except Exception as e:
        return jsonify({"error": f"comparison failed: {e}"}), 500
    return jsonify(result)


@app.route("/api/reviews", methods=["POST"])
def submit_review():
    body = request.get_json(force=True)
    required = ["doc_id", "field_id", "field_name", "ai_value", "human_value", "status", "reviewed_by"]
    missing = [k for k in required if k not in body]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    try:
        review_id = human_review_agent.submit_review(
            db,
            doc_id=body["doc_id"],
            field_id=body["field_id"],
            field_name=body["field_name"],
            ai_value=body["ai_value"],
            human_value=body["human_value"],
            status=body["status"],
            reviewed_by=body["reviewed_by"],
        )
        human_review_agent.advance_if_reviews_complete(db, body["doc_id"], settings.confidence_threshold)
    except Exception as e:
        return jsonify({"error": f"review submission failed: {e}"}), 500
    return jsonify({"review_id": review_id})


@app.route("/api/actions/<action_id>/decide", methods=["POST"])
def decide_action(action_id: str):
    body = request.get_json(force=True)
    decision = body.get("decision")
    decided_by = body.get("decided_by", "unknown")
    if decision not in ("approved", "rejected"):
        return jsonify({"error": "decision must be 'approved' or 'rejected'"}), 400
    try:
        action_agent.decide_action(db, action_id, decision, decided_by)
    except Exception as e:
        return jsonify({"error": f"decision failed: {e}"}), 500
    return jsonify({"action_id": action_id, "status": decision})


@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json(force=True)
    question = body.get("question")
    if not question:
        return jsonify({"error": "missing 'question'"}), 400
    try:
        result = chat_agent.ask(db, ro_db, settings, question)
    except Exception as e:
        return jsonify({"error": f"query failed: {e}"}), 500
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5005)
