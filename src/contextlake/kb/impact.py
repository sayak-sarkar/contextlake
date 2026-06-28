"""Bounded reverse-reachability ("blast radius") over the code/dependency graph:
what could break if a given node changes.

Shared by the ``impact`` CLI verb and the ``blast_radius`` MCP tool so both walk the
graph identically. Returns raw hits (ids/names unsanitised); the MCP boundary applies
``sanitize_label`` itself, while the CLI prints locally.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RELATIONS = ("calls", "depends_on")
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
    (default ``calls`` + ``depends_on``). Returns ``(hits, truncated)``; ``truncated``
    is True when the cap was reached (so the slice is bounded, never exhaustive).
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
