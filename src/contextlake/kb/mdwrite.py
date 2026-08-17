"""Markdown table writing, shared by every generated document.

One implementation because the escaping is not cosmetic. Every cell here carries text read
out of somebody's repository: a C++ signature holds `|` for bitwise-or, a doc comment holds
newlines, and either one silently rewrites the table around it. That was found once in the
structural wiki page and fixed there; a second copy in the documentation generator would
have been the same defect waiting to be found again.

Deliberately small. It knows about cells, rows and the "a table with no rows is nothing"
rule, and nothing about what any particular document contains.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def _flat(text: object) -> str:
    """``text`` as one line, with no escaping. The shared half of `cell` and `code`."""
    return " ".join(str(text if text is not None else "").split())


def cell(text: object) -> str:
    """One table cell, with the two characters that would break the row neutralised.

    A pipe splits a row and a newline ends it. Both arrive here from source, so escaping is
    what keeps a symbol's own text from rewriting the table it appears in.
    """
    return _flat(text).replace("|", "\\|")


def code(text: object) -> str:
    """Inline code, or empty when there is nothing to render.

    Empty rather than a pair of bare backticks: ``````` in a cell reads as a value that is
    the empty string, which is a claim, where a blank cell reads as "not recorded".

    Deliberately does NOT escape. `table` escapes every cell it is given, so a `code` value
    passed into a row would otherwise be escaped twice -- a C++ `operator|` overload rendered
    as `operator\\|`, with the backslash visible to the reader. Escaping exactly once, in the
    one place that knows a value is becoming a table row, is what keeps that from recurring.
    Outside a table a pipe needs no escaping, so nothing is lost.
    """
    got = _flat(text)
    return f"`{got}`" if got else ""


def table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    """A Markdown table as lines, or NO lines at all when there are no rows.

    Returning nothing rather than a header with an empty body is what lets a caller treat
    "this section rendered nothing" as "this section is empty". A lone header row still
    renders as a table, so it would make every section permanently non-empty.
    """
    body = [list(r) for r in rows]
    if not body:
        return []
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in body]
    return out
