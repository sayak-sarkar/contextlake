"""Bounded reverse-reachability ("blast radius") over the code/dependency graph:
what could break if a given node changes.

Shared by the ``impact`` CLI verb and the ``blast_radius`` MCP tool so both walk the
graph identically. Returns raw hits (ids/names unsanitised); the MCP boundary applies
``sanitize_label`` itself, while the CLI prints locally.
"""

from __future__ import annotations

from dataclasses import dataclass

# Reverse-reach over calls (a caller breaks), depends_on (a dependent breaks), and
# inherits (a subclass breaks when its base changes).
DEFAULT_RELATIONS = ("calls", "depends_on", "inherits")
# Walk EXTRACTED edges before INFERRED/AMBIGUOUS so the highest-confidence impact
# surfaces first when the cap is hit.
_CONF_RANK = {"EXTRACTED": 0, "INFERRED": 1, "AMBIGUOUS": 2}


@dataclass
class ImpactHit:
    id: str
    repo: str
    kind: str
    name: str
    hop: int          # distance from the seed (1 = direct caller/dependent)
    via: str          # the relation traversed
    confidence: str   # EXTRACTED | INFERRED | AMBIGUOUS


def blast_radius(store, node_id: str, *, hops: int = 3,
                 relations=None, limit: int = 100) -> tuple[list[ImpactHit], bool]:
    """Breadth-first walk of INCOMING edges (callers / dependents) from ``node_id``.

    Goes up to ``hops`` levels, capped at ``limit`` hits, over ``relations``
    (default ``calls`` + ``depends_on`` + ``inherits``). Returns ``(hits, truncated)``;
    ``truncated`` is True when the cap was reached (a bounded slice, never exhaustive).
    """
    rels = set(relations or DEFAULT_RELATIONS)
    seen = {node_id}
    hits: list[ImpactHit] = []
    frontier = [(node_id, 0)]
    truncated = False
    while frontier and not truncated:
        cur, hop = frontier.pop(0)
        if hop >= hops:
            continue
        incoming = sorted(store.neighbors(cur, direction="in"),
                          key=lambda e: _CONF_RANK.get(e.confidence.value, 9))
        for e in incoming:
            if e.relation not in rels or e.src in seen:
                continue
            if len(hits) >= limit:
                truncated = True
                break
            seen.add(e.src)
            n = store.get_node(e.src)
            if not n:
                continue
            hits.append(ImpactHit(id=n.id, repo=n.repo, kind=n.kind, name=n.name,
                                  hop=hop + 1, via=e.relation, confidence=e.confidence.value))
            frontier.append((e.src, hop + 1))
    return hits, truncated


# Source-symbol kinds worth seeding an impact walk; ranked above files / refs / packages
# so an exact-name match prefers the definition the user most likely meant.
_SOURCE_KINDS = {"class", "interface", "function", "method", "struct", "enum", "type"}


def _rank_candidates(nodes: list) -> list:
    """Order exact-name matches: real source symbols first, non-test files next."""
    def key(n):
        is_source = 0 if n.kind in _SOURCE_KINDS else 1
        is_test = 1 if (n.file and "test" in n.file.lower()) else 0
        return (is_source, is_test, n.id)
    return sorted(nodes, key=key)


def resolve_target(store, target: str, *, repo: str | None = None):
    """Resolve an impact target (a node id OR a bare symbol name) to one seed node.

    Resolution order, each scoped to ``repo`` when given:
      1. exact node id (``get_node`` — ids are globally unique)
      2. exact symbol name (``nodes_by_name``), ranked source-first
      3. fuzzy full-text search (``search``)

    Returns ``(node, candidates)``:
      * ``(node, [])``       — resolved to exactly one node
      * ``(None, [c, …])``   — ambiguous: the name is defined in several repos; the
                                caller lists them and asks the user to narrow with ``repo``
      * ``(None, [])``       — nothing matched

    This fixes the prior ``search(name)[0]`` behaviour, where a common name (``Node``,
    ``Order``) silently resolved to an unrelated repo's highest-ranked FTS hit and
    reported a confidently-wrong blast radius.
    """
    node = store.get_node(target)
    if node is not None and (repo is None or node.repo == repo):
        return node, []

    named = _rank_candidates(store.nodes_by_name(target, repo=repo))
    if named:
        if repo is not None:
            return named[0], []                  # user already narrowed by repo
        if len({n.repo for n in named}) == 1:
            return named[0], []                  # only one repo defines this name
        return None, named                       # ambiguous across repos

    matches = store.search(target, limit=10)
    if repo:
        matches = [m for m in matches if m.repo == repo]
    return (matches[0], []) if matches else (None, [])
