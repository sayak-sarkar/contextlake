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
_CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\s+" + _NAME, re.I)
_CREATE_VIEW = re.compile(r"\bCREATE\s+(?:OR\s+ALTER\s+)?VIEW\s+" + _NAME, re.I)
_CREATE_PROC = re.compile(r"\bCREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\s+" + _NAME, re.I)
_REFERENCES = re.compile(r"\bREFERENCES\s+" + _NAME, re.I)
# Any top-level statement boundary that ends a CREATE TABLE scope.
_SCOPE_END = re.compile(
    r"\bCREATE\s+(?:OR\s+ALTER\s+)?(?:TABLE|VIEW|PROC|PROCEDURE|FUNCTION)\b|\bALTER\s+TABLE\b|^\s*GO\s*$",
    re.I | re.M)


def _norm_name(raw: str) -> str:
    """A SQL object name normalized for matching: bare identifier, casefolded."""
    return raw.strip().strip("[]").casefold()


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def parse_sql(
    repo_id: str, rel_path: str, source: bytes, verified_at: date | None = None
) -> tuple[list[Node], list[tuple[str, str, str, int]]]:
    """Parse one ``.sql`` file into (DDL def nodes, unresolved FK ref tuples).

    ``verified_at`` is accepted for signature parity with the other parsers; SQL
    nodes carry structural provenance (file/line) and resolved edges are stamped at
    resolution time, so it is unused here.
    """
    text = source.decode("utf-8", "replace") if isinstance(source, (bytes, bytearray)) else source
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
