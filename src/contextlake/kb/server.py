"""MCP server for the knowledge layer.

Exposes the graph to AI agents as MCP *tools* (model-invoked) over stdio or
Streamable HTTP. Every text field returned is passed through ``sanitize_label``
first, so hostile repo content can't inject into an agent's context. Results are
structured + cited (each edge carries its source file and verified date); the
graph is an index, so inferred edges should be verified against the source.
"""

from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from .model import Edge, Node
from .security import sanitize_label
from .store.base import Store

_INSTRUCTIONS = (
    "Query the local code knowledge graph instead of grepping. Results are cited "
    "(source file + verified date) and confidence-tagged: treat EXTRACTED edges as "
    "ground truth and verify INFERRED/AMBIGUOUS ones against the cited file."
)


class NodeOut(BaseModel):
    id: str
    repo: str
    kind: str
    name: str
    qualified_name: str | None = None
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    lang: str | None = None


class EdgeOut(BaseModel):
    src: str
    dst: str
    relation: str
    confidence: str
    context: str | None = None
    source_file: str
    verified_at: str


class StatsOut(BaseModel):
    repos: int
    nodes: int
    edges: int
    by_confidence: dict[str, int]


class NeighborsOut(BaseModel):
    edges: list[EdgeOut]
    total: int
    truncated: bool  # true => more edges exist than returned; raise limit or narrow relation


class NodesOut(BaseModel):
    nodes: list[NodeOut]
    total: int
    truncated: bool


class RepoEdgeOut(BaseModel):
    src: str      # repo id
    dst: str      # repo id
    relation: str
    confidence: str
    weight: float
    context: str | None = None


class RepoEdgesOut(BaseModel):
    edges: list[RepoEdgeOut]
    total: int
    truncated: bool


class BlastHit(BaseModel):
    id: str
    repo: str
    kind: str
    name: str
    hop: int          # distance from the seed (1 = direct caller/dependent)
    via: str          # the relation traversed
    confidence: str   # verify INFERRED / AMBIGUOUS hits against the cited source


class BlastRadiusOut(BaseModel):
    seed: str
    hops: int
    hits: list[BlastHit]
    total: int
    truncated: bool


class WikiOut(BaseModel):
    repo: str
    found: bool
    stale: bool                  # the wiki may describe code that has since changed
    wiki_commit: str | None      # commit the wiki was generated from
    current_commit: str | None   # the repo's current indexed head
    markdown: str


class ReadmeOut(BaseModel):
    repo: str
    found: bool
    path: str | None             # the README filename that was read, e.g. "README.md"
    markdown: str


class TopSymbol(BaseModel):
    kind: str
    name: str
    file: str | None


class RepoBriefOut(BaseModel):
    repo: str
    found: bool
    head: str | None = None
    node_count: int = 0
    edge_count: int = 0
    kinds: dict[str, int] = {}       # kind -> count (e.g. {"function": 412, ...})
    langs: dict[str, int] = {}       # language -> count
    top_symbols: list[TopSymbol] = []
    packages: list[str] = []
    files: list[str] = []


class RepoSummaryOut(BaseModel):
    id: str
    default_branch: str | None = None
    head_commit: str | None = None
    indexed_at: str | None = None    # ISO timestamp of the last index, or null
    node_count: int | None = None    # only when include_stats


class ReposOut(BaseModel):
    total: int
    truncated: bool
    repos: list[RepoSummaryOut]


class LinkOut(BaseModel):
    kind: str                        # issue | page | design | merge_request | ...
    name: str
    url: str | None = None
    title: str | None = None
    status: str | None = None
    confidence: str


class RepoLinksOut(BaseModel):
    repo: str
    total: int
    links: dict[str, list[LinkOut]]  # relation (tracked_by/documented_by/…) -> links


class DanglingOut(BaseModel):
    repo: str
    src: str
    relation: str
    dst: str


class GraphHealthOut(BaseModel):
    repos: int
    checked: int                     # edges checked
    stale: int                       # repos whose HEAD moved past the index
    dangling: int                    # edges pointing at a missing node
    stale_repos: list[str]
    dangling_sample: list[DanglingOut]   # first 20


# EXTRACTED is ground truth; surface it before inferred/ambiguous so a truncated
# result keeps the most trustworthy edges.
_CONF_RANK = {"EXTRACTED": 0, "INFERRED": 1, "AMBIGUOUS": 2}


def _budget(items: list, limit: int) -> tuple[list, int, bool]:
    total = len(items)
    return items[:limit], total, total > limit


def _node_out(n: Node) -> NodeOut:
    s = sanitize_label
    return NodeOut(
        id=s(n.id), repo=s(n.repo), kind=s(n.kind), name=s(n.name),
        qualified_name=s(n.qualified_name) or None, file=s(n.file) or None,
        line_start=n.line_start, line_end=n.line_end, lang=s(n.lang) or None,
    )


def _edge_out(e: Edge) -> EdgeOut:
    s = sanitize_label
    return EdgeOut(
        src=s(e.src), dst=s(e.dst), relation=s(e.relation), confidence=e.confidence.value,
        context=s(e.context) or None, source_file=s(e.provenance.source_file),
        verified_at=e.provenance.verified_at.isoformat(),
    )


def _bfs_path(store: Store, src_id: str, dst_id: str, max_hops: int) -> list[str]:
    """Shortest undirected path of node ids between two nodes, or [] if none."""
    if src_id == dst_id:
        return [src_id] if store.get_node(src_id) else []
    prev: dict[str, str | None] = {src_id: None}
    queue = deque([(src_id, 0)])
    found = False
    while queue and not found:
        cur, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for e in store.neighbors(cur, direction="both"):
            nxt = e.dst if e.src == cur else e.src
            if nxt not in prev:
                prev[nxt] = cur
                if nxt == dst_id:
                    found = True
                    break
                queue.append((nxt, depth + 1))
    if not found:
        return []
    path, node = [], dst_id
    while node is not None:
        path.append(node)
        node = prev.get(node)
    path.reverse()
    return path


def build_server(
    store: Store, *, name: str = "contextlake-kb", host: str = "127.0.0.1", port: int = 8765,
    embedder=None, vector_store=None,
) -> FastMCP:
    mcp = FastMCP(
        name, instructions=_INSTRUCTIONS, host=host, port=port,
        stateless_http=True, json_response=True,
    )

    @mcp.tool()
    def graph_stats() -> StatsOut:
        """Counts of indexed repos/nodes/edges and the edge-confidence breakdown."""
        st = store.stats()
        return StatsOut(repos=st.repos, nodes=st.nodes, edges=st.edges,
                        by_confidence=st.by_confidence)

    @mcp.tool()
    def get_node(node_id: str) -> NodeOut | None:
        """Fetch a single graph node by its id."""
        n = store.get_node(node_id)
        return _node_out(n) if n else None

    @mcp.tool()
    def get_neighbors(
        node_id: str, relation: str | None = None, direction: str = "both", limit: int = 50
    ) -> NeighborsOut:
        """List edges incident to a node (EXTRACTED-first), capped at `limit`.

        direction: in | out | both; optional relation filter. `truncated`/`total`
        report when a hub has more edges than returned — raise `limit` or filter by
        relation rather than assuming the list is complete.
        """
        edges = sorted(
            store.neighbors(node_id, relation=relation, direction=direction),
            key=lambda e: _CONF_RANK.get(e.confidence.value, 9))
        kept, total, truncated = _budget(edges, limit)
        return NeighborsOut(edges=[_edge_out(e) for e in kept], total=total, truncated=truncated)

    @mcp.tool()
    def search_code(
        query: str, kind: str | None = None, repo: str | None = None, limit: int = 20
    ) -> list[NodeOut]:
        """Search the graph for nodes by name/symbol, with optional kind and repo filters."""
        return [_node_out(n) for n in store.search(query, kind=kind, repo=repo, limit=limit)]

    @mcp.tool()
    def find_definition(
        name: str, kind: str | None = None, repo: str | None = None
    ) -> list[NodeOut]:
        """Find definition(s) with an exact name — 'where is X defined?'."""
        return [_node_out(n) for n in store.nodes_by_name(name, kind=kind, repo=repo)]

    @mcp.tool()
    def find_callers(node_id: str, limit: int = 50) -> NodesOut:
        """Find the definitions that call a node — 'who calls X?' (incoming calls edges).

        EXTRACTED-first, capped at `limit`; `truncated`/`total` flag hot symbols with
        more callers than returned.
        """
        edges = sorted(store.neighbors(node_id, relation="calls", direction="in"),
                       key=lambda e: _CONF_RANK.get(e.confidence.value, 9))
        seen: set[str] = set()
        out: list[NodeOut] = []
        for e in edges:
            if e.src in seen:
                continue
            seen.add(e.src)
            n = store.get_node(e.src)
            if n:
                out.append(_node_out(n))
        kept, total, truncated = _budget(out, limit)
        return NodesOut(nodes=kept, total=total, truncated=truncated)

    @mcp.tool()
    def find_dependents(package: str, limit: int = 50) -> NodesOut:
        """Find files/repos that depend on a package — cross-repo 'who uses X?'.

        Capped at `limit`; `truncated`/`total` flag widely-used packages.
        """
        seen: set[str] = set()
        out: list[NodeOut] = []
        for pkg in store.nodes_by_name(package, kind="package"):
            for e in store.neighbors(pkg.id, relation="depends_on", direction="in"):
                if e.src in seen:
                    continue
                seen.add(e.src)
                n = store.get_node(e.src)
                if n:
                    out.append(_node_out(n))
        kept, total, truncated = _budget(out, limit)
        return NodesOut(nodes=kept, total=total, truncated=truncated)

    @mcp.tool()
    def repo_dependencies(repo: str, direction: str = "both", limit: int = 50) -> RepoEdgesOut:
        """Repo→repo package dependencies for `repo` (the cross-repo architecture map).

        From the package two-hop (publishes ⨝ depends_on): edges are
        ``dependent --depends_on--> publisher``, weight = shared package count.
        direction: out (what `repo` depends on) | in (who depends on `repo`) | both.
        INFERRED, manifest-derived — a likely undercount; verify against the cited repo.
        """
        from .arch.resolve import repo_dependency_edges
        rows = [e for e in repo_dependency_edges(store)
                if (direction in ("out", "both") and e["src"] == repo)
                or (direction in ("in", "both") and e["dst"] == repo)]
        rows.sort(key=lambda e: -e["weight"])
        kept, total, truncated = _budget(rows, limit)
        return RepoEdgesOut(total=total, truncated=truncated, edges=[
            RepoEdgeOut(src=sanitize_label(e["src"]), dst=sanitize_label(e["dst"]),
                        relation=e["relation"], confidence=e["confidence"],
                        weight=e["weight"]) for e in kept])

    @mcp.tool()
    def repo_flow(repo: str, direction: str = "both", limit: int = 50) -> RepoEdgesOut:
        """Repo→repo HTTP request flow for `repo` (who calls whom over HTTP).

        From the endpoint two-hop (exposes ⨝ calls_http): edges are
        ``caller --flow--> exposer`` (the direction a request travels), weight =
        shared endpoint count. direction: out (endpoints `repo` calls) | in (callers
        of `repo`'s endpoints) | both. INFERRED, regex+path-matched — an undercount
        that omits async/event coupling; verify against the cited repo.
        """
        from .arch.resolve import repo_http_flow_edges
        rows = [e for e in repo_http_flow_edges(store)
                if (direction in ("out", "both") and e["src"] == repo)
                or (direction in ("in", "both") and e["dst"] == repo)]
        rows.sort(key=lambda e: -e["weight"])
        kept, total, truncated = _budget(rows, limit)
        return RepoEdgesOut(total=total, truncated=truncated, edges=[
            RepoEdgeOut(src=sanitize_label(e["src"]), dst=sanitize_label(e["dst"]),
                        relation=e["relation"], confidence=e["confidence"],
                        weight=e["weight"], context=e.get("context")) for e in kept])

    @mcp.tool()
    def repo_event_flow(repo: str, direction: str = "both", limit: int = 50) -> RepoEdgesOut:
        """Repo→repo EVENT flow for `repo` (who publishes events that whom consumes).

        From the topic two-hop (publishes_event ⨝ consumes_event): edges are
        ``publisher --flow--> consumer`` (the direction an event travels), weight =
        shared topic count. direction: out (topics `repo` publishes that others consume)
        | in (publishers `repo` consumes from) | both. INFERRED, regex-detected literal
        topics — an undercount that omits config-variable topics; verify against the repo.
        """
        from .arch.resolve import repo_event_flow_edges
        rows = [e for e in repo_event_flow_edges(store)
                if (direction in ("out", "both") and e["src"] == repo)
                or (direction in ("in", "both") and e["dst"] == repo)]
        rows.sort(key=lambda e: -e["weight"])
        kept, total, truncated = _budget(rows, limit)
        return RepoEdgesOut(total=total, truncated=truncated, edges=[
            RepoEdgeOut(src=sanitize_label(e["src"]), dst=sanitize_label(e["dst"]),
                        relation=e["relation"], confidence=e["confidence"],
                        weight=e["weight"], context=e.get("context")) for e in kept])

    @mcp.tool()
    def blast_radius(node_id: str, hops: int = 3, relations: list[str] | None = None,
                     limit: int = 100) -> BlastRadiusOut:
        """What could break if you change this node — bounded transitive REVERSE reach.

        Walks INCOMING edges (who calls / depends on the node) breadth-first up to
        `hops`, capped at `limit`, over `relations` (default calls + depends_on).
        Each hit carries its hop distance, the relation, and confidence —
        EXTRACTED-first; verify INFERRED/AMBIGUOUS against the cited source. A
        bounded impact slice, never an exhaustive guarantee (`truncated` says when
        the cap was hit).
        """
        rels = set(relations or ["calls", "depends_on"])
        seen = {node_id}
        hits: list[BlastHit] = []
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
                hits.append(BlastHit(
                    id=sanitize_label(n.id), repo=sanitize_label(n.repo),
                    kind=sanitize_label(n.kind), name=sanitize_label(n.name),
                    hop=hop + 1, via=sanitize_label(e.relation),
                    confidence=e.confidence.value))
                frontier.append((e.src, hop + 1))
        return BlastRadiusOut(seed=node_id, hops=hops, total=len(hits),
                              truncated=truncated, hits=hits)

    @mcp.tool()
    def get_wiki(repo: str) -> WikiOut:
        """The generated LLM-wiki page for a repo (Markdown prose).

        **Advisory, not ground truth** — synthesized text to verify against the
        cited sources/graph; it never outranks EXTRACTED facts. ``stale`` is true
        when the wiki was generated from a different commit than the repo's current
        indexed head (or either is unknown), so an agent never cites prose that
        describes code which has since changed.
        """
        sp = getattr(store, "path", None)
        wiki_file = (Path(sp).parent / "wiki" / (repo.replace("/", "__") + ".md")
                     if sp else None)
        if not wiki_file or not wiki_file.exists():
            return WikiOut(repo=sanitize_label(repo), found=False, stale=True,
                           wiki_commit=None, current_commit=None, markdown="")
        raw = wiki_file.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"at commit `([^`]+)`", raw)
        wiki_commit = m.group(1) if m else None
        r = store.get_repo(repo)
        current = r.head_commit if r else None
        stale = wiki_commit is None or current is None or wiki_commit != current
        return WikiOut(
            repo=sanitize_label(repo), found=True, stale=stale,
            wiki_commit=sanitize_label(wiki_commit) if wiki_commit else None,
            current_commit=sanitize_label(current) if current else None,
            markdown=sanitize_label(raw, max_len=200_000))

    @mcp.tool()
    def get_readme(repo: str) -> ReadmeOut:
        """The repo's own README, read from its local clone (offline).

        Ground truth — the maintainers' own words, straight from the working tree —
        distinct from the synthesized, advisory ``get_wiki`` prose. Returns the first
        README-like file found, or ``found=False`` if the clone has none.
        """
        r = store.get_repo(repo)
        base = Path(r.path) if r and getattr(r, "path", None) else None
        if base and base.is_dir():
            for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
                f = base / name
                if f.is_file():
                    raw = f.read_text(encoding="utf-8", errors="replace")
                    return ReadmeOut(repo=sanitize_label(repo), found=True, path=name,
                                     markdown=sanitize_label(raw, max_len=200_000))
        return ReadmeOut(repo=sanitize_label(repo), found=False, path=None, markdown="")

    @mcp.tool()
    def get_repo_brief(repo: str) -> RepoBriefOut:
        """A repo's 'anatomy' — grounded facts from its indexed graph (offline).

        node/edge counts, kind + language breakdown, the top symbols by connectivity,
        packages, and a file sample. ``found=False`` if the repo has no indexed shard.
        """
        from .wiki.generate import repo_brief
        sp = getattr(store, "path", None)
        brief = repo_brief(Path(sp).parent, repo) if sp else None
        if not brief:
            return RepoBriefOut(repo=sanitize_label(repo), found=False)
        return RepoBriefOut(
            repo=sanitize_label(repo), found=True,
            head=sanitize_label(brief["head"]) if brief.get("head") else None,
            node_count=brief["node_count"], edge_count=brief["edge_count"],
            kinds=brief["kinds"], langs=brief["langs"],
            top_symbols=[TopSymbol(kind=k, name=sanitize_label(n),
                                   file=sanitize_label(f) if f else None)
                         for (k, n, f) in brief["top_symbols"]],
            packages=[sanitize_label(p) for p in brief["packages"]],
            files=[sanitize_label(f) for f in brief["files"]])

    @mcp.tool()
    def list_repos(include_stats: bool = True, limit: int = 500) -> ReposOut:
        """The repo fleet — the dashboard's repository list (offline).

        Each entry carries the branch, indexed head, and last-index time; with
        ``include_stats`` (default) also the indexed node count. Capped at ``limit``.
        """
        counts = {}
        if include_stats:
            counts = dict(store.conn.execute(
                "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
        rows = store.conn.execute(
            "SELECT repo_id, default_branch, head_commit, indexed_at FROM repos "
            "ORDER BY repo_id LIMIT ?", (limit + 1,)).fetchall()
        truncated = len(rows) > limit
        repos = [RepoSummaryOut(
            id=sanitize_label(r["repo_id"]),
            default_branch=r["default_branch"],
            head_commit=sanitize_label(r["head_commit"]) if r["head_commit"] else None,
            indexed_at=r["indexed_at"],
            node_count=int(counts.get(r["repo_id"], 0)) if include_stats else None,
        ) for r in rows[:limit]]
        total = store.conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        return ReposOut(total=total, truncated=truncated, repos=repos)

    @mcp.tool()
    def get_repo_links(repo: str) -> RepoLinksOut:
        """A repo's cross-links to external knowledge — Jira / Confluence / Figma /
        GitLab — grouped by relation (tracked_by / documented_by / designed_in /
        has_merge_request / has_issue). Populated by `connect`; served offline after.
        """
        from .ids import make_id
        link_rels = {"tracked_by", "documented_by", "designed_in",
                     "has_merge_request", "has_issue"}
        grouped: dict[str, list[LinkOut]] = {}
        for e in store.neighbors(make_id("repo", repo), direction="out"):
            if e.relation not in link_rels:
                continue
            n = store.get_node(e.dst)
            if not n:
                continue
            attrs = getattr(n, "attrs", None) or {}
            title = attrs.get("title") or attrs.get("summary")
            conf = e.confidence.value if hasattr(e.confidence, "value") else str(e.confidence)
            grouped.setdefault(e.relation, []).append(LinkOut(
                kind=n.kind, name=sanitize_label(n.name),
                url=sanitize_label(attrs["url"]) if attrs.get("url") else None,
                title=sanitize_label(title) if title else None,
                status=sanitize_label(attrs["status"]) if attrs.get("status") else None,
                confidence=conf))
        total = sum(len(v) for v in grouped.values())
        return RepoLinksOut(repo=sanitize_label(repo), total=total, links=grouped)

    @mcp.tool()
    def graph_health() -> GraphHealthOut:
        """Knowledge-graph health — stale repos (local HEAD moved past the index) and
        dangling edges (pointing at a missing node). The dashboard's health panel;
        offline (reads local git HEADs).
        """
        from .commands import lint_result
        sp = getattr(store, "path", None)
        res = lint_result(store, Path(sp).parent) if sp else {
            "repos": 0, "checked": 0, "stale": 0, "dangling": 0,
            "stale_repos": [], "dangling_sample": []}
        return GraphHealthOut(
            repos=res["repos"], checked=res["checked"], stale=res["stale"],
            dangling=res["dangling"],
            stale_repos=[sanitize_label(x) for x in res["stale_repos"]],
            dangling_sample=[DanglingOut(
                repo=sanitize_label(d["repo"]), src=sanitize_label(d["src"]),
                relation=d["relation"], dst=sanitize_label(d["dst"]))
                for d in res["dangling_sample"]])

    @mcp.tool()
    def shortest_path(src_id: str, dst_id: str, max_hops: int = 6) -> list[NodeOut]:
        """Shortest path between two nodes over the graph (<= max_hops). Empty if none."""
        path_ids = _bfs_path(store, src_id, dst_id, max_hops)
        return [_node_out(n) for nid in path_ids if (n := store.get_node(nid))]

    if embedder is not None and vector_store is not None:
        @mcp.tool()
        def semantic_search(query: str, k: int = 10, repo: str | None = None) -> list[NodeOut]:
            """Semantic (embedding) search over indexed nodes — for natural-language
            queries where exact names are unknown. Results are ranked by similarity."""
            vec = embedder.embed([query])[0]
            out: list[NodeOut] = []
            for node_id, _score in vector_store.search(vec, k=k, repo=repo):
                n = store.get_node(node_id)
                if n:
                    out.append(_node_out(n))
            return out

        @mcp.tool()
        def hybrid_search(query: str, k: int = 10, repo: str | None = None) -> list[NodeOut]:
            """Hybrid retrieval: seed with embeddings, then rank by Personalized
            PageRank over the graph. Surfaces structurally-related nodes (callers,
            dependents) that a pure semantic match would miss."""
            from .embeddings.hybrid import hybrid_search as _hybrid

            out: list[NodeOut] = []
            for node_id, _score in _hybrid(store, vector_store, embedder, query, k=k, repo=repo):
                n = store.get_node(node_id)
                if n:
                    out.append(_node_out(n))
            return out

    @mcp.resource("kb://stats")
    def stats_resource() -> str:
        st = store.stats()
        return json.dumps(
            {"repos": st.repos, "nodes": st.nodes, "edges": st.edges,
             "by_confidence": st.by_confidence}
        )

    return mcp


def run_server(
    store: Store, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765,
    embedder=None, vector_store=None,
) -> None:
    """Build and run the MCP server (blocking)."""
    build_server(
        store, host=host, port=port, embedder=embedder, vector_store=vector_store
    ).run(transport=transport)
