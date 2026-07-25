"""Intra-repo dataflow detection: which files read or write which SQL
tables/views, via embedded query strings (regex, language-agnostic — a SQL
statement looks the same whether it's inside a Python/JS/C# string literal).

Emits unresolved ``(src_id, table_name, rel_path, line)`` reference tuples —
the same shape as :mod:`.http`'s endpoint refs and :mod:`..sql`'s FK refs —
resolved repo-wide against tables/views actually *defined* somewhere in the
repo (``kb/parse.py``'s ``_resolve_name_refs``, ``target_kinds=_SQL_KINDS``)
rather than fabricating a node here. A query against a table this repo never
defines is an honest miss, not a guessed link — the same stance
:mod:`..sql`'s FK extraction takes for a ``REFERENCES`` target it never sees
a ``CREATE TABLE`` for.

Table names are normalized with :func:`..sql._norm_name` — the exact recipe
``kb/sql.py`` uses for the table/view nodes this must match by name, so a
second, drifting copy of "strip brackets, casefold" never has to stay in
sync by hand.

High-precision, not exhaustive: only literal SQL statement text is matched
(``SELECT ... FROM`` / ``INSERT INTO`` / ``UPDATE ... SET`` / ``DELETE
FROM``), so a query built by string concatenation or an ORM with no raw-SQL
escape hatch is missed — an honest undercount, never a false read/write.
"""

from __future__ import annotations

import re

from .. import sql as _sql
from ..ids import make_id

_SELECT = re.compile(r"\bSELECT\b.*?\bFROM\s+" + _sql._NAME, re.IGNORECASE | re.DOTALL)
_INSERT = re.compile(r"\bINSERT\s+INTO\s+" + _sql._NAME, re.IGNORECASE)
_UPDATE = re.compile(r"\bUPDATE\s+" + _sql._NAME + r"\s+SET\b", re.IGNORECASE)
_DELETE = re.compile(r"\bDELETE\s+FROM\s+" + _sql._NAME, re.IGNORECASE)

# A SELECT's FROM must follow within this many characters to count as the
# same statement -- bounds the DOTALL '.*?' scan so it can't leap across
# unrelated code to the next SELECT's FROM in the file.
_MAX_SELECT_SPAN = 300


def extract_data_refs(
    repo_id: str, rel_path: str, source
) -> tuple[list[tuple[str, str, str, int]], list[tuple[str, str, str, int]]]:
    """``(reads, writes)`` reference tuples for one file — file-level
    granularity (like :mod:`.http`'s endpoints, not per-function), resolved
    later, repo-wide, against actual table/view definitions.
    """
    text = source.decode("utf-8", "replace") if isinstance(source, (bytes, bytearray)) else source
    file_id = make_id(repo_id, rel_path)
    reads: list[tuple[str, str, str, int]] = []
    writes: list[tuple[str, str, str, int]] = []
    seen: set[tuple[str, str]] = set()

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    def scan(rx: re.Pattern, bucket: list, tag: str, max_span: int | None = None) -> None:
        for m in rx.finditer(text):
            if max_span is not None and (m.end() - m.start()) > max_span:
                continue  # spans past this statement into unrelated code -- not trustworthy
            name = _sql._norm_name(m.group(1))
            if not name or (tag, name) in seen:
                continue
            seen.add((tag, name))
            bucket.append((file_id, name, rel_path, line_of(m.start())))

    scan(_SELECT, reads, "r", max_span=_MAX_SELECT_SPAN)
    scan(_INSERT, writes, "w")
    scan(_UPDATE, writes, "w")
    scan(_DELETE, writes, "w")

    return reads, writes
