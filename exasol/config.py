"""
config.py — central configuration for the agentic document intelligence platform.

All values are read from environment variables (see .env.example). Nothing
here should contain a real secret; the starter kit itself refuses to let you
print its credential files for the same reason (see AGENTS.md).

Two DB identities are expected, matching the starter kit's own model:
  - EXASOL_* / write identity: used by ingestion, extraction, reasoning,
    action and audit-log code paths (the orchestrator).
  - EXASOL_RO_* / read-only identity: the same one the starter kit's MCP
    server uses. The chat agent must connect with this identity ONLY, so a
    prompt-injected or malformed query can never write to DOC_INTEL.
"""

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is a convenience, not a hard requirement — env vars can
    # also be exported directly by the shell / deployment platform.
    pass


def _require(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class ExasolConnection:
    dsn: str            # host:port, e.g. "localhost:8563"
    user: str
    password: str
    schema: str = "DOC_INTEL"


@dataclass(frozen=True)
class Settings:
    # Read-write connection, used by every agent except chat.
    exasol_rw: ExasolConnection
    # Read-only connection, used ONLY by the chat agent's SQL execution path.
    exasol_ro: ExasolConnection

    # LLM — three independent slots (extraction, reasoning [also used by
    # action drafting + relationship inference], chat). Each slot picks its
    # own provider: "ollama" (local, free, runs on your GPU) or "gemini"
    # (hosted, needs GEMINI_API_KEY, subject to Gemini's quota). Default is
    # Ollama for everything; flip a single slot to "gemini" via env var if
    # you want that one path on a hosted model without touching the others.
    llm_api_key: str | None       # only required if any slot uses "gemini"
    ollama_host: str

    extraction_provider: str
    extraction_model: str
    reasoning_provider: str
    reasoning_model: str
    chat_provider: str
    chat_model: str

    # Confidence gate threshold (0-1). Below this, a field pauses for human review.
    confidence_threshold: float

    # Where uploaded source files are stored before/after processing.
    upload_dir: str


_DEFAULT_OLLAMA_MODEL = "qwen2.5:7b-instruct"


def _provider(name: str) -> str:
    value = os.getenv(name, "ollama").strip().lower()
    if value not in ("ollama", "gemini"):
        raise RuntimeError(f"{name} must be 'ollama' or 'gemini', got {value!r}")
    return value


def _model_default(provider: str) -> str:
    return _DEFAULT_OLLAMA_MODEL if provider == "ollama" else "gemini-3.6-flash"


def load_settings() -> Settings:
    extraction_provider = _provider("EXTRACTION_PROVIDER")
    reasoning_provider = _provider("REASONING_PROVIDER")
    chat_provider = _provider("CHAT_PROVIDER")

    # Gemini key is only mandatory if something actually uses Gemini —
    # a pure-Ollama setup shouldn't be blocked on an unrelated API key.
    needs_gemini = "gemini" in (extraction_provider, reasoning_provider, chat_provider)
    llm_api_key = _require("GEMINI_API_KEY") if needs_gemini else os.getenv("GEMINI_API_KEY")

    return Settings(
        exasol_rw=ExasolConnection(
            dsn=_require("EXASOL_DSN", "localhost:8563"),
            user=_require("EXASOL_USER", "sys"),
            password=_require("EXASOL_PASSWORD"),
            schema=os.getenv("EXASOL_SCHEMA", "DOC_INTEL"),
        ),
        exasol_ro=ExasolConnection(
            dsn=os.getenv("EXASOL_RO_DSN", os.getenv("EXASOL_DSN", "localhost:8563")),
            user=_require("EXASOL_RO_USER"),
            password=_require("EXASOL_RO_PASSWORD"),
            schema=os.getenv("EXASOL_SCHEMA", "DOC_INTEL"),
        ),
        llm_api_key=llm_api_key,
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        extraction_provider=extraction_provider,
        extraction_model=os.getenv("EXTRACTION_MODEL", _model_default(extraction_provider)),
        reasoning_provider=reasoning_provider,
        reasoning_model=os.getenv("REASONING_MODEL", _model_default(reasoning_provider)),
        chat_provider=chat_provider,
        chat_model=os.getenv("CHAT_MODEL", _model_default(chat_provider)),
        confidence_threshold=float(os.getenv("CONFIDENCE_THRESHOLD", "0.8")),
        upload_dir=os.getenv("UPLOAD_DIR", "./data/uploads"),
    )
