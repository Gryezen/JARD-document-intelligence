"""
scripts/apply_schema.py — apply schema.sql (and optionally docs/mcp-grants.sql)
directly via pyexasol, using the same EXASOL_* env vars api/routes.py already
reads from .env.

This exists as a CLI-tool-agnostic fallback: exapump's exact flags vary by
version, so rather than depend on guessing them, this reuses the pyexasol
connection the app already relies on (see database/db.py, config.py).

Usage (run from the project root, with your .env already filled in):
    python scripts/apply_schema.py schema.sql
    python scripts/apply_schema.py docs/mcp-grants.sql

WARNING: schema.sql uses CREATE OR REPLACE TABLE and DROP TABLE IF EXISTS —
applying it wipes all existing rows in DOC_INTEL. That's expected for a
schema migration, but don't point this at anything you want to keep.
"""

import re
import ssl
import sys
from pathlib import Path

import pyexasol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings  # noqa: E402


def split_statements(sql_text: str) -> list[str]:
    """Split a .sql file into individual statements on top-level semicolons.

    Strips '--' line comments first (simple approach; schema.sql and
    mcp-grants.sql don't use semicolons or '--' inside string literals, so
    this doesn't need to be a full SQL tokenizer).
    """
    no_comments = re.sub(r"--[^\n]*", "", sql_text)
    statements = [s.strip() for s in no_comments.split(";")]
    return [s for s in statements if s]


def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <path-to-sql-file>")
        sys.exit(1)

    sql_path = Path(sys.argv[1])
    if not sql_path.exists():
        print(f"No such file: {sql_path}")
        sys.exit(1)

    settings = load_settings()
    conn_info = settings.exasol_rw
    statements = split_statements(sql_path.read_text())

    print(f"Connecting to {conn_info.dsn} as {conn_info.user}...")
    # Same websocket_sslopt as database/db.py's _connect(): the starter
    # kit's local Exasol instance uses a self-signed cert, so this skips
    # verification instead of failing with CERTIFICATE_VERIFY_FAILED.
    conn = pyexasol.connect(
        dsn=conn_info.dsn,
        user=conn_info.user,
        password=conn_info.password,
        websocket_sslopt={"cert_reqs": ssl.CERT_NONE},
    )

    try:
        for i, stmt in enumerate(statements, 1):
            preview = " ".join(stmt.split())[:80]
            print(f"[{i}/{len(statements)}] {preview}...")
            conn.execute(stmt)
        print(f"Done — applied {len(statements)} statement(s) from {sql_path}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
