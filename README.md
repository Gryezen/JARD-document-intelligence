# JARD — Agentic Document Intelligence Platform (PS23)

**Exasol AI Build Challenge 2026**

## One-sentence pitch

Most document-intelligence tools stop at "PDF → JSON." This platform goes further: it
extracts structured fields, gates on confidence, **reasons across related documents**
to catch discrepancies, drafts a next action, keeps a human in the loop, and lets
anyone query the accumulated knowledge base in plain English — all backed by Exasol.



[![JARD Demo Video](https://img.youtube.com/vi/M_TS8T7-XnE/maxresdefault.jpg)](https://youtu.be/M_TS8T7-XnE)



## Why this problem still exists

OCR and field extraction are solved problems (Azure Document Intelligence, Google
Document AI, Amazon Textract, ABBYY Vantage). What none of them do out of the box is
**reason across documents that belong to the same case** — e.g. an invoice, its PO,
and its contract, or a citizen's income certificate against their welfare application
— and turn that reasoning into a concrete next step a human can approve. That's the
gap this project targets, drawing on real-world evidence of the problem (Swedish Land
Registry, Telangana High Court, Indian voter-roll ETL, government health data entry,
banking KYC, Maharashtra municipal corporations).

## Architecture

```
User → Frontend → Orchestrator → Ingestion → Extraction → Confidence Gate
                                                  ├─ low  → Human Review → continue
                                                  └─ high → Reasoning
                                                                ├─ no discrepancy → Complete
                                                                └─ discrepancy    → Action → Human Approval

Chat Agent → SQL Validation → Read-only Exasol Query → Answer
```

Every agent action — extraction, gate decision, reasoning output, action draft,
human override, chat query — is written to `AUDIT\\\_LOG`. Nothing in this system
should be trusted as "what happened" unless it's traceable back to an audit row.

## Components

|Component|Responsibility|
|-|-|
|`agents/ingestion.py`|Normalize PDF/image/scanned input into text + metadata|
|`agents/extraction.py`|Extract structured fields + confidence per field|
|`agents/confidence.py`|Deterministic routing: below threshold → human review|
|`agents/human\\\_review.py`|Records corrections/approvals, unblocks a document once resolved|
|`agents/relationships.py`|Links documents into the same case (rule pass + bounded LLM fallback)|
|`agents/reasoning.py`|Compare related documents, produce structured discrepancies|
|`agents/action.py`|Draft an email/task proposal for a discrepancy (never auto-sent)|
|`agents/report.py`|One-call plain-language summary of everything uploaded into a case|
|`agents/chat.py`|Natural language → validated read-only SQL → Exasol → explanation|
|`agents/llm\\\_client.py`|Shared forced-structured-output wrapper over the Ollama/Gemini backends|
|`database/db.py`|Two connection identities: read-write (agents) and read-only (chat)|
|`database/cases.py`|CRUD for `CASES`, the container documents are uploaded into and compared within|
|`database/queries.py`|Read-only helpers shared by `api/routes.py`|
|`database/audit.py`|Single place every agent logs an explainable event|
|`orchestration/state.py`|Legal state transitions for a document's lifecycle|
|`orchestration/workflow.py`|Drives the loop above, calling agents between transitions|

Agents that don't exist yet are intentionally not stubbed with fake logic — see
**Status** below.

## Database (Exasol, schema `DOC\\\_INTEL`)

`schema.sql` defines: `CASES`, `DOCUMENTS`, `EXTRACTED\\\_FIELDS`,
`DOCUMENT\\\_RELATIONSHIPS`, `DISCREPANCIES`, `ACTIONS`, `HUMAN\\\_REVIEWS`,
`AUDIT\\\_LOG`. A case is the container a user explicitly groups uploaded
documents into, and cross-document reasoning is scoped to it — documents
are only ever compared against other documents in the *same* case, never
against the whole registry. `docs/mcp-grants.sql` documents the read-only
grant used by both the chat agent and the starter kit's own MCP server,
so a bad or injected query is rejected by Exasol's grants, not just by
application-level SQL validation.

## Setup

### 1\. Install the Exasol Personal Local starter kit

```bash
curl -fsSL https://raw.githubusercontent.com/exasol-labs/exasol-personal-local-starterkit/main/install.sh | sh
exakit status      # wait for "running"
exakit info        # connection details
```

### 2\. Load the schema

```bash
exapump sql -p starter-kit -f schema.sql
exapump sql -p starter-kit -f docs/mcp-grants.sql
```

`exapump`'s flags have changed across versions — if `-f` isn't recognized as
"file" on your install (run `exapump sql --help` to check), use the
CLI-agnostic fallback instead, which connects with the same credentials
your `.env` already has configured:

```bash
python scripts/apply\\\_schema.py schema.sql
python scripts/apply\\\_schema.py docs/mcp-grants.sql
```

Re-run this (either way) any time `schema.sql` changes — it uses
`CREATE OR REPLACE TABLE` / `DROP TABLE IF EXISTS`, so applying it wipes
existing rows in `DOC\\\_INTEL`. If you see `object CASES not found` or
`object CASE\\\_ID not found` errors from the API, that means the schema
hasn't been re-applied since it last changed.

### 3\. Configure the app

```bash
cp .env.example .env
# fill in EXASOL\\\_PASSWORD, EXASOL\\\_RO\\\_USER/PASSWORD (from `exakit info`)
```

This project runs fully on **Ollama** — no API key needed. Install it,
pull the model, and start it:

```bash
curl -fsSL https://ollama.com/install.sh | sh   # macOS/Linux; see ollama.com for Windows
ollama pull qwen3:8b
ollama start
```

(`config.py` also supports switching any of `EXTRACTION\\\_PROVIDER` /
`REASONING\\\_PROVIDER` / `CHAT\\\_PROVIDER` to `gemini` per slot if you ever
want a hosted fallback — see `docs/TEAM-SETUP.md` — but the default,
Ollama-only setup above is all this project actually uses.)

### 4\. Install dependencies

**4a. System packages (OCR engine + PDF rasterizer):**

```bash
sudo apt-get install -y tesseract-ocr poppler-utils
```

**4b. Python packages:**

```bash
pip install -r requirements.txt --break-system-packages   # or use a venv
```

### 5\. Verify the wiring

```bash
python main.py
```

Expected output: read-write connection confirmed, read-only connection
confirmed, `DOCUMENTS` row count printed. This step only checks
connectivity — it does not start the app.

### 6\. Run the dashboard

```bash
python -m api.routes
```

Run this from the project root (not from inside `api/`) — the app uses
absolute imports like `from agents import chat as chat\\\_agent`, which only
resolve correctly when the project root is on `sys.path`, and `-m`
guarantees that. Running `python api/routes.py` directly will fail with
`ModuleNotFoundError: No module named 'agents'`.

Once it's running, open **http://localhost:5005** in a browser — that's
the whole app: Flask serves the `frontend/index.html` dashboard at `/`
and the JSON API under `/api/...` from the same process, so there's
nothing separate to start for the frontend. (This is the local dev port
set in `api/routes.py`; the Dockerized/Render deployment listens on
`10000` instead — see `docs/TEAM-SETUP.md` / `docs/DEPLOY.md`.)

## Reliability rules this project follows

* Chat-generated SQL is read-only and runs under a dedicated Exasol identity
with `SELECT`-only grants on `DOC\\\_INTEL` — enforced at the database level,
not just by prompting.
* No email is sent automatically; `agents/action.py` only ever produces a
draft that a human approves via `ACTIONS.status`.
* Confidence routing (`agents/confidence.py`) is deterministic application
code, not a second model call.
* `.env` is git-ignored; only `.env.example` (names, no values) is committed.

## Status

**Built and tested:**

* Schema, config, DB layer (read-write + read-only identities), audit logger, typed models, state machine
* `agents/ingestion.py` — native PDF text layer used directly when present; scanned PDFs (no text layer) and image uploads fall back to Tesseract OCR, with the average word-confidence recorded in `AUDIT\\\_LOG` and a separate low-confidence warning logged below 60% so a poor scan is distinguishable from genuine field ambiguity later in the pipeline. Verified against real generated files (native-text PDF, image-only PDF, plain image), not mocked.
* `agents/extraction.py` — forced structured-output call (via `agents/llm\\\_client.py`, Ollama by default or Gemini per `EXTRACTION\\\_PROVIDER`) that returns structured fields + confidence, persisted to `EXTRACTED\\\_FIELDS`
* `agents/confidence.py` — deterministic gate (no model call), routes to `review` or `reasoning`
* `agents/human\\\_review.py` — records corrections/approvals, advances the document once all low-confidence fields are resolved
* `agents/reasoning.py` — cross-document comparison producing structured `DISCREPANCIES`, not prose
* `agents/action.py` — drafts email + task proposals per discrepancy; never sends anything, only writes `status='proposed'` rows for human approval
* `agents/chat.py` — NL → SQL with a forced tool call, a hard-coded schema description (no guessed columns), and `validate\\\_sql()` as a second line of defense in front of the read-only DB identity
* `agents/relationships.py` — links documents into the same case: a free deterministic rule pass (same vendor + a known compatible type pair, e.g. invoice↔purchase\_order) runs first, with a bounded LLM fallback only for vendor-matched pairs whose document types aren't in the known list yet
* `agents/report.py` — one Gemini/Ollama call per case (not per document) that turns the case's already-extracted fields and discrepancies into a short plain-language summary a case handler can read in five seconds
* `orchestration/workflow.py` — drives `ingest → extract → link relationships → confidence gate` and, separately, `reasoning → action → complete` for a document once unblocked
* `api/routes.py` — Flask endpoints for upload, documents, fields, discrepancies, audit timeline, review submission, action approval, and chat. `/api/documents/upload` runs the full ingest→extract→link→gate pipeline synchronously so a demo shows real processing, not a spinner
* `frontend/` — single-page HTML/JS/CSS app served by Flask itself (`api/routes.py` mounts it as static root): a marketing landing page plus a signed-in dashboard (documents, case comparison, review queue, discrepancies, proposed actions, audit log, NL chat) wired against the real API — sign-in itself is local-only (`localStorage`), everything past it is live
* `tests/` — 74 passing unit tests covering the confidence gate, state-machine transitions, SQL validation, relationship rule-matching (and its LLM fallback), OCR/PDF/plain-text ingestion, extraction/reasoning/action/chat agent call sites, the shared `agents/llm\\\_client.py` wrapper, and that every `api/routes.py` endpoint returns a JSON error (not a bare 500) when the agent it calls fails (all run without a live Exasol connection or API key; mocked at the `call\\\_tool` boundary using real `google.genai.types` objects, and the OCR tests generate real image/PDF fixtures on the fly and run actual Tesseract on them)

## Sample data

Two corpora live under `data/`, for different purposes:

* **`data/sample/`** — a small synthetic corpus (42 documents, PNG, generated by `scripts/generate\\\_sample\\\_documents.py`) with clean key-value layouts and a controlled mix of OCR messiness (clean / stamped / blurred+rotated). Good for a quick pipeline smoke test and for demoing the confidence gate.
* **`data/simulated\\\_v2/`** — a larger, more realistic corpus (50 case folders, \~168 files, PDF + plain text, generated by another Claude session's `generate\\\_simulated\\\_dataset.py`) written as dense bureaucratic prose rather than tables, with \~10-20% of fields intentionally corrupted (typos, transposed digits, OCR-style character swaps) and per-field ground truth in `manifest.json` — the better stress test for `agents/extraction.py`'s actual reading comprehension, and for scoring extraction accuracy automatically. 4 of its 50 cases carry a deliberate larger cross-document discrepancy for the reasoning agent to catch. See `data/simulated\\\_v2/README.md` for the corruption model and regeneration command.

Neither corpus is derived from a real person, real government record, or external dataset (FUNSD/CORD/RVL-CDIP/data.gov.in were considered and ruled out — see `data/sample/README.md` for why: license terms, unreachable hosts, and content mismatch with this project's schema). Both are safe to commit and redistribute.

**Known limitation:** relationship linking only runs once, right after a document's own extraction. If document A finishes with no relationships (nothing to link to yet) and document B is uploaded later and links back to A, A itself is not re-queued into reasoning — only B proceeds to compare against A. For the demo (all three related documents uploaded in one sitting) this doesn't bite, but a production version needs a "wake up documents that just gained a relationship" step in `orchestration/workflow.py`.

Run `pytest` from the project root to run the test suite.

## Team

Four-way split (see the **Components** table above for what each piece owns):
Extraction · Data (Exasol schema/audit) · Orchestration (reasoning/action/chat) ·
Frontend/Demo.

