"""Entity state-machine detection (regex/AST-light, mirrors flow/http.py).

Finds guarded assignments to a state-bearing field (``status``/``state``/``stage``,
case-insensitive) within a class: ``if order.status == Created: ... order.status =
Paid``. Only *guarded* transitions are emitted — the source state must be
established by a preceding comparison on the same receiver and field, in the same
method, within a bounded distance of the assignment. Option (a) from the design
spec: fewer, high-confidence edges are more useful than a diagram where most
transitions originate from an unhelpful synthetic "any state" node.

Regex-based and per-language (Python/JS·TS/C#), so every edge is ``INFERRED`` —
the same honesty stance as flow/http.py's endpoint detection: a likely undercount
(an unguarded assignment, or a guard the regex doesn't match, is simply missed),
never a false transition.

The "entity" a state machine belongs to is the nearest enclosing class textually
before the transition — the same proximity heuristic flow/http.py implicitly uses
per-file (no real scope tree). A transition with no enclosing class is dropped:
without an entity there is nothing to group it under.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from datetime import date

from .._lines import LineIndex
from ..ids import make_id
from ..model import Confidence, Edge, Node, Provenance

# language (from LANG_BY_EXT) -> pattern family, same split as flow/http.py
_FAMILY = {"python": "py", "javascript": "js", "typescript": "js",
           "tsx": "js", "csharp": "cs"}

_FIELD = r"(?:status|state|stage)"
# how far (chars) an assignment may trail its guard and still count as the same
# transition -- bounds the match to "same method body" in the common case
# without needing real indentation/brace scope tracking.
_MAX_GAP = 400

_GUARD_ASSIGN: dict[str, re.Pattern] = {
    "py": re.compile(
        rf"if\s+(?P<recv>[\w.]+)\.(?P<field>{_FIELD})\s*(?:==|is)\s*"
        rf"(?P<from>[\w.'\"]+)\s*:"
        rf"(?P<gap>.{{0,{_MAX_GAP}}}?)"
        rf"(?P=recv)\.(?P=field)\s*=\s*(?P<to>[\w.'\"]+)",
        re.DOTALL | re.IGNORECASE,
    ),
    "js": re.compile(
        rf"if\s*\(\s*(?P<recv>[\w.]+)\.(?P<field>{_FIELD})\s*===?\s*"
        rf"(?P<from>[\w.'\"`]+)\s*\)"
        rf"(?P<gap>.{{0,{_MAX_GAP}}}?)"
        rf"(?P=recv)\.(?P=field)\s*=\s*(?P<to>[\w.'\"`]+)",
        re.DOTALL | re.IGNORECASE,
    ),
    "cs": re.compile(
        rf"if\s*\(\s*(?P<recv>[\w.]+)\.(?P<field>{_FIELD})\s*==\s*"
        rf"(?P<from>[\w.'\"]+)\s*\)"
        rf"(?P<gap>.{{0,{_MAX_GAP}}}?)"
        rf"(?P=recv)\.(?P=field)\s*=\s*(?P<to>[\w.'\"]+)\s*;",
        re.DOTALL | re.IGNORECASE,
    ),
}

_CLASS = re.compile(r"\bclass\s+(\w+)")
_METHOD_NAME: dict[str, re.Pattern] = {
    "py": re.compile(r"\bdef\s+(\w+)\s*\("),
    "js": re.compile(
        r"\bfunction\s+(\w+)\s*\("
        r"|\b(?!if\b|for\b|while\b|catch\b|switch\b|else\b)(\w+)\s*\([^()]*\)\s*\{"
    ),
    "cs": re.compile(
        r"\b(?:public|private|protected|internal)\s+(?:static\s+)?(?:async\s+)?"
        r"[\w<>\[\],\s]+?\s+(\w+)\s*\([^)]*\)\s*\{"
    ),
}

# A guard licenses the assignment that follows it only if nothing between them
# suggests the assignment is actually reached under a *different* condition --
# an `else`/`elif` sibling branch, a `}` closing the guard's own block, crossing
# into another method/class, or a second guard/reassignment of the same
# receiver+field (which would actually be the one governing the assignment).
# Any of these means "possibly not really guarded by this if" -- and per this
# module's stated invariant (never a false transition), that must fail closed,
# not emit a transition anyway.
_BOUNDARY: dict[str, re.Pattern] = {
    "py": re.compile(r"\b(?:else|elif|def|class)\b"),
    "js": re.compile(r"}|\bfunction\b|\bclass\b"),
    "cs": re.compile(r"}|\bclass\b|\b(?:public|private|protected|internal)\b"),
}


def _crosses_boundary(fam: str, gap: str, recv: str, field: str) -> bool:
    if _BOUNDARY[fam].search(gap):
        return True
    # a second mention of the same receiver+field in between means some other
    # condition or reassignment -- not our guard -- actually governs it.
    return re.search(rf"{re.escape(recv)}\.{re.escape(field)}\b", gap, re.IGNORECASE) is not None


def _strip_value(raw: str) -> str:
    """``OrderStatus.Paid`` / ``"paid"`` / ``'Paid'`` -> ``Paid``."""
    v = raw.strip().strip("'\"`")
    return v.rsplit(".", 1)[-1]


def _first_group(m: re.Match) -> str | None:
    return next((g for g in m.groups() if g), None)


class _NearestBefore:
    """The nearest ``pattern`` match that ends at or before an offset, from one scan.

    The lookup used to re-run ``pattern.finditer(text, 0, pos)`` for every transition,
    which reads the file from the top each time: (transitions x file size), 8 s on a
    0.8 MB file with 1,000 transitions. One scan of the whole file records where each
    match starts and ends, and a binary search answers every lookup. Matches never
    overlap, so the ends are already sorted. The scan runs on the first lookup, so a file
    with no transition pays nothing.

    One case still reads the old way. A transition's guard has no word boundary before
    its ``if``, so it can begin inside a longer word (``class Midif o.status ...``). A
    match that runs across ``pos`` was cut short by the old ``endpos=pos`` scan, which
    then read the shortened name. That lookup repeats the cut scan, so the answer stays
    the same. A differential run of 47,432 lookups (real files plus seeded random code)
    found 246 such lookups and no other difference.
    """

    __slots__ = ("_pattern", "_text", "_starts", "_ends", "_values")

    def __init__(self, pattern: re.Pattern, text: str) -> None:
        self._pattern = pattern
        self._text = text
        self._ends: list[int] | None = None
        self._starts: list[int] = []
        self._values: list[str | None] = []

    def before(self, pos: int) -> str | None:
        if self._ends is None:
            matches = list(self._pattern.finditer(self._text))
            self._starts = [m.start() for m in matches]
            self._ends = [m.end() for m in matches]
            self._values = [_first_group(m) for m in matches]
        i = bisect_right(self._ends, pos)   # matches 0..i-1 end at or before pos
        if i < len(self._ends) and self._starts[i] < pos:
            last = None
            for m in self._pattern.finditer(self._text, 0, pos):
                last = m
            return _first_group(last) if last else None
        return self._values[i - 1] if i else None


def extract_state_flow(repo_id: str, rel_path: str, source, lang: str,
                       verified_at: date | None = None) -> tuple[list[Node], list[Edge]]:
    """``state`` nodes + ``transitions_to`` edges for one file's entities.

    Each state node also gets a ``contains`` edge from the file, matching how
    the tree-sitter pass links a file to the classes/functions it defines —
    without it, a state node would be an island reachable only from a
    ``--repo``/``--overview`` view, never from a ``--name <EntityClass>`` seed
    (which hops file <-> class <-> state, not class -> state directly, since
    this regex pass never resolves the tree-sitter class node's own id).
    """
    fam = _FAMILY.get(lang)
    if not fam:
        return [], []
    text = source.decode("utf-8", "replace") if isinstance(source, (bytes, bytearray)) else source
    verified_at = verified_at or date.today()
    file_id = make_id(repo_id, rel_path)
    line_of = LineIndex(text).line_of
    nearest_class = _NearestBefore(_CLASS, text)
    nearest_method = _NearestBefore(_METHOD_NAME[fam], text)
    nodes: list[Node] = []
    edges: list[Edge] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    def state_node(entity: str, value: str, line: int) -> str:
        nid = make_id("state", repo_id, entity, value)
        if nid not in seen_nodes:
            seen_nodes.add(nid)
            nodes.append(Node(id=nid, repo=repo_id, kind="state", name=value,
                              qualified_name=f"{entity}.{value}", attrs={"entity": entity}))
            edges.append(Edge(
                src=file_id, dst=nid, relation="contains",
                confidence=Confidence.INFERRED,
                provenance=Provenance(source_file=rel_path, source_line=line,
                                      verified_at=verified_at)))
        return nid

    for m in _GUARD_ASSIGN[fam].finditer(text):
        recv, field = m.group("recv"), m.group("field")
        if _crosses_boundary(fam, m.group("gap"), recv, field):
            continue  # assignment isn't reliably reached under this guard
        entity = nearest_class.before(m.start())
        if not entity:
            continue  # no enclosing class -> nothing to group this transition under
        from_val, to_val = _strip_value(m.group("from")), _strip_value(m.group("to"))
        if from_val == to_val:
            continue  # a guard re-asserting the same value is not a transition
        field_lower = field.lower()
        if from_val.lower() == field_lower or to_val.lower() == field_lower:
            continue  # e.g. `x.status = other.status` -- a field read, not a state literal
        method = nearest_method.before(m.start()) or "?"
        line = line_of(m.start())

        src_id = state_node(entity, from_val, line)
        dst_id = state_node(entity, to_val, line)
        key = (src_id, dst_id, method)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(Edge(
            src=src_id, dst=dst_id, relation="transitions_to",
            confidence=Confidence.INFERRED, context=method,
            provenance=Provenance(source_file=rel_path,
                                  source_line=line,
                                  verified_at=verified_at)))

    return nodes, edges
