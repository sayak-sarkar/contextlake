"""C4-style namespace boundary model over the fleet's repo graph.

Buckets every included repo into a namespace boundary (by path-prefix depth --
similar to, but not the same heuristic as, ``derive_groups``'s dashboard domain
grid; see ``_c4_namespaces`` for why boundary tagging needs its own rule), then
aggregates the real repo-to-repo edges (``cross_repo_edges``: dependency / HTTP
flow / event flow) into one edge per ``(src, dst, flavor)``, tagging each as
``internal`` (both endpoints share a namespace) or ``boundary`` (they don't).

This is pure data assembly, no rendering. The DOT/cytoscape renderers and the
CLI wiring that consume ``C4Model`` are separate, later pieces of work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .arch.resolve import repo_external_system_edges
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
class C4System:
    """An external system box (the C1 layer): a raw host an indexed repo calls
    over HTTP that never resolves to any indexed repo's exposed route. See
    :func:`.arch.resolve.repo_external_system_edges` -- deliberately
    unclassified, a third-party dependency and an unindexed internal service
    look identical here."""
    system_id: str
    label: str


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
    systems: list[C4System] = field(default_factory=list)


def _c4_namespaces(repo_ids: list[str], depth: int) -> dict[str, list[str]]:
    """Bucket ``repo_ids`` into c4 namespace boundaries by path-prefix depth.

    Deliberately its own logic, not a reuse of ``derive_groups`` (the
    dashboard's display-grouping heuristic): that function's shared
    ``"(ungrouped)"`` bucket is fine for a dashboard grid (a graceful catch-all
    for many unrelated small repos) but wrong for *boundary tagging*, where two
    only-coincidentally-ungrouped repos must never be treated as sharing a
    namespace. Here, a repo's own path prefix (or its own full id, when that id
    has fewer than ``depth`` segments) becomes its namespace -- so a repo with
    no namespace beyond ``depth`` gets a namespace of exactly itself (any edge
    touching it is always a boundary edge), and a repo that IS the exact
    namespace another repo sits under (e.g. a repo literally named ``acme``
    alongside ``acme/sensors/api``) correctly joins that namespace instead of a
    meaningless shared catch-all.
    """
    depth = max(1, int(depth))
    groups: dict[str, list[str]] = {}
    for r in repo_ids:
        key = "/".join(r.split("/")[:depth])
        groups.setdefault(key, []).append(r)
    return groups


def _sys_node_id(host: str) -> str:
    """Model-level id for a C1 external-system box.

    Prefixed with ``sys:`` (mirroring ``_ns_node_id``'s ``ns:`` prefix for
    boundary nodes) so a host can never collide with a real repo id -- and
    pre-sanitized here, matching ``C4Container.repo_id``'s eager
    ``sanitize_label`` (unlike ordinary ``C4Edge``s, which stay raw until a
    renderer sanitizes both endpoints together; a system edge's ``dst`` is
    built from this same helper, so it already matches by construction).
    """
    return sanitize_label(f"sys:{host}")


def c4_model(store, *, group_depth: int = 1, repos: list[str] | None = None,
            c1: bool = False) -> C4Model:
    """Build a namespace-boundary C4 model over ``store``.

    ``repos``, if given, is the pre-filtered repo-id list to include (otherwise
    every ``store.list_repos()``). Repos are bucketed into boundaries by
    path-prefix depth (see ``_c4_namespaces``). ``cross_repo_edges(store)`` is
    then filtered to edges whose both endpoints are in the included repo set,
    collapsed by ``(src, dst, flavor)`` (summing ``weight``; confidence is
    always ``"INFERRED"`` today), and tagged ``boundary=True`` when its two
    endpoints resolve to different namespaces.

    ``c1=True`` adds the C1 layer: one :class:`C4System` box per distinct host
    an included repo calls that never resolves to any indexed repo's exposed
    route (:func:`.arch.resolve.repo_external_system_edges`), plus a
    ``flavor="external"`` edge from the calling repo to it. Always
    ``boundary=True`` -- a system, by definition, is outside every namespace.
    Off by default: this is additive data on top of the C2 model above, not a
    separate view, so existing callers get identical output unless they opt in.
    """
    repo_ids = repos if repos is not None else [r.id for r in store.list_repos()]
    groups = _c4_namespaces(repo_ids, group_depth)

    namespace_of: dict[str, str] = {}
    boundaries: list[C4Boundary] = []
    for key, repo_group in sorted(groups.items()):
        namespace = sanitize_label(key)
        containers = [C4Container(repo_id=sanitize_label(rid), label=rid, namespace=namespace)
                      for rid in sorted(repo_group)]
        boundaries.append(C4Boundary(namespace=namespace, label=namespace,
                                      containers=containers))
        for rid in repo_group:
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

    systems: list[C4System] = []
    if c1:
        systems_by_id: dict[str, C4System] = {}
        for e in repo_external_system_edges(store):
            if e["src"] not in included:
                continue
            sys_id = _sys_node_id(e["system"])
            systems_by_id.setdefault(sys_id, C4System(system_id=sys_id, label=e["system"]))
            edges.append(C4Edge(src=e["src"], dst=sys_id, flavor="external",
                                weight=int(e["weight"]), confidence="INFERRED", boundary=True))
        systems = sorted(systems_by_id.values(), key=lambda s: s.label)

    container_count = sum(len(b.containers) for b in boundaries)
    meta = {
        "group_depth": group_depth,
        "container_count": container_count,
        "boundary_count": len(boundaries),
        "edge_count": len(edges),
        "system_count": len(systems),
    }
    return C4Model(boundaries=boundaries, edges=edges, meta=meta, systems=systems)


# ---------------------------------------------------------------------------
# DOT rendering
# ---------------------------------------------------------------------------
_DOT_UNSAFE = re.compile(r"[^0-9A-Za-z_]")


def _dot_id(raw: str) -> str:
    """Turn a repo id or namespace (e.g. ``acme/sensors/api``) into a DOT-safe node
    or subgraph name.

    Every character outside ``[0-9A-Za-z_]`` (``/``, ``.``, ``-``, etc.) becomes
    ``_``. This is not collision-proof in the abstract: two distinct ids collide
    only if an intra-segment ``-``/``.`` in one id lands where another id has an
    inter-segment ``/`` (e.g. ``acme/sensors-web`` and ``acme/sensors/web`` both
    sanitize to ``acme_sensors_web``). GitLab namespace paths do not form ids that way in
    practice, so this does not occur in the fleet. The full (unsanitized) path
    is kept as the DOT ``label``, so even in a hypothetical collision the
    rendered text stays readable; only the internal node identity would be
    shared.
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

    # C1 systems draw OUTSIDE every cluster (never inside a subgraph), dashed to
    # read as "unclassified" -- could be a real third party or just an
    # unindexed internal service, see C4System's docstring.
    for system in sorted(model.systems, key=lambda s: s.system_id):
        node_id = _dot_id(system.system_id)
        label = _dot_escape(system.label)
        lines.append(f'  {node_id} [label="{label}", style=dashed];')

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
    through ``sanitize_label`` inside ``c4_model``, but
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
    for boundary in model.boundaries:
        ns_id = _ns_node_id(boundary.namespace)
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

    # C1 systems sit outside every namespace compound (parent=None), same as a
    # namespace node itself -- they're not a repo any more than a namespace is.
    for system in model.systems:
        nodes.append({
            "id": system.system_id, "repo": None, "kind": "system",
            "name": system.label, "qualified_name": None, "file": None,
            "line": None, "lang": None, "signature": None, "parent": None,
        })

    edges: list[dict] = []
    for edge in model.edges:
        edges.append({
            "src": sanitize_label(edge.src), "dst": sanitize_label(edge.dst),
            "relation": "flow", "confidence": edge.confidence, "context": edge.flavor,
            "weight": edge.weight, "prov_file": None, "prov_line": None, "verified_at": None,
        })

    return to_payload(nodes, edges, dict(model.meta))
