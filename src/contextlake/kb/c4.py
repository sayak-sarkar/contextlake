"""C4-style namespace boundary model over the fleet's repo graph.

Buckets every included repo into a namespace boundary (by path-prefix depth, the
same heuristic ``derive_groups`` uses for the dashboard's domain grid), then
aggregates the real repo-to-repo edges (``cross_repo_edges``: dependency / HTTP
flow / event flow) into one edge per ``(src, dst, flavor)``, tagging each as
``internal`` (both endpoints share a namespace) or ``boundary`` (they don't).

This is pure data assembly, no rendering. The DOT/cytoscape renderers and the
CLI wiring that consume ``C4Model`` are separate, later pieces of work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dashboard.data import derive_groups
from .wiki.cluster import cross_repo_edges


@dataclass
class C4Container:
    repo_id: str
    label: str
    namespace: str


@dataclass
class C4Boundary:
    namespace: str
    label: str
    containers: list[C4Container] = field(default_factory=list)


@dataclass
class C4Edge:
    src: str
    dst: str
    flavor: str
    weight: int
    confidence: str
    boundary: bool


@dataclass
class C4Model:
    boundaries: list[C4Boundary]
    edges: list[C4Edge]
    meta: dict


def c4_model(store, *, group_depth: int = 1, repos: list[str] | None = None) -> C4Model:
    """Build a namespace-boundary C4 model over ``store``.

    ``repos``, if given, is the pre-filtered repo-id list to include (otherwise
    every ``store.list_repos()``). Repos are bucketed into boundaries via
    ``derive_groups`` at ``group_depth`` (``"(ungrouped)"`` is a valid boundary
    for repos shallower than the depth). ``cross_repo_edges(store)`` is then
    filtered to edges whose both endpoints are in the included repo set,
    collapsed by ``(src, dst, flavor)`` (summing ``weight``; confidence is
    always ``"INFERRED"`` today), and tagged ``boundary=True`` when its two
    endpoints resolve to different namespaces.
    """
    repo_ids = repos if repos is not None else [r.id for r in store.list_repos()]
    groups = derive_groups(repo_ids, group_depth)

    namespace_of: dict[str, str] = {}
    boundaries: list[C4Boundary] = []
    for g in groups:
        namespace = g["group"]
        containers = [C4Container(repo_id=rid, label=rid, namespace=namespace)
                      for rid in g["repos"]]
        boundaries.append(C4Boundary(namespace=namespace, label=namespace,
                                      containers=containers))
        for rid in g["repos"]:
            namespace_of[rid] = namespace

    included = set(namespace_of)
    collapsed: dict[tuple[str, str, str], int] = {}
    for e in cross_repo_edges(store):
        src, dst, flavor = e["src"], e["dst"], e["flavor"]
        if src not in included or dst not in included:
            continue
        key = (src, dst, flavor)
        collapsed[key] = collapsed.get(key, 0) + int(e["weight"])

    edges = [
        C4Edge(src=src, dst=dst, flavor=flavor, weight=weight, confidence="INFERRED",
               boundary=namespace_of[src] != namespace_of[dst])
        for (src, dst, flavor), weight in collapsed.items()
    ]

    container_count = sum(len(b.containers) for b in boundaries)
    meta = {
        "group_depth": group_depth,
        "container_count": container_count,
        "boundary_count": len(boundaries),
        "edge_count": len(edges),
    }
    return C4Model(boundaries=boundaries, edges=edges, meta=meta)
