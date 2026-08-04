"""Graph-data assembly: seed resolution, bounded subgraph extraction, the canonical payload."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...logging_setup import log
from .._util import _or_default

if TYPE_CHECKING:  # avoid importing the model at call time; we only need types here
    from ..model import Edge, Node
    from ..store.base import Store

def _is_sentinel_repo(repo_id: str) -> bool:
    """A pseudo-repo id (``kb.model.SHARED_REPO`` and family) -- never a real
    repo, so must never appear as a node in a fleet-wide repo listing or get a
    per-repo page. Checked by the shared ``(``-prefix convention rather than
    importing ``kb.model`` (this module intentionally avoids the pydantic
    import at load time); ``dashboard.js`` makes the same check independently.
    """
    return repo_id.startswith("(")


def repo_node_sizes(store: Store) -> dict[str, int]:
    """``{repo_id: node_count}`` for real repos only -- a shared/packages/external
    sentinel node (e.g. every module imported fleet-wide) is not a repo and must
    not be ranked, listed, or given a page as though it were one. Public: shared
    with ``kb/dashboard/server.py``, which reuses this exact query for the
    dashboard's embedded graph pages."""
    sizes = dict(store.conn.execute(
        "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
    return {r: c for r, c in sizes.items() if not _is_sentinel_repo(r)}

# ---------------------------------------------------------------------------
# Styling vocab (kind -> colour, confidence -> line style). Generic, no private data.
# ---------------------------------------------------------------------------


def _node_dict(n) -> dict:
    if isinstance(n, dict):
        return n
    return {"id": n.id, "repo": n.repo, "kind": n.kind, "name": n.name,
            "qualified_name": n.qualified_name, "file": n.file, "line": n.line_start,
            "lang": n.lang, "signature": (getattr(n, "attrs", None) or {}).get("signature")}


def _edge_dict(e) -> dict:
    if isinstance(e, dict):
        return e
    conf = e.confidence.value if hasattr(e.confidence, "value") else str(e.confidence)
    prov = getattr(e, "provenance", None)
    # verified_at is a datetime.date — must serialize to a string or json.dumps throws.
    verified = getattr(prov, "verified_at", None)
    return {"src": e.src, "dst": e.dst, "relation": e.relation, "confidence": conf,
            "context": e.context, "weight": e.weight,
            "prov_file": getattr(prov, "source_file", None),
            "prov_line": getattr(prov, "source_line", None),
            "verified_at": verified.isoformat() if verified else None}


def to_payload(nodes, edges, meta: dict | None = None) -> dict:
    """Normalize (Node|dict, Edge|dict) lists into the canonical payload."""
    nd = [_node_dict(n) for n in nodes]
    ed = [_edge_dict(e) for e in edges]
    m = dict(meta or {})
    m.setdefault("node_count", len(nd))
    m.setdefault("edge_count", len(ed))
    return {"nodes": nd, "edges": ed, "meta": m}


# ---------------------------------------------------------------------------
# Subgraph builders
# ---------------------------------------------------------------------------


def seed_ids_from_args(store: Store, args) -> list[str]:
    """Resolve --node / --name(+--kind) / --search / positional text into seed ids."""
    node = getattr(args, "node", None)
    if node:
        return [node]
    kind = getattr(args, "kind", None)
    repo = getattr(args, "repo", None)
    name = getattr(args, "name", None)
    limit = _or_default(getattr(args, "limit", None), 20)
    if name:
        nodes = store.nodes_by_name(name, kind=kind, repo=repo)
    else:
        query = getattr(args, "search", None) or " ".join(getattr(args, "args", []) or []).strip()
        if not query:
            return []
        nodes = store.search(query, kind=kind, repo=repo, limit=limit)
    ids = [n.id for n in nodes]
    if len(ids) > limit:
        log(f"  {len(ids)} seed nodes matched; using the first {limit}")
        ids = ids[:limit]
    return ids


def extract_subgraph(store: Store, seed_ids, *, hops: int = 2, max_nodes: int = 500,
                     max_fanout: int = 50, relation: str | None = None,
                     direction: str = "both",
                     meta: dict | None = None) -> tuple[list[Node], list[Edge]]:
    """BFS a bounded subgraph out from ``seed_ids``.

    Caps are enforced while expanding: each ``neighbors()`` result is sliced to
    ``max_fanout`` (a hub node can otherwise return tens of thousands of edges),
    and node growth stops at ``max_nodes``. Edge objects are retained, then the
    result is filtered to the *induced* subgraph (both endpoints kept) so no
    dangling edges reach the renderers. Truncation is logged, never silent.
    """
    seen: set[str] = set()
    nodes: list[Node] = []
    truncated = False

    for sid in seed_ids:
        if sid in seen:
            continue
        node = store.get_node(sid)
        if node is None:
            log(f"  seed {sid!r} not found in the graph, skipping")
            continue
        seen.add(sid)
        nodes.append(node)

    # Walk in seed order, NOT `list(seen)`: iterating a set of strings follows
    # hash order, which PYTHONHASHSEED re-randomises per process, so the same
    # store exported twice expanded its seeds in a different order and wrote a
    # different byte-string. `nodes` is already insertion-ordered.
    frontier = [n.id for n in nodes]
    edges_by_key: dict[tuple, Edge] = {}
    for _hop in range(max(0, hops)):
        if len(seen) >= max_nodes:
            truncated = True
            break
        nxt: list[str] = []
        for nid in frontier:
            nbrs = store.neighbors(nid, relation=relation, direction=direction)
            if len(nbrs) > max_fanout:
                truncated = True
                nbrs = sorted(nbrs, key=lambda e: (e.relation, e.src, e.dst))[:max_fanout]
            for e in nbrs:
                edges_by_key.setdefault((e.src, e.dst, e.relation), e)
                other = e.dst if e.src == nid else e.src
                if other in seen:
                    continue
                if len(seen) >= max_nodes:
                    truncated = True
                    continue
                node = store.get_node(other)
                if node is None:
                    continue
                seen.add(other)
                nodes.append(node)
                nxt.append(other)
        frontier = nxt
        if not frontier:
            break

    edges = [e for e in edges_by_key.values() if e.src in seen and e.dst in seen]
    if truncated:
        log(f"  truncated: reached max_nodes={max_nodes} / max_fanout={max_fanout} "
            f"(the real neighbourhood is larger)")
    if meta is not None:
        # BFS early-stops, so the true size is unknown — flag truncation but DON'T
        # report a total (a number here would be a fabrication).
        meta["truncated"] = truncated
    return nodes, edges


def repo_subgraph(store: Store, repo_id: str, *, max_nodes: int = 500,
                  max_edges: int | None = None, path_prefix: str | None = None,
                  meta: dict | None = None) -> tuple[list[Node], list[Edge]]:
    """One repo's internal graph: its nodes (capped), plus any node exactly one
    hop out via an outbound edge, and the edges among/to them.

    The one-hop widening is what lets a linked GitLab MR, Figma design, Slack
    channel, or wiki page section (all now reachable via a real outbound edge
    from a code node -- see ``link_to_code``/``link_documents_to_symbols``)
    actually show up in an export instead of being silently dropped by the
    old "both endpoints must be in-repo" filter. It is deliberately NOT
    restricted to sentinel/partition repos (``(external)``, ``@wiki:...``) --
    a node in any OTHER repo qualifies too, the same way a genuinely cross-repo
    edge would. The one exception: a neighbor that belongs to THIS SAME repo
    (``node.repo == repo_id``) never counts as "one hop external" -- it was
    either already selected above, or deliberately excluded by ``max_nodes``
    (truncated a dense repo) or ``path_prefix`` (scoped to one module); letting
    it back in one hop later would silently defeat both caps' whole purpose.
    This does not recurse: a one-hop node's own neighbors are never walked, so
    the widening can't cascade into a second hop.

    **Truncation caps:** one-hop external nodes are exempt from ``max_nodes``
    (they're additive, bounded by however many one-hop neighbors exist -- the
    node cap governs the size of the *repo-internal* selection query, which
    runs before any of this). Their edges DO count toward ``max_edges``,
    though: once an edge reaches the page, a Mermaid renderer can't tell an
    internal edge from a one-hop external one, so exempting them would
    reopen the exact ``maxEdges`` render-failure ``max_edges`` exists to
    prevent.

    ``max_edges`` is opt-in (``None`` = no additional edge cap beyond whatever
    ``max_nodes`` induces) -- **not on by default**, because not every consumer
    needs it. A cytoscape ``--format html``/``dot`` view has no edge-count limit
    of its own and used to show every edge among the capped nodes; only the
    Mermaid-rendered formats (``mermaid``/``classdiagram``/``statediagram``/
    ``erdiagram``/``deploymentdiagram``) have a hard ``maxEdges`` (500 by
    default) that a dense repo (heavy ``contains``/``calls`` fan-out, e.g. a
    large C/C++ codebase) can blow past with just 500 nodes -- surfacing as a
    raw render error. Callers rendering through Mermaid should pass
    ``max_edges=400`` (below Mermaid's 500, so the dashboard's belt-and-braces
    ``maxEdges`` bump is genuinely a safety margin, not the load-bearing limit);
    everyone else should leave it ``None``.

    ``path_prefix``, when given, scopes to nodes whose ``file`` is exactly
    ``path_prefix`` or starts with ``path_prefix`` plus a ``/`` (segment-boundary
    match, not a plain string prefix -- ``path_prefix="api"`` must not also match
    a sibling directory like ``apiv2/``; see ``repo_brief``'s identical fix) --
    the "one module at a time" view for repos too large to render meaningfully in
    one slice (see ``repo_modules`` for the prefixes worth offering a caller).
    """
    where = "repo_id=?"
    params: list[object] = [repo_id]
    if path_prefix:
        clean = path_prefix.rstrip("/")
        escaped = clean.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += " AND (file=? OR file LIKE ? ESCAPE '\\')"
        params.append(clean)
        params.append(escaped + "/%")
    deg_params = [repo_id, repo_id]
    rows = store.conn.execute(
        f"""
        SELECT n.node_id FROM nodes n
        LEFT JOIN (
            SELECT src AS node_id, COUNT(*) AS c FROM edges WHERE repo_id=? GROUP BY src
            UNION ALL
            SELECT dst AS node_id, COUNT(*) AS c FROM edges WHERE repo_id=? GROUP BY dst
        ) deg ON deg.node_id = n.node_id
        WHERE {where}
        GROUP BY n.node_id
        ORDER BY COALESCE(SUM(deg.c), 0) DESC, n.node_id ASC
        LIMIT ?
        """,
        (*deg_params, *params, max_nodes + 1),
    ).fetchall()
    node_truncated = len(rows) > max_nodes
    ids = [r[0] for r in rows[:max_nodes]]
    seen = set(ids)
    nodes = [n for nid in ids if (n := store.get_node(nid)) is not None]
    edges: list[Edge] = []
    edge_keys: set[tuple] = set()
    edge_truncated = False
    external_ids: set[str] = set()
    ext_cache: dict[str, Node | None] = {}  # avoids a get_node() per repeated dst
    for nid in ids:
        if max_edges is not None and len(edges) >= max_edges:
            edge_truncated = True
            break
        for e in store.neighbors(nid, direction="out"):
            k = (e.src, e.dst, e.relation)
            if k in edge_keys:
                continue
            is_external = False
            if e.dst not in seen:
                # a neighbor outside this query's own selection -- resolve it
                # BEFORE the max_edges check below, so an edge that turns out
                # to be unqualified is never counted against the cap (that
                # would make the cap dishonest: fewer real edges returned than
                # max_edges implies). Two cases don't qualify as "one hop
                # external": no such node at all (a dangling edge), or a node
                # that belongs to THIS SAME repo -- i.e. one excluded by
                # max_nodes/path_prefix, not a genuinely external link. Letting
                # those back in would defeat both caps' whole purpose.
                if e.dst not in ext_cache:
                    ext_cache[e.dst] = store.get_node(e.dst)
                node = ext_cache[e.dst]
                if node is None or node.repo == repo_id:
                    continue
                is_external = True
            if max_edges is not None and len(edges) >= max_edges:
                edge_truncated = True
                break
            edge_keys.add(k)
            edges.append(e)
            if is_external:
                # e.g. a linked GitLab MR, Figma design, Slack channel, or wiki
                # page section, all reachable via a real outbound edge from a
                # code node. Never walked further (one hop only).
                external_ids.add(e.dst)
    # sorted(), not raw set order: set iteration follows string hash order, which
    # is re-randomised per process, so an unchanged store exported twice produced
    # the same node SET in a different sequence and therefore different bytes --
    # defeating diffing, content-hashing and caching of an export.
    nodes.extend(ext_cache[nid] for nid in sorted(external_ids))  # already resolved, non-None
    truncated = node_truncated or edge_truncated
    if truncated:
        log(f"  truncated: repo {repo_id!r} subgraph exceeds max_nodes={max_nodes}"
            + (f" or max_edges={max_edges}" if max_edges is not None else ""))
    if meta is not None:
        meta["truncated"] = truncated
        if node_truncated:  # cheap exact total only when we actually capped on nodes
            meta["total"] = store.conn.execute(
                f"SELECT COUNT(*) FROM nodes WHERE {where}", tuple(params)).fetchone()[0]
    return nodes, edges


def repo_modules(store: Store, repo_id: str, *, within: str | None = None,
                 min_nodes: int = 5) -> list[dict]:
    """Path segments worth offering as a "scope to one module" choice on a
    repo's Diagrams tab -- computed from each node's ``file``, not a fixed
    depth, so it works for both ``src/foo/...`` layouts and single-top-dir repos.
    Segments with fewer than ``min_nodes`` nodes are dropped (not worth a whole
    tab of its own); the remainder is sorted by node count, largest first, so a
    caller populating a dropdown can offer the modules actually worth scoping to.

    ``within``, when given, scopes to one path segment deeper than the default
    top level: passing the prefix a caller already scoped to (e.g. ``"src"``)
    returns ITS children (``"src/foo"``, ``"src/bar"``, ...) instead of the
    same top-level list again. This is the fix for a module that is itself
    still too large to render in one slice -- e.g. a repo whose entire code
    lives under one top-level ``src/``, where the flat (depth-1) listing offers
    no way to narrow further. Each returned ``prefix`` is a full path (already
    including ``within``), ready to pass straight back in as either the next
    call's ``within`` or as ``repo_subgraph``'s ``path_prefix`` -- both accept
    arbitrary depth unchanged, only this enumerator was ever depth-1-only.
    """
    depth = 0
    where = "repo_id=? AND file IS NOT NULL AND file != ''"
    params: list[object] = [repo_id]
    if within:
        clean = within.strip("/")
        depth = clean.count("/") + 1
        where += " AND file LIKE ? ESCAPE '\\'"
        params.append(clean.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
    rows = store.conn.execute(f"SELECT file FROM nodes WHERE {where}", tuple(params)).fetchall()
    counts: dict[str, int] = {}
    for (file,) in rows:
        parts = file.split("/")
        if len(parts) <= depth:
            continue  # a file directly at this depth, not one level under it
        seg = "/".join(parts[: depth + 1])
        counts[seg] = counts.get(seg, 0) + 1
    return [
        {"prefix": prefix, "nodes": n}
        for prefix, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if n >= min_nodes
    ]


def overview_subgraph(store: Store, *, max_nodes: int = 5000,
                      meta: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Repos-as-nodes with **real** cross-repo dependency edges (the architecture map).

    Edges come from the package two-hop (``publishes ⨝ depends_on``, see
    ``arch.resolve.repo_dependency_edges``) — the only trustworthy cross-repo
    signal. The raw cross-repo ``imports`` join is deliberately NOT used: it is
    dominated by import-star artifacts (global ``module`` nodes shared fleet-wide),
    which would render hundreds of thousands of phantom edges. Dependencies are
    marked ``INFERRED`` (manifest-derived, a likely undercount — not ground truth).
    """
    from ..arch.resolve import repo_dependency_edges, repo_event_flow_edges, repo_http_flow_edges
    sizes = repo_node_sizes(store)
    log("  resolving real cross-repo dependencies (package two-hop)…")
    # structural deps (depends_on) + runtime flow (HTTP + events); all INFERRED,
    # all repo→repo. Flow is empty until an index has run the flow extractors.
    dep_edges = (repo_dependency_edges(store) + repo_http_flow_edges(store)
                 + repo_event_flow_edges(store))

    degree: dict[str, int] = {}
    for e in dep_edges:
        degree[e["src"]] = degree.get(e["src"], 0) + e["weight"]
        degree[e["dst"]] = degree.get(e["dst"], 0) + e["weight"]

    # One node per repo for the WHOLE fleet: the repo registry (list_repos) unioned
    # with any repo that has nodes — so even a repo with no parsed code (no edges) is
    # present and findable. Rank by connectivity then content so that if the fleet
    # exceeds max_nodes the most-connected/biggest win and empty repos drop first —
    # never alphabetically (which would hide heavily-linked hubs that sort late).
    candidates = {r.id for r in store.list_repos()} | set(sizes)
    ranked = sorted(candidates, key=lambda r: (-degree.get(r, 0), -sizes.get(r, 0), r))
    truncated = len(ranked) > max_nodes
    repo_ids = ranked[:max_nodes]
    keep = set(repo_ids)
    # Label with the short repo name (last path segment) so nodes are distinguishable
    # — the full id is a long shared-prefix path that truncates to an identical,
    # useless stub on every node. The full id stays as qualified_name (searchable +
    # shown in the inspector) and as repo.
    # dominant language per repo -> drives the tech-stack lettermark in the overview,
    # so the fleet architecture map reads its stack at a glance (one GROUP BY pass).
    dom_lang: dict[str, str] = {}
    best_lang_count: dict[str, int] = {}
    for repo, lang, cnt in store.conn.execute(
            "SELECT repo_id, lang, COUNT(*) FROM nodes "
            "WHERE lang IS NOT NULL GROUP BY repo_id, lang").fetchall():
        if repo in keep and cnt > best_lang_count.get(repo, 0):
            best_lang_count[repo] = cnt
            dom_lang[repo] = lang
    nodes = [{"id": r, "repo": r, "kind": "repo", "name": r.rsplit("/", 1)[-1],
              "qualified_name": r, "file": None, "line": None, "lang": dom_lang.get(r),
              "attrs": {"node_count": sizes.get(r, 0)}} for r in repo_ids]
    edges = [e for e in dep_edges if e["src"] in keep and e["dst"] in keep]
    if truncated:
        log(f"  {len(ranked)} repos; showing the {max_nodes} most "
            f"connected (raise --max-nodes to see more)")
    if meta is not None:
        meta["truncated"] = truncated
        meta["total"] = len(ranked)  # exact: every repo is a known candidate
    return nodes, edges


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------
