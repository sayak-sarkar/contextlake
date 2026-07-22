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

import re
from dataclasses import dataclass, field

from .dashboard.data import derive_groups
from .security import sanitize_label
from .visualize import _CONF_DOT, _dot_escape, to_payload
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


# ---------------------------------------------------------------------------
# DOT rendering
# ---------------------------------------------------------------------------
_DOT_UNSAFE = re.compile(r"[^0-9A-Za-z_]")


def _dot_id(raw: str) -> str:
    """Turn a repo id or namespace (e.g. ``acme/pay/api``) into a DOT-safe node
    or subgraph name.

    Every character outside ``[0-9A-Za-z_]`` (``/``, ``.``, ``-``, etc.) becomes
    ``_``. This is not collision-proof in the abstract (``acme/pay`` and
    ``acme.pay`` would both sanitize to ``acme_pay``), but real repo ids in this
    fleet are GitLab namespace paths where ``/`` is the only separator actually
    used between path segments, so collisions do not occur in practice. The
    full (unsanitized) path is kept as the DOT ``label``, so even in a
    hypothetical collision the rendered text stays readable; only the internal
    node identity would be shared.
    """
    return _DOT_UNSAFE.sub("_", raw)


def to_c4_dot(model: C4Model) -> str:
    """Render ``model`` as a Graphviz ``digraph`` with one cluster subgraph per
    namespace boundary.

    Output is fully deterministic: boundaries are sorted by namespace,
    containers within a boundary by repo_id, and edges by (src, dst, flavor) --
    so calling this twice on the same model always yields identical text.
    """
    lines = ["digraph c4 {", "  rankdir=LR;",
             '  node [shape=box, fontname="sans-serif"];']

    for boundary in sorted(model.boundaries, key=lambda b: b.namespace):
        cluster_id = f"cluster_{_dot_id(boundary.namespace)}"
        lines.append(f"  subgraph {cluster_id} {{")
        lines.append(f'    label="{_dot_escape(boundary.label)}";')
        for container in sorted(boundary.containers, key=lambda c: c.repo_id):
            node_id = _dot_id(container.repo_id)
            label = _dot_escape(container.label)
            lines.append(f'    {node_id} [label="{label}"];')
        lines.append("  }")

    for edge in sorted(model.edges, key=lambda e: (e.src, e.dst, e.flavor)):
        src_id = _dot_id(edge.src)
        dst_id = _dot_id(edge.dst)
        edge_label = _dot_escape(f"{edge.flavor} x{edge.weight}")
        style = _CONF_DOT.get(edge.confidence, "solid")
        lines.append(f'  {src_id} -> {dst_id} [label="{edge_label}", style={style}];')

    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cytoscape payload (compound-node) rendering
# ---------------------------------------------------------------------------
def _ns_node_id(namespace: str) -> str:
    """Node id for a boundary's compound parent node.

    Prefixed with ``ns:`` so a namespace can never collide with a repo id
    node (repo ids are GitLab paths and never contain ``:``).
    """
    return f"ns:{namespace}"


def c4_payload(model: C4Model) -> dict:
    """Bridge ``model`` into a ``to_payload``-compatible dict for the cytoscape
    HTML renderer, with namespace boundaries as compound parent nodes.

    Each ``C4Boundary`` becomes a parent node (``kind="namespace"``); each
    ``C4Container`` becomes a ``kind="repo"`` node carrying ``parent`` set to
    its boundary's node id, so cytoscape draws it nested inside the boundary.

    Node/edge id canonicalization: ``C4Container.repo_id`` already went
    through ``sanitize_label`` inside ``derive_groups`` (``c4_model``), but
    ``C4Edge.src``/``dst`` come straight from ``cross_repo_edges`` and are
    raw/unsanitized. Cytoscape joins an edge to its endpoints by exact string
    match on node id, so a raw edge endpoint that differs from its sanitized
    node id would silently fail to attach (no error, just a missing edge).
    To guarantee the join, both the container node id and the edge
    src/dst are run through the same ``sanitize_label`` transform here. This
    is a no-op for ordinary ASCII repo ids (the common case today) and only
    changes behavior for repo ids containing control characters.
    """
    nodes: list[dict] = []
    ns_id_of: dict[str, str] = {}
    for boundary in model.boundaries:
        ns_id = _ns_node_id(boundary.namespace)
        ns_id_of[boundary.namespace] = ns_id
        nodes.append({
            "id": ns_id, "repo": None, "kind": "namespace", "name": boundary.label,
            "qualified_name": None, "file": None, "line": None, "lang": None,
            "signature": None, "parent": None,
        })
        for container in boundary.containers:
            container_id = sanitize_label(container.repo_id)
            nodes.append({
                "id": container_id, "repo": container_id, "kind": "repo",
                "name": container.label, "qualified_name": None, "file": None,
                "line": None, "lang": None, "signature": None, "parent": ns_id,
            })

    edges: list[dict] = []
    for edge in model.edges:
        edges.append({
            "src": sanitize_label(edge.src), "dst": sanitize_label(edge.dst),
            "relation": "flow", "confidence": edge.confidence, "context": edge.flavor,
            "weight": edge.weight, "prov_file": None, "prov_line": None, "verified_at": None,
        })

    return to_payload(nodes, edges, dict(model.meta))
