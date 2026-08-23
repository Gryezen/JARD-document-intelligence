# Running this locally on a teammate's machine

Two different goals get confused easily, so start here:

| Goal | What teammates need | Guide |
|---|---|---|
| **One shared, always-on instance** everyone just opens in a browser | Nothing at all — just a URL | See `docs/DEPLOY.md` (Render + cloud Exasol) |
| **Each teammate runs their own local copy** | Docker, plus a couple of one-command installs below | This doc |

If what you actually want is "my team opens a link and nothing else," skip
this file and use the deploy path instead — it's the only option that's
truly zero-install. What follows gets very close (three one-liners, no
manual dependency wrangling) but isn't literally nothing, because two
things this app depends on — the database and, by default, the LLM —
live outside the app container and can't responsibly be baked into it:
Exasol's local install needs privileged Docker/Podman access of its own,
and Ollama wants direct access to your GPU. Bundling either inside this
project's Dockerfile would make the image huge and fragile across OSes
for no real benefit over letting each run its own well-tested installer.

## What "install" actually means here

The **Dockerfile only packages this app** — Flask, the OCR pipeline
(tesseract/poppler), and all the Python dependencies. A teammate never
manually installs Python, tesseract, poppler, or any pip package — that's
the part Docker genuinely solves. It does not, and can't, package Exasol
or Ollama for you. So the real setup is three independent pieces, each a
single command:

1. **Docker** (Docker Desktop on Mac/Windows, Docker Engine on Linux) —
   the only truly unavoidable install, and only needed at all on
   Windows/Linux (see step 2).
2. **Exasol**, via the official local starter kit — one command, no
   account, no cloud, ~2 minutes:
   ```bash
   # macOS / Linux / WSL
   curl https://www.exasol.com/install/starter-kit.sh | sh
   ```
   ```powershell
   # Windows PowerShell — requires Docker Desktop already running
   irm https://www.exasol.com/install/starter-kit.ps1 | iex
   ```
   macOS runs the database natively (no Docker involved for this step at
   all). Linux and WSL use Docker/Podman under the hood. Windows requires
   Docker Desktop to already be running. Once it finishes, run whatever
   its output tells you to (typically an `info`-style command) to get the
   connection details: host, port (default `8563`), user, password.
3. **Ollama** (only if you're keeping the default local LLM provider —
   see the decision below):
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh   # macOS/Linux
   # or download the installer from ollama.com for Windows
   ollama pull qwen2.5:7b-instruct
   ollama serve
   ```

## Decision: Ollama (local) vs Gemini (hosted) for the LLM

`.env.example` defaults every provider slot (`EXTRACTION_PROVIDER`,
`REASONING_PROVIDER`, `CHAT_PROVIDER`) to `ollama`. Pick based on your
team's hardware and patience:

- **Keep Ollama** if teammates have a reasonably capable machine (the
  default `qwen2.5:7b-instruct` model is a multi-GB download and wants a
  decent GPU or Apple Silicon to run at a usable speed). Upside: no API
  key, no account, no rate limit, works offline once pulled.
- **Switch a slot to `gemini`** if a teammate's machine can't comfortably
  run a local model. Each teammate should get **their own** free key at
  https://aistudio.google.com/apikey and put it in their own `.env` —
  don't share one team key across multiple people running simultaneously,
  that just means everyone hits the same 20-requests/day free-tier ceiling
  together instead of each having their own.

Mixing is fine — e.g. `EXTRACTION_PROVIDER=ollama` (bulk of the calls,
runs locally for free) with `CHAT_PROVIDER=gemini` (occasional, fine on
free tier) if that split suits your hardware better.

## Putting it together

```bash
# 1. Get the code
git clone <your repo> && cd <repo>

# 2. Configure
cp .env.example .env
```

Edit `.env`:
- `EXASOL_DSN` / `EXASOL_RO_DSN` → `host.docker.internal:8563` (not
  `localhost:8563` — the app runs inside a container, and
  `host.docker.internal` is how the container reaches services running on
  your actual machine; `docker-compose.yml` is already configured to make
  this resolve on Mac, Windows, and Linux).
- `EXASOL_USER` / `EXASOL_PASSWORD` → from the starter kit's `info`
  output.
- If keeping Ollama: `OLLAMA_HOST=http://host.docker.internal:11434` for
  the same reason as the DSN above.
- If using Gemini for any slot: your own `GEMINI_API_KEY`.

```bash
# 3. Start the app
docker compose up --build

# 4. One-time: apply the schema (run once per fresh Exasol instance —
#    re-run any time schema.sql changes, since it uses CREATE OR REPLACE
#    and wipes existing rows)
docker compose exec app python scripts/apply_schema.py schema.sql
docker compose exec app python scripts/apply_schema.py docs/mcp-grants.sql

# 5. Open http://localhost:10000
```

That's the whole loop after the three one-time installs above. From here
on, `docker compose up` is the only command a teammate needs to run the
app again.

## Tearing down / resetting

- `docker compose down` stops the app (uploaded files persist in the
  `uploads` volume; delete it with `docker compose down -v` to reset).
- Stopping/removing the Exasol starter kit is a separate command from its
  own tooling — check its `info`/`status`/`down`-style commands rather
  than anything in this repo.
