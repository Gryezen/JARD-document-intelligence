"""
agents/ingestion.py — turns an uploaded file into (a) a DOCUMENTS row and
(b) plain text the extraction agent can read.

Scope for the hackathon MVP: PDF (text layer or scanned), common image
formats, and plain text. Native PDF text is used directly when present,
since re-OCRing a clean text layer only introduces errors. Scanned PDFs
(no text layer) and image uploads are OCR'd via Tesseract, with a
per-document OCR confidence recorded in AUDIT_LOG so a consistently
low-confidence scan is visible before it ever reaches the extraction
agent. Plain .txt uploads skip OCR entirely, same as a native PDF text
layer.
"""

import uuid
from pathlib import Path

import pytesseract
from pdf2image import convert_from_path
from PIL import Image
from pypdf import PdfReader

from database.audit import log_event
from database.db import Database

INSERT_DOCUMENT_SQL = """
    INSERT INTO DOCUMENTS
        (doc_id, case_id, filename, document_type, vendor, status, source_path, page_count, uploaded_by, uploaded_at, updated_at)
    VALUES
        ({doc_id}, {case_id}, {filename}, {document_type}, {vendor}, {status}, {source_path}, {page_count!d}, {uploaded_by}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
"""

# Below this average Tesseract word-confidence, the scan is flagged in the
# audit log as likely too poor to trust downstream extraction confidence
# scores on. This does NOT block the pipeline — the extraction agent still
# runs and assigns its own per-field confidence — it's a documented warning
# so a case handler can tell "the model was unsure" apart from "the source
# scan itself was unreadable."
_OCR_LOW_CONFIDENCE_THRESHOLD = 60.0


class IngestionResult:
    def __init__(self, doc_id: str, text: str, page_count: int, ocr_confidence: float | None = None):
        self.doc_id = doc_id
        self.text = text
        self.page_count = page_count
        self.ocr_confidence = ocr_confidence  # None when no OCR was needed (native PDF text)


def _ocr_image(image: Image.Image) -> tuple[str, float | None]:
    """Run Tesseract on one image, return (text, average_word_confidence).

    Tesseract reports -1 confidence for non-text detections (e.g. layout
    blocks with no recognized characters); those are excluded from the
    average rather than dragging it down artificially.
    """
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    text = pytesseract.image_to_string(image)
    confidences = [int(c) for c in data["conf"] if str(c) not in ("-1", "")]
    avg_confidence = sum(confidences) / len(confidences) if confidences else None
    return text, avg_confidence


def _extract_pdf_text(path: Path) -> tuple[str, int, float | None]:
    """Try the PDF's native text layer first. If it's empty (a scanned
    PDF with no embedded text), fall back to rasterizing each page and
    running OCR on it.
    """
    reader = PdfReader(str(path))
    pages_text = [page.extract_text() or "" for page in reader.pages]
    combined = "\n\n".join(pages_text)
    page_count = len(reader.pages)

    if combined.strip():
        return combined, page_count, None  # native text layer, no OCR confidence to report

    images = convert_from_path(str(path))
    ocr_texts = []
    confidences = []
    for image in images:
        text, conf = _ocr_image(image)
        ocr_texts.append(text)
        if conf is not None:
            confidences.append(conf)
    avg_conf = sum(confidences) / len(confidences) if confidences else None
    return "\n\n".join(ocr_texts), page_count, avg_conf


def _extract_image_text(path: Path) -> tuple[str, float | None]:
    image = Image.open(path)
    return _ocr_image(image)


def extract_text_from_file(file_path: str) -> tuple[str, int, float | None]:
    """Turn a file already on disk into (text, page_count, ocr_confidence)
    without touching the database or creating a DOCUMENTS row.

    Split out of ingest_document() so orchestration.workflow.retry_document()
    can re-run just the text-extraction step against the same stored file
    after a failure (source_path is kept permanently — see
    api/routes.py's upload handler) without re-ingesting the file as a
    brand-new document.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(file_path)

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    elif suffix in (".png", ".jpg", ".jpeg", ".tiff", ".bmp"):
        text, ocr_confidence = _extract_image_text(path)
        return text, 1, ocr_confidence
    elif suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
        return text, 1, None
    else:
        raise ValueError(f"Unsupported file type: {suffix}")


def ingest_document(
    db: Database,
    file_path: str,
    filename: str,
    uploaded_by: str | None = None,
    document_type: str | None = None,
    vendor: str | None = None,
    case_id: str | None = None,
) -> IngestionResult:
    """Normalize a file into text and register it in DOCUMENTS.

    document_type/vendor can be passed in if already known (e.g. the user
    picked "invoice" on upload); otherwise leave None for the extraction
    agent to fill in. case_id ties this document to the case (see
    database/cases.py) it was uploaded into — cross-document reasoning is
    scoped to documents sharing a case, so this is what makes that scoping
    possible downstream in agents/relationships.py.
    """
    text, page_count, ocr_confidence = extract_text_from_file(file_path)
    path = Path(file_path)

    doc_id = str(uuid.uuid4())
    db.execute(
        INSERT_DOCUMENT_SQL,
        {
            "doc_id": doc_id,
            "case_id": case_id,
            "filename": filename,
            "document_type": document_type,
            "vendor": vendor,
            "status": "uploaded",
            "source_path": str(path),
            "page_count": page_count,
            "uploaded_by": uploaded_by,
        },
    )

    ocr_note = f", ocr_confidence={ocr_confidence:.1f}" if ocr_confidence is not None else ", ocr=not_needed"
    log_event(
        db,
        agent_name="ingestion",
        action="ingested_document",
        doc_id=doc_id,
        input_summary=f"file={filename}, type={path.suffix}",
        output_summary=f"page_count={page_count}, text_chars={len(text)}{ocr_note}",
        confidence=(ocr_confidence / 100.0) if ocr_confidence is not None else None,
    )

    if ocr_confidence is not None and ocr_confidence < _OCR_LOW_CONFIDENCE_THRESHOLD:
        log_event(
            db,
            agent_name="ingestion",
            action="low_quality_scan_warning",
            doc_id=doc_id,
            output_summary=(
                f"OCR average word confidence {ocr_confidence:.1f} is below the "
                f"{_OCR_LOW_CONFIDENCE_THRESHOLD} warning threshold. Extraction "
                f"confidence downstream may reflect scan quality more than "
                f"genuine field ambiguity — consider re-scanning at higher "
                f"resolution."
            ),
            confidence=ocr_confidence / 100.0,
        )

    if not text.strip():
        log_event(
            db,
            agent_name="ingestion",
            action="empty_text_warning",
            doc_id=doc_id,
            output_summary="No extractable text found even after OCR — file may be blank, corrupted, or unreadable.",
        )

    return IngestionResult(doc_id=doc_id, text=text, page_count=page_count, ocr_confidence=ocr_confidence)
