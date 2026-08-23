"""Graph-data assembly: seed resolution, bounded subgraph extraction, the canonical payload."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...logging_setup import log
from .._util import _or_default

if TYPE_CHECKING:  # avoid importing the model at call time; we only need types here
    from ..model import Edge, Node
    from ..store.base import Store

def _is_not_a_real_repo(repo_id: str) -> bool:
    """True for any id that must not appear in a fleet-wide repo listing or get a page.

    NAMED for what it checks, not for one of the two families it excludes. It was called
    `_is_sentinel_repo`, which is the name of the narrower `(`-prefix contract that
    `kb.model.is_sentinel_repo` owns and that `cmds/forget.py` depends on -- so widening
    the check under that name would have quietly changed a shared word's meaning.

    TWO families, and only one of them was checked. The ``(`` prefix covers the
    ``kb.model.SHARED_REPO`` sentinels (``(shared)``, ``(packages)``). The ``@`` prefix
    covers the PARTITIONS written beside a repo -- ``@wiki:<repo>``, ``@connect:<repo>``,
    ``@enrich:<repo>``, ``@ingest:<name>`` -- which own nodes but have no ``repos`` row and
    are not clones. ``store.list_partitions`` documents exactly this split.

    Checking only ``(`` meant the fleet count DOUBLED the first time ``kb wiki`` ran: a
    three-repository store rendered "6 repos with a parsed graph", with `@wiki:*` entries
    indistinguishable from real ones in the list, each linked to its own page. The same
    store's ``kb lint`` and the dashboard's ``data.json`` both said 3, so the correct
    answer existed a few lines away.

    Checked by prefix rather than by importing ``kb.model`` (this module intentionally
    avoids the pydantic import at load time); ``dashboard.js`` makes the same check
    independently.
    """
    return repo_id.startswith("(") or repo_id.startswith("@")


def repo_node_sizes(store: Store) -> dict[str, int]:
    """``{repo_id: node_count}`` for real repos only -- a shared/packages/external
    sentinel node (e.g. every module imported fleet-wide) is not a repo and must
    not be ranked, listed, or given a page as though it were one. Public: shared
    with ``kb/dashboard/server.py``, which reuses this exact query for the
    dashboard's embedded graph pages."""
    sizes = dict(store.conn.execute(
        "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
    return {r: c for r, c in sizes.items() if not _is_not_a_real_repo(r)}

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


def fold_contained_leaves(nd: list[dict], ed: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Collapse structural leaves into a count on their container.

    A node is a leaf here when, **within this payload**, it is the source of no edge and
    the only relation reaching it is ``contains``. Such a node adds a dot and a label to
    the picture and answers no question the container does not already answer: nothing
    calls it, it calls nothing, and its one edge says where it lives.

    Measured on a 663k-node store, five kinds satisfy that -- `config_key`, `macro`,
    `field`, `enum_constant`, `global_variable` -- and they are **74.1% of all nodes**.

    **The rule is structural on purpose, and the kind list above is not in the code.**
    Three ways a hardcoded list would have been wrong. It would encode one fleet's shape:
    `config_key` alone is 55.6% of that store, is 100% XML, and three repositories hold
    71.8% of it. It would keep folding a kind the day a parser learns to emit a real edge
    from it. And it would need `typedef` remembered as an exception -- 22k nodes that are
    also never a source, but which `inherits` edges reach, so folding them would hide a
    type hierarchy. Asking the graph instead gets all three right for free.

    The container keeps the tally, so nothing is hidden silently: a folded container
    carries ``folded`` (how many) and ``folded_kinds`` (a kind -> count map) for the
    renderer to show. A leaf whose container is not in this payload is KEPT -- folding it
    into something absent would drop it from the picture entirely.
    """
    by_id = {n["id"]: n for n in nd if n.get("id")}
    sources = {e.get("src") for e in ed}
    incoming: dict[str, set] = {}
    container: dict[str, str] = {}
    for e in ed:
        dst, rel, src = e.get("dst"), e.get("relation"), e.get("src")
        if dst is None:
            continue
        incoming.setdefault(dst, set()).add(rel)
        if rel == "contains":
            container.setdefault(dst, src)

    foldable = {
        nid for nid in by_id
        if nid not in sources
        and incoming.get(nid) == {"contains"}
        and container.get(nid) in by_id
    }
    if not foldable:
        return nd, ed, {}

    for nid in foldable:
        parent = by_id[container[nid]]
        parent["folded"] = parent.get("folded", 0) + 1
        kinds = parent.setdefault("folded_kinds", {})
        k = by_id[nid].get("kind") or "unknown"
        kinds[k] = kinds.get(k, 0) + 1

    kept_nodes = [n for n in nd if n.get("id") not in foldable]
    kept_edges = [e for e in ed
                  if e.get("src") not in foldable and e.get("dst") not in foldable]
    tally: dict = {}
    for nid in foldable:
        k = by_id[nid].get("kind") or "unknown"
        tally[k] = tally.get(k, 0) + 1
    return kept_nodes, kept_edges, tally


def to_payload(nodes, edges, meta: dict | None = None, *, fold_leaves: bool = False) -> dict:
    """Normalize (Node|dict, Edge|dict) lists into the canonical payload.

    ``fold_leaves`` collapses structural leaves into a count on their container (see
    :func:`fold_contained_leaves`). **Off by default, and the default is the interesting
    part.** Folding is a property of the interactive graph VIEW, which has a readability
    problem, and not of the payload, which several exports consume whole.

    Defaulting it on broke the class diagram, and the way it broke is the argument: a
    `method` reached only by `contains` satisfies the structural leaf rule exactly, and in
    a UML class view the methods ARE the content. `to_json`, `to_dot`, `to_mermaid`,
    `to_class_diagram`, `to_sequence_diagram` and `to_state_diagram` all read this payload
    and all need every node. Only `to_html` opts in.
    """
    nd = [_node_dict(n) for n in nodes]
    ed = [_edge_dict(e) for e in edges]
    m = dict(meta or {})
    # The counts describe what the caller HANDED us, recorded before any folding, so
    # "how big is this subgraph" and "how much is drawn" stay separable. A reader told
    # only the post-fold number would think the graph were smaller than it is.
    m.setdefault("node_count", len(nd))
    m.setdefault("edge_count", len(ed))
    if fold_leaves:
        nd, ed, folded = fold_contained_leaves(nd, ed)
        if folded:
            m["folded_leaves"] = sum(folded.values())
            m["folded_leaf_kinds"] = folded
            m["drawn_node_count"] = len(nd)
    return {"nodes": nd, "edges": ed, "meta": m}


# ---------------------------------------------------------------------------
# Subgraph builders
# ---------------------------------------------------------------------------


def seed_ids_from_args(store: Store, args) -> list[str]:
    """Resolve --node / --name(+--kind) / --search / positional text into seed ids."""
    node = getattr(args, "node", None)
    if node:
        # Checked against the store, like every other seed flag. Returned unchecked, a
        # mistyped id produced an empty graph and exit 0, while the SAME miss reached
        # through --name or --search exits 2 -- one event with two verdicts, so a script
        # gating on the exit code could catch a typo in one flag and not the other.
        return [node] if store.get_node(node) is not None else []
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


# Share of ``max_nodes`` the one-hop external widening may claim when the repo's
# own nodes would otherwise fill the budget. A repo view is primarily the repo's
# own code, so its links (packages, wiki sections, MRs, designs) get a bounded
# minority slice rather than the unlimited additive allowance they used to have.
_EXTERNAL_BUDGET_SHARE = 5

# Share of ``max_nodes`` the per-kind floors below may claim between them. Only a
# kind's actual shortfall is ever taken -- a kind already at or above its floor
# displaces nothing -- so a view that starves no kind is identical to one computed
# with no floors at all. It exists because ranking purely by degree turns out to be
# kind-biased in practice, not just kind-neutral-with-noise: a repo with 412 table
# and 402 Terraform resource nodes rendered 0 of each into its default view while
# keeping 100% of its package nodes, which is also why its ER and deployment
# diagrams came out empty on a repo that plainly has both a schema and infra.
_KIND_FLOOR_SHARE = 2


def _kind_floors(available: dict[str, int], budget: int) -> dict[str, int]:
    """Max-min fair split of ``budget`` across the node kinds present in a view.

    Progressive filling: every kind is offered an equal slice, a kind holding
    fewer nodes than its slice takes only what it has and hands the remainder
    back, and that repeats until nothing more can be given away. So a rare kind
    (4 ``state`` nodes) cannot sit on budget the others could use, and a common
    one cannot claim the whole reserve. Kinds are walked in sorted order, so the
    remainder of an uneven division always lands in the same place and the
    resulting selection stays byte-reproducible across runs.
    """
    floors: dict[str, int] = {}
    pending = sorted(available)
    remaining = budget
    while pending:
        share = remaining // len(pending)
        if share <= 0:
            break
        small = [k for k in pending if available[k] <= share]
        if not small:
            for kind in pending:
                floors[kind] = share
                remaining -= share
            break
        for kind in small:
            floors[kind] = available[kind]
            remaining -= available[kind]
            pending.remove(kind)
    return floors


def _evict_lowest_degree_tail(items: list, need: int, kind_of, floors: dict[str, int]):
    """Drop ``need`` items from the end of a degree-ordered ``items`` list,
    stepping over any whose kind is already down to its floor.

    Returns ``(kept, evicted)``; ``evicted`` can be short of ``need`` when the
    floors hold the line, which the caller has to absorb elsewhere rather than
    exceed its own budget.
    """
    live: dict[str, int] = {}
    for item in items:
        kind = kind_of(item)
        live[kind] = live.get(kind, 0) + 1
    kept = list(items)
    evicted = 0
    i = len(kept) - 1
    while evicted < need and i >= 0:
        kind = kind_of(kept[i])
        if live[kind] > floors.get(kind, 0):
            del kept[i]
            live[kind] -= 1
            evicted += 1
        i -= 1
    return kept, evicted


def repo_subgraph(store: Store, repo_id: str, *, max_nodes: int = 500,
                  max_edges: int | None = None, max_fanout: int | None = None,
                  path_prefix: str | None = None,
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

    **Truncation caps:** ``max_nodes`` bounds the nodes actually returned, all of
    them. One-hop external nodes used to be exempt and purely additive, which made
    the cap a claim rather than a bound: ``--max-nodes 100`` wrote 728 nodes and
    the default 500 wrote 1186, while ``--help`` said "cap on rendered nodes" and
    the runtime warning named the same number. They now take whatever of the budget
    the repo's own nodes left, and when the repo filled it entirely they still get
    ``max_nodes // _EXTERNAL_BUDGET_SHARE`` of it, which the selection's
    lowest-degree tail gives way for -- so the widening still happens on a dense
    repo, a small repo with many dependencies still fills its view, and both stay
    inside the number the user asked for. Their
    edges count toward ``max_edges`` as before: once an edge reaches the page, a
    Mermaid renderer can't tell an internal edge from a one-hop external one, so
    exempting them would reopen the exact ``maxEdges`` render failure
    ``max_edges`` exists to prevent.

    **Per-kind floors.** Ranking purely by degree is not kind-neutral: measured on
    a real repo, the default view kept 100% of its ``package`` nodes and **0 of
    412** ``table``, **0 of 402** ``resource`` and **0 of 4** ``state`` nodes, so
    a repo that plainly has a schema and infrastructure rendered an empty
    ``erdiagram`` and ``deploymentdiagram`` while the console reported hundreds of
    nodes. Whole kinds are now guaranteed a floor -- a max-min fair split of
    ``max_nodes // _KIND_FLOOR_SHARE`` across the kinds present (see
    :func:`_kind_floors`) -- and only a kind's actual *shortfall* is ever taken,
    from the selection's lowest-degree tail, so a view that starves no kind is
    unchanged and the total still never exceeds ``max_nodes``. The floors are
    honoured at both eviction points: the selection itself, and the one-hop link
    budget below, where a floored node sits in exactly the tail that gives way.

    ``max_fanout`` (opt-in, ``None`` = uncapped here) caps how many outbound edges
    are taken from any ONE node, the anti-hub bound ``extract_subgraph`` applies to
    a seeded view. It was accepted on this path and ignored: 0, 1 and 100000 all
    produced byte-identical output. Uncapped stays the default so an unasked-for
    view is unchanged; the slice is taken in sorted order, never in whatever order
    the store returned.

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
    # A per-site relation stores one edge per occurrence in source, so a raw COUNT(*)
    # would rank a function called fifty times from a single caller as though fifty
    # callers depended on it -- and this ranking decides which nodes survive truncation
    # into a diagram, so the distortion silently changes what the reader is shown.
    # Collapse those to distinct pairs. Every other relation keeps counting rows, since
    # a repeated `contains` pair is one genuine declaration site each, and de-duplicating
    # it here would change today's output. Handing the non-per-site rows their own
    # edge_id as the discriminator lets one DISTINCT do both jobs in a single scan.
    # An empty PER_SITE_RELATIONS yields `IN (NULL)`, which matches nothing -- so this
    # degrades to the historical COUNT(*) rather than to invalid SQL.
    # Imported here, not at module level: this module deliberately keeps `kb.model`
    # (and so pydantic) off its load path -- see the note in `_is_not_a_real_repo`. By the
    # time a subgraph is being built the store has already imported the model, so the
    # deferred import costs nothing.
    from ..model import PER_SITE_RELATIONS
    per_site = sorted(PER_SITE_RELATIONS)
    per_site_ph = ",".join("?" for _ in per_site) or "NULL"
    deg_params = [*per_site, repo_id]
    ranked = f"""
        WITH deg_rows AS (
            SELECT DISTINCT src, dst, relation,
                   CASE WHEN relation IN ({per_site_ph}) THEN -1 ELSE edge_id END AS disc
            FROM edges WHERE repo_id=?
        )
        SELECT n.node_id, n.kind, COALESCE(SUM(deg.c), 0) AS d FROM nodes n
        LEFT JOIN (
            SELECT src AS node_id, COUNT(*) AS c FROM deg_rows GROUP BY src
            UNION ALL
            SELECT dst AS node_id, COUNT(*) AS c FROM deg_rows GROUP BY dst
        ) deg ON deg.node_id = n.node_id
        WHERE {where}{{kind}}
        GROUP BY n.node_id, n.kind
        ORDER BY d DESC, n.node_id ASC
        LIMIT ?
    """  # noqa: S608 - placeholders only; values bound
    rows = store.conn.execute(
        ranked.format(kind=""), (*deg_params, *params, max_nodes + 1)).fetchall()
    node_truncated = len(rows) > max_nodes

    floors_cache: dict[str, int] | None = None

    def floors() -> dict[str, int]:
        """This view's per-kind floors -- computed at most once, and only if a
        caller actually has to drop something (the common untruncated view pays
        for neither the extra query nor the arithmetic)."""
        nonlocal floors_cache
        if floors_cache is None:
            budget = max_nodes // _KIND_FLOOR_SHARE if max_nodes > 0 else 0
            available = dict(store.conn.execute(
                f"SELECT kind, COUNT(*) FROM nodes n WHERE {where} GROUP BY kind",  # noqa: S608 - placeholders only; values bound
                tuple(params)).fetchall()) if budget else {}
            # A single-kind view has nothing to starve, and a floor there would
            # only ever re-derive the ranking the query already produced.
            floors_cache = _kind_floors(available, budget) if len(available) > 1 else {}
        return floors_cache

    selected = rows[:max_nodes]
    if node_truncated and floors():
        held = {kind: 0 for kind in floors()}
        for _, kind, _d in selected:
            if kind in held:
                held[kind] += 1
        short = {k: floors()[k] - held[k] for k in sorted(held) if held[k] < floors()[k]}
        if short:
            have = {nid for nid, _k, _d in selected}
            # A kind's members that made the global cut are exactly its
            # highest-degree ones, so its top `floor` rows minus what is already
            # selected is precisely the shortfall, in the same order.
            extra = [r for kind in short
                     for r in store.conn.execute(
                         ranked.format(kind=" AND n.kind=?"),
                         (*deg_params, *params, kind, floors()[kind])).fetchall()
                     if r[0] not in have]
            selected, evicted = _evict_lowest_degree_tail(
                selected, len(extra), lambda r: r[1], floors())
            # Re-ranked, not appended: the view stays "the top max_nodes by
            # degree, subject to the floors" rather than "...plus a low-degree
            # annex", which keeps the edge walk below and the link budget's
            # tail eviction both meaning what they say.
            selected = sorted(selected + extra[:evicted], key=lambda r: (-r[2], r[0]))
    ids = [nid for nid, _k, _d in selected]
    seen = set(ids)
    nodes = [n for nid in ids if (n := store.get_node(nid)) is not None]
    edges: list[Edge] = []
    edge_keys: set[tuple] = set()
    edge_truncated = False
    external_ids: set[str] = set()
    ext_cache: dict[str, Node | None] = {}  # avoids a get_node() per repeated dst
    fanout_truncated = False
    for nid in ids:
        if max_edges is not None and len(edges) >= max_edges:
            edge_truncated = True
            break
        out = store.neighbors(nid, direction="out")
        if max_fanout is not None and len(out) > max_fanout:
            # Sorted before slicing: which edges survive an anti-hub cap must not
            # depend on the store's return order (see extract_subgraph, same rule).
            fanout_truncated = True
            out = sorted(out, key=lambda e: (e.relation, e.src, e.dst))[:max_fanout]
        for e in out:
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
    all_ext = sorted(external_ids)
    ext_ids = all_ext
    dropped_ext = 0
    if len(nodes) + len(ext_ids) > max_nodes:
        # The budget is the whole file's, not the selection query's. Externals fill
        # whatever the repo's own nodes left of it -- and when the repo filled it
        # entirely (a dense repo, the case the cap exists for), they still get a
        # reserved slice, which the lowest-degree tail of the selection gives way
        # for (it is ordered by degree). A node that fits is never evicted, and the
        # budget is never left half-used: only what genuinely does not fit is
        # dropped, and it is counted rather than appended over the cap.
        leftover = max_nodes - len(nodes)
        keep_ext = min(len(ext_ids),
                       leftover or max_nodes // _EXTERNAL_BUDGET_SHARE)
        if len(nodes) > max_nodes - keep_ext:
            # The tail gives way, but never past a kind's floor: a floored node
            # is by construction at the low-degree end, so an unguarded slice
            # here would quietly undo the floor the selection just applied.
            need = len(nodes) - (max_nodes - keep_ext)
            nodes, evicted = _evict_lowest_degree_tail(
                nodes, need, lambda n: n.kind, floors())
            if evicted < need:
                # The floors held; the link slice yields the difference instead,
                # so the total is still exactly max_nodes either way.
                keep_ext -= need - evicted
            node_truncated = node_truncated or evicted > 0  # in-repo nodes left out
        dropped_ext = len(all_ext) - keep_ext
        ext_ids = all_ext[:keep_ext]
    nodes.extend(ext_cache[nid] for nid in ext_ids)  # already resolved, non-None
    kept = {n.id for n in nodes}
    # Induced: an edge into a node the budget dropped must not survive as a dangling
    # reference (the same rule the max_nodes eviction already had to follow).
    edges = [e for e in edges if e.src in kept and e.dst in kept]
    # `truncated` covers dropped links too, but `node_truncated` must not: it is what
    # gates meta["total"], which counts IN-REPO nodes. Setting it when only links were
    # dropped made the banner read "showing 500 of 450" -- more rendered than exist.
    truncated = node_truncated or edge_truncated or fanout_truncated or bool(dropped_ext)
    if truncated:
        log(f"  truncated: repo {repo_id!r} subgraph exceeds max_nodes={max_nodes}"
            + (f" or max_edges={max_edges}" if max_edges is not None else "")
            + (f" or max_fanout={max_fanout}" if fanout_truncated else "")
            + (f"; {dropped_ext} linked external node(s) dropped to stay inside "
               f"max_nodes={max_nodes}" if dropped_ext else ""))
    if meta is not None:
        meta["truncated"] = truncated
        if node_truncated:  # cheap exact total only when we actually capped on nodes
            meta["total"] = store.conn.execute(
                f"SELECT COUNT(*) FROM nodes WHERE {where}", tuple(params)).fetchone()[0]  # noqa: S608 - placeholders only; values bound
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
    rows = store.conn.execute(f"SELECT file FROM nodes WHERE {where}", tuple(params)).fetchall()  # noqa: S608 - placeholders only; values bound
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
    # Both halves filtered. `sizes` already excludes pseudo ids, but the `list_repos`
    # half did not, so a persisted or legacy `@wiki:*` row would still be drawn and still
    # increment the total -- leaving the docstring's "must never appear" true of one input
    # and false of the union. A predicate applied to one of two sources is not applied.
    candidates = {r.id for r in store.list_repos() if not _is_not_a_real_repo(r.id)} | set(sizes)
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
