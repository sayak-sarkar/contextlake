"""SQL DDL extraction -> a referential (foreign-key) graph.

The fleet's SQL is dialect-heavy (T-SQL/PL-SQL) and defeats a tree-sitter AST
(measured: most files produce ERROR nodes exactly where DDL lives). So this is a
regex extractor targeting the high-value defs -- CREATE TABLE / VIEW / PROCEDURE --
and foreign-key ``REFERENCES`` clauses, mirroring the dependency-free style of
:mod:`.manifest` and :mod:`.flow.http`. Every edge is ``INFERRED`` (regex, a likely
undercount, never asserted as ground truth). Object names are normalized (brackets
and a schema qualifier stripped, casefolded) so an FK reference and its target
table -- possibly in another file -- land on the same node.
"""

from __future__ import annotations

import re
from datetime import date

from .ids import make_id
from .model import Node

# One object-name token: optional [ ], optional schema. qualifier, bare identifier.
_NAME = r"(?:\[?[A-Za-z_]\w*\]?\.)?\[?([A-Za-z_]\w*)\]?"
# T-SQL writes `CREATE OR ALTER`; Oracle writes `CREATE OR REPLACE`. Both, everywhere a
# redefinition is legal: the previous version accepted only the T-SQL spelling, so every
# `CREATE OR REPLACE PROCEDURE` in an Oracle tree produced no node at all.
_OR_REDEF = r"(?:OR\s+(?:ALTER|REPLACE)\s+)?"
# Oracle marks a definition it may not have a body for; it changes nothing that is extracted.
_EDITIONABLE = r"(?:(?:NON)?EDITIONABLE\s+)?"
_HEAD = r"\bCREATE\s+" + _OR_REDEF + _EDITIONABLE
_CREATE_TABLE = re.compile(r"\bCREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?TABLE\s+" + _NAME, re.I)
_CREATE_VIEW = re.compile(_HEAD + r"(?:MATERIALIZED\s+)?VIEW\s+" + _NAME, re.I)
_CREATE_PROC = re.compile(_HEAD + r"PROC(?:EDURE)?\s+" + _NAME, re.I)
_REFERENCES = re.compile(r"\bREFERENCES\s+" + _NAME, re.I)

# --- PL/SQL -----------------------------------------------------------------------
#
# A package BODY must be matched before a package spec, and separately: a pattern that
# reads `PACKAGE\s+<name>` takes "BODY" as the name of a package that does not exist, and
# the real one is then never recorded. Two expressions, body first, and the spec pattern
# excludes the keyword explicitly rather than relying on match order alone.
_CREATE_PKG_BODY = re.compile(_HEAD + r"PACKAGE\s+BODY\s+" + _NAME, re.I)
_CREATE_PKG = re.compile(_HEAD + r"PACKAGE\s+(?!BODY\b)" + _NAME, re.I)
_CREATE_FUNC = re.compile(_HEAD + r"FUNCTION\s+" + _NAME, re.I)
_CREATE_TYPE = re.compile(_HEAD + r"TYPE\s+(?!BODY\b)" + _NAME, re.I)
_CREATE_TRIGGER = re.compile(_HEAD + r"TRIGGER\s+" + _NAME, re.I)
# The table a trigger fires on. `ON` also introduces a join, so this is only read inside a
# trigger's own scope, never across the file.
_TRIGGER_ON = re.compile(
    r"\b(?:BEFORE|AFTER|INSTEAD\s+OF)\b.*?\bON\s+" + _NAME, re.I | re.S)
# Any top-level statement boundary that ends a CREATE TABLE scope.
# Every statement that ends the previous definition's scope. The PL/SQL keywords are here
# for the same reason the others are: without them a CREATE TABLE's foreign-key scope runs
# on through an unrelated package body and attributes its REFERENCES to the wrong table.
_SCOPE_END = re.compile(
    r"\bCREATE\s+(?:OR\s+(?:ALTER|REPLACE)\s+)?(?:(?:NON)?EDITIONABLE\s+)?"
    r"(?:GLOBAL\s+TEMPORARY\s+)?(?:MATERIALIZED\s+)?"
    r"(?:TABLE|VIEW|PROC|PROCEDURE|FUNCTION|PACKAGE|TYPE|TRIGGER)\b"
    r"|\bALTER\s+TABLE\b|^\s*GO\s*$|^\s*/\s*$",
    re.I | re.M)


#: Every node kind this module can emit, as data rather than as a list somebody maintains.
#: The registry-parity test used to carry a hand-written `{"table", "view", "procedure"}`
#: for this producer, so adding a kind here left that test asserting a stale set: it
#: reported the new kinds as "registered but produced by nothing" while they were being
#: produced on every run. A set the code and the test both read cannot drift.
EMITTED_KINDS = frozenset({
    "table", "view", "procedure", "function", "typedef", "db_package", "trigger",
})


def _norm_name(raw: str) -> str:
    """A SQL object name normalized for matching: bare identifier, casefolded."""
    return raw.strip().strip("[]").casefold()


# Where a comment can begin -- and the one thing that can make those two tokens
# not a comment at all, a single-quoted literal.
_MASK_SCAN = re.compile(r"--|/\*|'")


def _mask_comments(text: str) -> str:
    """Blank out ``--`` line comments and ``/* */`` block comments, in place.

    A comment is not a schema. Commented-out DDL is exactly the kind of history
    line a long-lived script accumulates, and matching inside one invented
    foreign keys the database does not have -- measured on a fixture whose
    ``-- region_id INT NULL REFERENCES regions(region_id),`` produced a real
    ``orders -> regions`` edge, complete with the comment's own line number as
    its provenance.

    Every masked character is replaced by a space and **newlines are kept**, so
    the result is the same length with the same line breaks: offsets and
    ``_line_of`` results are identical to the raw text, and nothing downstream
    has to re-derive a position.

    Single-quoted literals are stepped over rather than scanned, so a ``--`` or
    ``/*`` inside one is not mistaken for the start of a comment -- masking from
    there would swallow the rest of a live statement and silently lose real
    tables, which is a worse failure than the false positive this fixes. ``''``
    is the SQL escape for a quote and stays inside the literal. Double-quoted
    and ``[bracketed]`` identifiers are deliberately not tracked: an identifier
    containing a comment marker is pathological, and treating ``"`` as a string
    delimiter would mis-scan the dialects that use it for identifiers.
    """
    out = list(text)
    n = len(text)
    i = 0
    while (m := _MASK_SCAN.search(text, i)) is not None:
        start = m.start()
        if m.group(0) == "'":
            j = start + 1
            while j < n:
                if text[j] != "'":
                    j += 1
                elif j + 1 < n and text[j + 1] == "'":
                    j += 2      # an escaped quote, still inside the literal
                else:
                    j += 1      # the closing quote
                    break
            i = j
            continue
        if m.group(0) == "--":
            end = text.find("\n", start)
            end = n if end < 0 else end
        else:
            end = text.find("*/", start + 2)
            # An unterminated block comment runs to end of file, which is what
            # every SQL engine does with one too.
            end = n if end < 0 else end + 2
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "
        i = end
    return "".join(out)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def parse_sql(
    repo_id: str, rel_path: str, source: bytes, verified_at: date | None = None
) -> tuple[list[Node], list[tuple[str, str, str, int]]]:
    """Parse one SQL or PL/SQL file into (def nodes, unresolved reference tuples).

    Covers `.sql` and the PL/SQL source extensions (`.pks`, `.pkb`, `.plb`, `.prc`,
    `.fnc`, `.trg`), which carry one object each by convention and were previously read
    by nothing at all.

    ``verified_at`` is accepted for signature parity with the other parsers; SQL
    nodes carry structural provenance (file/line) and resolved edges are stamped at
    resolution time, so it is unused here.
    """
    raw = source.decode("utf-8", "replace") if isinstance(source, (bytes, bytearray)) else source
    # Every match below runs against the masked copy, defs included: a CREATE
    # inside a comment must neither mint a node nor act as the scope boundary
    # that cuts a live table's FK scope short.
    text = _mask_comments(raw)
    nodes: list[Node] = []
    refs: list[tuple[str, str, str, int]] = []

    def _emit(rx, kind):
        for m in rx.finditer(text):
            name = _norm_name(m.group(1))
            if not name:
                continue
            nid = make_id(repo_id, rel_path, kind, name)
            nodes.append(Node(
                id=nid, repo=repo_id, kind=kind, name=name,
                qualified_name=f"{rel_path}::{name}", file=rel_path,
                line_start=_line_of(text, m.start()), lang="sql"))

    _emit(_CREATE_VIEW, "view")
    _emit(_CREATE_PROC, "procedure")
    # PL/SQL. The spec pattern excludes BODY with a lookahead, so this order is readability
    # rather than a guard: swapping these two lines changes nothing, which a break-test
    # confirmed. The lookahead is what does the work.
    _emit(_CREATE_PKG_BODY, "db_package")
    _emit(_CREATE_PKG, "db_package")
    _emit(_CREATE_FUNC, "function")
    _emit(_CREATE_TYPE, "typedef")

    # Triggers, and the table each one fires on. The `ON` is read only inside the trigger's
    # own scope: `ON` introduces a join everywhere else in SQL, and scanning the whole file
    # for it would attribute half the joins in a script to whichever trigger came first.
    for m in _CREATE_TRIGGER.finditer(text):
        name = _norm_name(m.group(1))
        if not name:
            continue
        nid = make_id(repo_id, rel_path, "trigger", name)
        nodes.append(Node(
            id=nid, repo=repo_id, kind="trigger", name=name,
            qualified_name=f"{rel_path}::{name}", file=rel_path,
            line_start=_line_of(text, m.start()), lang="sql"))
        scope_end = _SCOPE_END.search(text, m.end())
        end = scope_end.start() if scope_end else len(text)
        on = _TRIGGER_ON.search(text, m.end(), end)
        if on:
            target = _norm_name(on.group(1))
            if target and target != name:
                refs.append((nid, target, rel_path, _line_of(text, on.start())))

    # Tables + FK attribution: each CREATE TABLE owns the text up to the next
    # top-level CREATE / GO, and every REFERENCES in that scope is its FK.
    for m in _CREATE_TABLE.finditer(text):
        name = _norm_name(m.group(1))
        if not name:
            continue
        nid = make_id(repo_id, rel_path, "table", name)
        nodes.append(Node(
            id=nid, repo=repo_id, kind="table", name=name,
            qualified_name=f"{rel_path}::{name}", file=rel_path,
            line_start=_line_of(text, m.start()), lang="sql"))
        scope_end = _SCOPE_END.search(text, m.end())
        end = scope_end.start() if scope_end else len(text)
        for r in _REFERENCES.finditer(text, m.end(), end):
            target = _norm_name(r.group(1))
            if target and target != name:
                refs.append((nid, target, rel_path, _line_of(text, r.start())))

    return nodes, refs
