"""
tests/test_no_reserved_word_aliases.py — static guard against reintroducing
`AS value` / `AS action` / `AS timestamp` (or similar) in raw SQL.

Exasol rejects these as column ALIASES, not just as column names — confirmed
live against a real Exasol instance:

    SELECT field_value AS value, ...   -> syntax error, unexpected VALUE_
    SELECT action_name AS action, ...  -> syntax error, unexpected ACTION_

This is why EXTRACTED_FIELDS.field_value and AUDIT_LOG.action_name/logged_at
are named the way they are in schema.sql in the first place (VALUE, ACTION,
and TIMESTAMP are Exasol reserved words) — but it's easy to reintroduce the
same collision by aliasing *back* to the reserved word for a nicer API key.
Every consumer of these queries reads rows by tuple position (see
api/routes.py's `cols` lists and the manual dict-builders in
agents/confidence.py and agents/reasoning.py), so the alias was always dead
weight — renaming happens in Python, never in SQL.
"""

import glob
import re

_RESERVED_ALIAS_PATTERN = re.compile(r"\bAS\s+(VALUE|ACTION|TIMESTAMP)\b", re.IGNORECASE)


def _iter_source_files():
    for pattern in ("*.py", "agents/*.py", "database/*.py", "orchestration/*.py", "api/*.py"):
        yield from glob.glob(pattern)


def test_no_sql_aliases_a_reserved_word():
    offenders = []
    for path in _iter_source_files():
        if path.startswith("tests"):
            continue
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                if _RESERVED_ALIAS_PATTERN.search(line):
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Found SQL aliasing to an Exasol-reserved word (VALUE/ACTION/TIMESTAMP) — "
        "this fails at query time even though it looks fine in Python:\n"
        + "\n".join(offenders)
    )
