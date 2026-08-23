# Container image for the Agentic Document Intelligence Platform.
#
# Built for Render.com (see render.yaml) but works on any container host.
# Uses a real Dockerfile rather than Render's native Python build because
# pytesseract and pdf2image need OS packages (tesseract-ocr, poppler-utils)
# that Render's native buildpack build step cannot apt-get install.

FROM python:3.13-slim

# tesseract-ocr: OCR fallback for scanned PDFs / images (agents/ingestion.py)
# poppler-utils: gives pdf2image its `pdftoppm`/`pdftocairo` binaries
# curl: used only by the HEALTHCHECK below to hit the app's own /api/config
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Uploaded files land here at runtime (see config.py's UPLOAD_DIR). On
# Render's free/starter tiers this directory is ephemeral — it's wiped on
# every redeploy/restart — which is fine for a demo but not for
# production persistence. Mount a Render Disk at this path if uploads
# need to survive restarts.
RUN mkdir -p /app/data/uploads

# Run as a non-root user. Not optional-hardening theater here specifically:
# this app accepts arbitrary uploaded files (pdf/png/jpg/tiff/bmp/txt) and
# feeds them through pypdf/pillow/pytesseract/pdf2image, all of which parse
# untrusted, attacker-controllable input — exactly the kind of code path
# where a parser bug turning into code execution matters most. Running as
# root buys nothing here and turns that class of bug into a bigger one.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PORT=10000
EXPOSE 10000

# Hits the same endpoint render.yaml's healthCheckPath already points at,
# so `docker run` / `docker compose` locally get the identical signal
# Render uses — a container that's up but can't reach Exasol (db = Database(
# settings) connects eagerly at import time in api/routes.py) fails this
# the same way it fails Render's check, instead of only surfacing on the
# first real request.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f "http://localhost:${PORT}/api/config" || exit 1

CMD ["sh", "-c", "gunicorn -w 2 -k gthread --threads 4 -b 0.0.0.0:${PORT} api.routes:app"]
