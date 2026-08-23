# JARD demo video — production plan

Target length: **3:00**. Built around the app's actual UI (the 6-stage rail:
Upload → Extract → Review → Compare → Approve → Audit) rather than a generic
hackathon-demo structure, so every beat maps to something real you can click.

---

## 1. Before you record (do this first — ~30–45 min)

The single biggest risk to a smooth recording is hitting Gemini's free-tier
rate limit *while the camera is rolling*. Don't extract/reason live for
everything you show.

- [ ] **Pre-seed one full case** ahead of time: upload 2+ related documents
      (e.g. an income certificate + a welfare application) into a case, run
      `/api/cases/<id>/process`, and confirm it actually produced a
      discrepancy and a proposed action. Do this the night before, not the
      morning of — so if you *do* hit a 429, you have time to retry.
- [ ] **Pre-seed one pending human-review item**: upload a document you know
      will land a field under your confidence threshold (or temporarily
      raise the threshold in `.env` for one upload), so the Review Queue
      screen isn't empty when you get there.
- [ ] Leave **one fresh document unprocessed** so you have something real to
      upload live in Segment 2 — the live upload is for showing the
      *interaction* (dropzone, progress bar), not for waiting on an LLM call
      on camera. Cut away before/while it processes.
- [ ] Clear or curate `/api/discrepancies/open` so the Discrepancies table
      doesn't show half-finished test junk from development.
- [ ] Set your browser zoom to **110–125%** — screen recordings read small
      fields as illegible otherwise. Hide the bookmarks bar and any
      extensions toolbar clutter.
- [ ] Silence notifications (Slack, phone, OS notification center) on the
      recording machine.
- [ ] Rehearse the click-path once, silently, stopwatch running, before you
      record with voice — you want to know where you're clicking a half
      second before you say it.

---

## 2. Recording setup

- **Screen recorder**: OBS Studio (free, Win/Mac/Linux) is the most reliable
  choice. QuickTime (Mac) or Xbox Game Bar (Windows) work fine for something
  this simple if you don't want to install anything.
- **Resolution**: record at 1920×1080. Don't record in a tiny window and
  scale up later — it'll look soft.
- **Audio**: record voiceover live while screen-recording if you're
  confident in one take; otherwise **record the screen silently first**,
  then record voiceover separately against the muted playback and sync in
  editing. The second approach gives a much cleaner result if you're not
  used to narrating live — no "um," no mouse-click breathing room mismatches.
- **Cursor visibility**: turn on cursor highlighting (OBS has a filter for
  this, or use a free tool like Mouseposé/Cursor Highlight) — a plain small
  system cursor is hard to track at video scale.
- **Mic**: any USB or headset mic beats a laptop's built-in mic. Record in
  a room with soft furnishing (closet, bedroom) to cut echo if you don't
  have a treated space.

---

## 3. Shot list + voiceover script

Timestamps are targets, not hard cuts — pacing yourself to the visuals
matters more than hitting the number exactly. Each segment name matches a
section header below so you can re-order easily if needed.

### [0:00–0:15] Hook — the problem

**Visual:** Landing page hero (`Clarity across every document.`). Slow
cursor movement or a simple fade-in; no clicking yet.

> "Document intelligence tools are good at one thing: pulling text out of a
> PDF. What they don't do is notice when two related documents disagree
> with each other — an invoice against its purchase order, an income
> certificate against a welfare application. That gap is what JARD closes."

### [0:15–0:35] What JARD is

**Visual:** Scroll the landing page past the hero into the "How JARD works"
rail (Upload → Extract → Review → Compare → Approve → Approve → Audit — the
8-step version further down, or the 6-stage rail at the top, whichever
reads better on screen). Then click **Sign In**.

> "JARD ingests documents, extracts structured fields, and — this is the
> part that matters — reasons across documents that belong to the same
> case to catch discrepancies. It drafts a next action, but a human always
> approves it before anything goes out. Everything is backed by Exasol, and
> every decision is traceable in an audit log."

### [0:35–1:00] Upload

**Visual:** Land on the dashboard. Select (or create) a case from the case
dropdown. Drag your one pre-staged fresh file onto the dropzone; show the
upload progress bar filling. Cut away as soon as it's uploaded — don't
wait on processing here.

> "Let's walk through a real case. I'll drop a document in — JARD accepts
> PDFs, scanned images, or plain text, with OCR for anything that's not
> already machine-readable text."

*(Cut to the pre-seeded, already-processed case for the rest of the demo.)*

### [1:00–1:25] Extraction + Human Review

**Visual:** Switch to your pre-seeded case with a pending review item. Show
the Review Queue: the extracted field, its value, the confidence bar, and
the "Correct Value" / "Confirm" buttons.

> "Every extracted field gets a confidence score. Anything below threshold
> — right now that's [X]% — doesn't get to move forward silently. It stops
> here, in front of a human, before it can affect anything downstream."

Optionally click **Correct Value**, type a correction, hit **Confirm** —
shows the interaction working, not just static.

### [1:25–1:55] Reasoning — Discrepancies (the differentiator)

**Visual:** Navigate to Discrepancies. Show the table: case, field,
expected vs. actual, severity badge, status badge. Click **Inspect** on one
row.

> "This is the core of JARD. Once two related documents are both
> extracted, JARD compares them field by field. Here, the income figure on
> the application doesn't match the certificate — flagged medium severity,
> open status. This is the check that manual document review usually
> misses until it's too late."

### [1:55–2:20] Action proposal + approval

**Visual:** Navigate to Proposed Actions. Show the drafted email/task, the
"Human approval required" flag, and click **Approve** (or **Reject** —
whichever tells a cleaner story).

> "JARD doesn't just flag the problem — it drafts the next step. Here's a
> vendor clarification request, fully written. But JARD never sends this
> automatically. A person reviews it, and only an explicit approval moves
> it forward. JARD recommends. Humans decide."

### [2:20–2:40] Audit trail

**Visual:** Navigate to Audit Log for the same document. Scroll the
timeline of events.

> "Every step we just saw — extraction, the confidence gate, the reasoning
> output, the human correction, the approval — is written to an audit log
> in Exasol. Nothing in this system is 'trust me, it happened.' It's all
> traceable."

### [2:40–3:00] Knowledge query + close

**Visual:** Navigate to the Knowledge Base search. Use the pre-filled
example query (*"Which vendors have unresolved discrepancies this
month?"*) or type your own. Click **Run Query**, show the answer.

> "And because it's all sitting in Exasol, you can just ask. This runs as
> a validated, read-only SQL query — no natural-language input can touch
> the data. [Read the answer or summarize it in one line.] That's JARD —
> extraction, reasoning, human control, and a queryable knowledge base, in
> one pipeline."

*(End on the JARD logo / landing hero, or a simple title card with your
team name and "PS23 — Exasol AI Build Challenge 2026.")*

---

## 4. Editing & export

- Free editors that'll handle this fine: **DaVinci Resolve** (most
  capable, steeper learning curve), **CapCut** (fastest to learn), iMovie
  if you're on Mac.
- Add captions/subtitles — cheap to do, and judges skimming many
  submissions often watch muted first.
- Trim dead air aggressively between segments; a demo video should feel
  faster-paced than the actual usage.
- Export **H.264 MP4, 1080p, ~10–15 Mbps** — compatible with essentially
  every submission portal.
- Watch it back once at 1.5x speed before submitting — this surfaces
  pacing problems (a segment that drags) much faster than watching at
  normal speed.

## 5. If you need a shorter cut

If PS23's actual limit is 90 seconds, not 3 minutes, keep Segments 1, 3
(Discrepancies), 4 (Approval), and the closing line from Segment 6 — cut
Upload, Review, and Audit down to a single sentence each over a quick
montage instead of full walkthroughs. The discrepancy-detection and
human-approval moments are the two beats that actually differentiate this
project; protect those two above everything else if you're forced to cut.
