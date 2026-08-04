"""MCP server for the knowledge layer.

Exposes the graph to AI agents as MCP *tools* (model-invoked) over stdio or
Streamable HTTP. Every text field returned is passed through ``sanitize_label``
first, so hostile repo content can't inject into an agent's context. Results are
structured + cited (each edge carries its source file and verified date); the
graph is an index, so inferred edges should be verified against the source.

The two transport families have deliberately different security postures. stdio
is a pipe the editor spawns and owns -- there is no socket to reach and no
third party to authenticate, so it stays exactly as it was. The HTTP-family
transports are a listening socket, and what they answer (every indexed file
path, symbol name, docstring and owner identity) is precisely what an attacker
would want, so they get a bearer token and the MCP spec's required Host/Origin
validation. See :func:`build_http_app`.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from collections import deque
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from .. import observability
from .model import EXTERNAL_LINK_RELATIONS, Edge, Node
from .security import sanitize_label
from .store.base import Store

# Transports that open a socket, and so need authenticating. The values are the
# SDK's own transport names, not the CLI's (`kb serve --transport http` maps to
# "streamable-http" in cmds/serve.py).
HTTP_TRANSPORTS = frozenset({"streamable-http", "sse"})

TOKEN_ENV = "CONTEXTLAKE_MCP_TOKEN"

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
    signature: str | None = None     # parameter signature (definitions)
    doc: str | None = None           # captured docstring (definitions)


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


class OwnerOut(BaseModel):
    name: str
    commits: int
    lines: int
    last_active: str   # YYYY-MM-DD of the contributor's most recent commit
    share: float       # 0..1 fraction of the recency-weighted score


class OwnersOut(BaseModel):
    scope: str         # repo (optionally repo:sub-path) the ranking is for
    owners: list[OwnerOut]


class WikiOut(BaseModel):
    repo: str
    found: bool
    stale: bool                  # the wiki may describe code that has since changed
    wiki_commit: str | None      # commit the wiki was generated from
    current_commit: str | None   # the repo's current indexed head
    markdown: str
    kind: str = "repo"           # "repo" | "cluster" (a namespace-level page)


class ReadmeOut(BaseModel):
    repo: str
    found: bool
    path: str | None             # the README filename that was read, e.g. "README.md"
    markdown: str


class TopSymbol(BaseModel):
    kind: str
    name: str
    file: str | None
    signature: str | None = None
    doc: str | None = None


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
    # Repos whose graph an older parser built: the code has not moved, but this
    # build would not produce the graph on disk, so its answers are a previous
    # build's. Additive fields with defaults, so an existing caller reading only
    # the counts above is unaffected. Overlaps `stale` on purpose -- a repo can
    # be both, and each count answers its own question (see commands.lint_result).
    parser_stale: int = 0
    parser_stale_repos: list[str] = Field(default_factory=list)


class AskOut(BaseModel):
    """One-shot answer envelope: the router picked a substrate and filled the
    matching field. Read ``route`` to know which field holds the answer, and
    ``note`` for what it is and how much to trust it."""
    question: str
    route: str                       # definition|callers|dependents|impact|owners|explain|search
    target: str | None = None        # the symbol / repo the question resolved to
    note: str                        # plain-language: what answered, and the trust label
    nodes: list[NodeOut] = []         # definition | callers | dependents | search
    blast: BlastRadiusOut | None = None   # impact
    owners: OwnersOut | None = None       # owners
    wiki: WikiOut | None = None            # explain (ADVISORY prose, when a wiki exists)
    brief: RepoBriefOut | None = None      # explain fallback: the repo's grounded anatomy
    truncated: bool = False          # more results exist than returned (callers/dependents)


# EXTRACTED is ground truth; surface it before inferred/ambiguous so a truncated
# result keeps the most trustworthy edges.
_CONF_RANK = {"EXTRACTED": 0, "INFERRED": 1, "AMBIGUOUS": 2}


def _budget(items: list, limit: int) -> tuple[list, int, bool]:
    total = len(items)
    return items[:limit], total, total > limit


def _node_out(n: Node) -> NodeOut:
    s = sanitize_label
    attrs = getattr(n, "attrs", None) or {}
    return NodeOut(
        id=s(n.id), repo=s(n.repo), kind=s(n.kind), name=s(n.name),
        qualified_name=s(n.qualified_name) or None, file=s(n.file) or None,
        line_start=n.line_start, line_end=n.line_end, lang=s(n.lang) or None,
        signature=s(attrs["signature"]) if attrs.get("signature") else None,
        doc=s(attrs["doc"]) if attrs.get("doc") else None,
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
    store: Store, *, name: str = "contextlake-kb", embedder=None, vector_store=None,
) -> MCPServer:
    # host/port/stateless_http/json_response moved to run_server()/.run() --
    # this SDK version's server object no longer takes them at construction
    # (they're streamable-http-transport options, not server-identity options).
    from .. import __version__

    mcp = MCPServer(name, instructions=_INSTRUCTIONS, version=__version__)

    @mcp.tool()
    def graph_stats() -> StatsOut:
        """Counts of indexed repos/nodes/edges and the edge-confidence breakdown."""
        st = store.stats()
        return StatsOut(repos=st.repos, nodes=st.nodes, edges=st.edges,
                        by_confidence=st.by_confidence)

    @mcp.tool()
    def who_knows(repo: str, path: str | None = None, limit: int = 10) -> OwnersOut:
        """Likely owners / subject-matter experts for `repo` (optionally a sub-`path`).

        Ranked from the repo's git commit history by a recency-weighted blend of
        commit volume and lines changed, so recent active contributors outrank a
        long-departed prolific author. Names are as committed in the local mirror.
        """
        from .ownership import compute_owners
        r = store.get_repo(repo)
        scope = sanitize_label(repo + (f":{path}" if path else ""))
        if not r or not r.path:
            return OwnersOut(scope=scope, owners=[])
        owners = compute_owners(r.path, path, limit=max(1, min(limit, 50)))
        return OwnersOut(scope=scope, owners=[
            OwnerOut(name=sanitize_label(o.name), commits=o.commits, lines=o.lines,
                     last_active=o.last_active, share=round(o.share, 4))
            for o in owners])

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

    def _as_node_id(node_id_or_name: str) -> str | None:
        """Accept a node id OR a bare symbol name. Agents (and humans) naturally pass
        a name like ``CatalogService``; resolve it to the first matching node id so
        callers/impact work without a separate find_definition round-trip. An exact
        node id is returned as-is; an unknown string yields None."""
        if not node_id_or_name:
            return None
        if store.get_node(node_id_or_name):
            return node_id_or_name
        matches = store.nodes_by_name(node_id_or_name)
        return matches[0].id if matches else None

    @mcp.tool()
    def find_callers(node_id: str, limit: int = 50) -> NodesOut:
        """Find the definitions that call a node — 'who calls X?' (incoming calls edges).

        `node_id` accepts a node id **or a bare symbol name** (e.g. ``CatalogService``),
        resolved to its first matching definition. EXTRACTED-first, capped at `limit`;
        `truncated`/`total` flag hot symbols with more callers than returned.
        """
        nid = _as_node_id(node_id)
        if nid is None:
            return NodesOut(nodes=[], total=0, truncated=False)
        edges = sorted(store.neighbors(nid, relation="calls", direction="in"),
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

        `node_id` accepts a node id **or a bare symbol name** (e.g. ``CatalogService``),
        resolved to its first matching definition. Walks INCOMING edges (who calls /
        depends on / subclasses the node) breadth-first up to `hops`, capped at
        `limit`, over `relations` (default calls + depends_on + inherits).
        Each hit carries its hop distance, the relation, and confidence —
        EXTRACTED-first; verify INFERRED/AMBIGUOUS against the cited source. A
        bounded impact slice, never an exhaustive guarantee (`truncated` says when
        the cap was hit).
        """
        from .impact import blast_radius as _blast
        nid = _as_node_id(node_id) or node_id
        hits, truncated = _blast(store, nid, hops=hops, relations=relations, limit=limit)
        return BlastRadiusOut(
            seed=nid, hops=hops, total=len(hits), truncated=truncated,
            hits=[BlastHit(id=sanitize_label(h.id), repo=sanitize_label(h.repo),
                           kind=sanitize_label(h.kind), name=sanitize_label(h.name),
                           hop=h.hop, via=sanitize_label(h.via), confidence=h.confidence)
                  for h in hits])

    @mcp.tool()
    def get_wiki(repo: str) -> WikiOut:
        """The generated LLM-wiki page for a repo, or a namespace's cluster page.

        Pass a repo id for its page, or a namespace prefix (e.g. ``team/api``)
        for the cluster page narrating that group's cross-repo coupling.

        **Advisory, not ground truth** — synthesized text to verify against the
        cited sources/graph; it never outranks EXTRACTED facts. ``stale`` is true
        when a repo wiki was generated from a different commit than the repo's
        current indexed head (or either is unknown), so an agent never cites prose
        that describes code which has since changed. Cluster pages carry their own
        member-commit fingerprint and are not single-commit stale-checked.
        """
        sp = getattr(store, "path", None)
        wiki_dir = Path(sp).parent / "wiki" if sp else None
        slug = repo.replace("/", "__")
        wiki_file = wiki_dir / (slug + ".md") if wiki_dir else None
        if not wiki_file or not wiki_file.exists():
            # fall back to a cluster (namespace) page for this prefix
            cluster_file = wiki_dir / "_clusters" / (slug + ".md") if wiki_dir else None
            if cluster_file and cluster_file.exists():
                craw = cluster_file.read_text(encoding="utf-8", errors="replace")
                return WikiOut(repo=sanitize_label(repo), found=True, stale=False,
                               wiki_commit=None, current_commit=None, kind="cluster",
                               markdown=sanitize_label(craw, max_len=200_000))
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
        # store=None: this tool's output (RepoBriefOut) doesn't surface
        # readme_excerpt, so skip the filesystem read that field would trigger.
        brief = repo_brief(Path(sp).parent, repo) if sp else None
        if not brief:
            return RepoBriefOut(repo=sanitize_label(repo), found=False)
        return RepoBriefOut(
            repo=sanitize_label(repo), found=True,
            head=sanitize_label(brief["head"]) if brief.get("head") else None,
            node_count=brief["node_count"], edge_count=brief["edge_count"],
            kinds=brief["kinds"], langs=brief["langs"],
            top_symbols=[TopSymbol(
                kind=t["kind"], name=sanitize_label(t["name"]),
                file=sanitize_label(t["file"]) if t.get("file") else None,
                signature=sanitize_label(t["signature"]) if t.get("signature") else None,
                doc=sanitize_label(t["doc"]) if t.get("doc") else None,
            ) for t in brief["top_symbols"]],
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
        GitLab / Slack — grouped by relation (tracked_by / documented_by /
        designed_in / has_merge_request / has_issue / touched_by / discussed_in /
        referenced_in). Populated by `connect`; served offline after.
        """
        from .ids import make_id
        grouped: dict[str, list[LinkOut]] = {}
        for e in store.neighbors(make_id("repo", repo), direction="out"):
            if e.relation not in EXTERNAL_LINK_RELATIONS:
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
        """Knowledge-graph health — stale repos (local HEAD moved past the index),
        repos whose graph an older parser built (re-index to refresh), and dangling
        edges (pointing at a missing node). The dashboard's health panel; offline
        (reads local git HEADs).
        """
        from .commands import lint_result
        sp = getattr(store, "path", None)
        res = lint_result(store, Path(sp).parent) if sp else {
            "repos": 0, "checked": 0, "stale": 0, "dangling": 0, "parser_stale": 0,
            "stale_repos": [], "parser_stale_repos": [], "dangling_sample": []}
        return GraphHealthOut(
            repos=res["repos"], checked=res["checked"], stale=res["stale"],
            dangling=res["dangling"], parser_stale=res["parser_stale"],
            stale_repos=[sanitize_label(x) for x in res["stale_repos"]],
            parser_stale_repos=[sanitize_label(x) for x in res["parser_stale_repos"]],
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
            queries where exact names are unknown. Results are ranked by similarity.
            Hits of kind 'wiki'/'document' are ADVISORY prose (LLM-generated or
            ingested), not extracted code facts — verify against the cited file."""
            if not query.strip():
                return []  # matches search_code's empty-query handling, not a crash
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

            if not query.strip():
                return []  # matches search_code's empty-query handling, not a crash
            out: list[NodeOut] = []
            for node_id, _score in _hybrid(store, vector_store, embedder, query, k=k, repo=repo):
                n = store.get_node(node_id)
                if n:
                    out.append(_node_out(n))
            return out

    @mcp.tool()
    def ask(question: str, k: int = 8, repo: str | None = None) -> AskOut:
        """One question, auto-routed to the right substrate — for agents that would
        rather ask in plain language than pick among the graph tools.

        Classifies the question (definition / callers / dependents / impact / owners /
        explain / search), resolves the symbol or repo it is about, and returns a
        single labeled answer. Graph routes are cited and confidence-tagged; the
        'explain' route returns ADVISORY wiki prose. When unsure, prefer the specific
        tool (find_definition, find_callers, blast_radius, …) — this is the convenience
        front door over them, not a replacement."""
        from .router import (
            CALLERS,
            DEFINITION,
            DEPENDENTS,
            EXPLAIN,
            IMPACT,
            OWNERS,
            SEARCH,
            SUBCLASSES,
            classify,
        )

        route, target = classify(question)

        def _out(note, **kw):
            return AskOut(question=question, route=route, target=target, note=note, **kw)

        def _resolve_id(name):
            """A question names a symbol; callers/impact need a node id."""
            if not name:
                return None, "no symbol found in the question"
            if store.get_node(name):
                return name, None
            matches = store.nodes_by_name(name, repo=repo)
            if not matches:
                return None, f"no indexed symbol named {name!r}"
            extra = (f" ({len(matches)} matched {name!r}; used the first)"
                     if len(matches) > 1 else "")
            return matches[0].id, extra or None

        def _resolve_repo(name):
            """A question names a repo, typically by a short/partial name (the
            natural way a person refers to one, e.g. "the catalog-api") -- but
            owners/explain need the full, canonical repo id (host-qualified,
            e.g. "gitlab.example.com/acme/catalog-api") an exact store lookup
            requires. Falls back to matching the repo's last path segment,
            mirroring how _resolve_id resolves a symbol name to a node id."""
            if not name:
                return None, "no repo named in the question"
            if store.get_repo(name):
                return name, None
            matches = [r.id for r in store.list_repos() if r.id.rsplit("/", 1)[-1] == name]
            if not matches:
                return None, f"no indexed repo matching {name!r}"
            extra = (f" ({len(matches)} matched {name!r}; used the first)"
                     if len(matches) > 1 else "")
            return matches[0], extra or None

        if route == DEFINITION:
            hits = find_definition(target, repo=repo) if target else []
            if hits:
                return _out(f"Definition(s) of {target!r} — EXTRACTED, cited.", nodes=hits)
            # fall through to a search when the exact name isn't a definition
            route = SEARCH

        if route == CALLERS:
            nid, why = _resolve_id(target)
            if nid is None:
                return _out(f"Couldn't resolve a symbol to find callers of — {why}.")
            res = find_callers(nid, limit=k)
            return _out(f"Callers of {target!r} — incoming calls, EXTRACTED-first"
                        + (why or "") + ".", nodes=res.nodes, truncated=res.truncated)

        if route == DEPENDENTS:
            res = find_dependents(target, limit=k) if target else NodesOut(
                nodes=[], total=0, truncated=False)
            return _out(f"Repos/files depending on package {target!r} — INFERRED from "
                        "manifests, verify against the cited file.",
                        nodes=res.nodes, truncated=res.truncated)

        if route == SUBCLASSES:
            nid, why = _resolve_id(target)
            if nid is None:
                return _out(f"Couldn't resolve a type to find subclasses of — {why}.")
            # incoming `inherits` edges are the types that extend/implement this one
            subs, seen = [], set()
            for e in store.neighbors(nid, relation="inherits", direction="in"):
                if e.src in seen:
                    continue
                seen.add(e.src)
                if (n := store.get_node(e.src)):
                    subs.append(_node_out(n))
            return _out(f"Types that extend or implement {target!r}"
                        + (why or "") + f" — {len(subs)} found via inherits edges.",
                        nodes=subs[:k], truncated=len(subs) > k)

        if route == IMPACT:
            nid, why = _resolve_id(target)
            if nid is None:
                return _out(f"Couldn't resolve a symbol for blast radius — {why}.")
            res = blast_radius(nid, hops=3)
            return _out(f"Blast radius of {target!r}: {res.total} node(s) within 3 hops"
                        + (why or "") + ". Reverse reach over calls+depends_on+inherits; "
                        "INFERRED/AMBIGUOUS hits may under- or over-count — verify.",
                        blast=res, truncated=res.truncated)

        if route == OWNERS:
            rid, why = _resolve_repo(target)
            if rid is None:
                return _out(f"Couldn't tell which repo to find owners for — {why}.")
            res = who_knows(rid, limit=k)
            return _out(f"Likely owners / SMEs for {target!r}" + (why or "")
                        + ", ranked from git history.", owners=res)

        if route == EXPLAIN:
            if target:
                rid, why = _resolve_repo(target)
                if rid is not None:
                    w = get_wiki(rid)
                    if w.found:
                        stale = " (STALE — the code changed since)" if w.stale else ""
                        return _out(f"Curated wiki for {target!r}{stale}" + (why or "")
                                    + " — ADVISORY prose, grounded in the graph; verify "
                                    "specifics against code.", wiki=w)
                    # No wiki page — a structured brief (real anatomy) beats a blind
                    # search for an "explain this repo" question.
                    b = get_repo_brief(rid)
                    if b.found:
                        return _out(f"No wiki for {target!r} yet" + (why or "")
                                    + " — here is its grounded anatomy (top symbols, "
                                    "packages, languages) from the graph. Run "
                                    "`contextlake kb wiki` for prose.", brief=b)
            # not a repo we know: degrade to a semantic/keyword explanation search
            route = SEARCH

        # SEARCH (fallback for everything else, and for definition/explain misses).
        # `route` has already been reassigned to SEARCH above where we fell through,
        # so _out records it correctly.
        route = SEARCH
        if question.strip() and embedder is not None and vector_store is not None:
            vec = embedder.embed([question])[0]
            out: list[NodeOut] = []
            for nid, _s in vector_store.search(vec, k=k, repo=repo):
                n = store.get_node(nid)
                if n:
                    out.append(_node_out(n))
            return _out("Semantic search over the graph (names + signatures + docstrings); "
                        "'wiki'/'document' hits are ADVISORY. No exact route matched.",
                        nodes=out)
        hits = search_code(question, repo=repo, limit=k)
        return _out("Full-text search over node names (no embeddings configured). "
                    "No exact route matched.", nodes=hits)

    @mcp.resource("kb://stats")
    def stats_resource() -> str:
        st = store.stats()
        return json.dumps(
            {"repos": st.repos, "nodes": st.nodes, "edges": st.edges,
             "by_confidence": st.by_confidence}
        )

    return mcp


def resolve_token() -> tuple[str, bool]:
    """The bearer token for an HTTP-family transport, and whether it came from env.

    ``CONTEXTLAKE_MCP_TOKEN`` exists so a client config can pin one stable token
    instead of chasing a freshly-minted one on every launch. Empty or whitespace
    is treated as unset and a fresh token is minted rather than honoured: an env
    var a shell expanded to "" must not be the difference between a server only
    its operator can reach and one anybody can.
    """
    env = (os.environ.get(TOKEN_ENV) or "").strip()
    return (env, True) if env else (secrets.token_urlsafe(32), False)


def _host_header_form(host: str) -> str:
    """An IPv6 literal is bracketed in a Host header (RFC 3986), bare as a bind."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def transport_security(host: str) -> TransportSecuritySettings:
    """Host/Origin allow-list for an HTTP-family bind.

    This is the MCP spec's Origin-validation requirement for HTTP transports,
    enforced by the SDK's own ``TransportSecurityMiddleware`` rather than a
    second implementation here.

    The loopback names are included unconditionally, not only for a loopback
    bind, and that is load-bearing: ``MCPServer.sse_app`` /
    ``.streamable_http_app`` auto-build protective settings *only* when
    ``transport_security is None``, so passing our own replaces theirs. Deriving
    the list purely from ``host`` would have hardened the remote bind while
    quietly disarming the default ``--host 127.0.0.1`` one.

    A wildcard bind keeps the same consequence documented for the local HTTP
    servers in :func:`..http_base.allowed_host_headers`: bound to ``0.0.0.0``, a
    request naming the machine's LAN address in ``Host`` is refused, because
    that address is not in this list. Bind the address clients will actually
    name (``--host 192.0.2.10``).

    No port here, deliberately: every entry ends in the SDK's ``:*`` wildcard,
    matching what the SDK itself builds for a loopback bind. A request reaching
    this process already arrived on the port we bound, so a Host header naming
    a different one is a client mistake rather than an attack a stricter list
    would catch. (The wildcard requires *some* port, so a bare ``Host: example``
    with no port is refused either way.)
    """
    hosts = {f"{h}:*" for h in ("127.0.0.1", "localhost", "[::1]")}
    hosts.add(f"{_host_header_form(host)}:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(f"http://{h}" for h in hosts),
    )


async def _send_unauthorized(send) -> None:
    body = b'{"error":"unauthorized"}'
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        # RFC 6750: name the scheme to retry with, without a realm -- there is
        # no authorization server here, just a process-local shared secret.
        (b"www-authenticate", b"Bearer"),
    ]})
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """ASGI gate requiring ``Authorization: Bearer <token>`` on every HTTP request.

    Deliberately not the SDK's own auth hook. That one is OAuth-shaped:
    ``AuthSettings`` makes ``issuer_url`` a required field and ``MCPServer``
    refuses a ``token_verifier`` without it, so routing one process-local shared
    secret through it would mean advertising an authorization server that does
    not exist. A shared secret is a middleware, so it is one.

    Non-HTTP scopes pass straight through -- notably ``lifespan``, which is what
    starts the SDK's session manager; gating that would leave the app never
    started rather than merely unauthenticated.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._token = token.encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        await _send_unauthorized(send)

    def _authorized(self, scope) -> bool:
        for key, value in scope.get("headers") or ():
            if key.lower() != b"authorization":
                continue
            scheme, _, presented = value.partition(b" ")
            # Compared as bytes end to end: hmac.compare_digest raises TypeError
            # on a str carrying non-ASCII, which would surface a hostile token
            # as a 500 instead of a 401.
            return (scheme.lower() == b"bearer"
                    and hmac.compare_digest(presented.strip(), self._token))
        return False


def build_http_app(
    store: Store, *, transport: str, host: str, token: str,
    embedder=None, vector_store=None,
):
    """The token-gated, Origin-checked ASGI app for an HTTP-family transport.

    Split out of :func:`run_server` so the security properties are assertable
    without binding a socket: the SDK's ``run_streamable_http_async`` /
    ``run_sse_async`` go straight into ``uvicorn.Server.serve()``, so a test can
    never reach the app they build.

    sse is the legacy HTTP+SSE transport (superseded by streamable-http in the
    MCP spec, kept for older clients that only speak SSE -- see docs/serve.md);
    its ``/messages/`` POST endpoint is behind the same gate as ``/sse``.
    """
    server = build_server(store, embedder=embedder, vector_store=vector_store)
    security = transport_security(host)
    if transport == "sse":
        app = server.sse_app(transport_security=security, host=host)
    else:
        app = server.streamable_http_app(
            stateless_http=True, json_response=True,
            transport_security=security, host=host)
    return BearerAuthMiddleware(app, token)


def run_server(
    store: Store, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765,
    embedder=None, vector_store=None, token: str | None = None,
) -> None:
    """Build and run the MCP server (blocking).

    stdio takes the SDK's ``.run()`` path untouched, with no token and no
    host/port: it is a pipe the editor already owns, it is the default and by
    far the most-used transport, and a handshake added there would break every
    existing editor entry to fix an exposure it does not have.

    The HTTP-family transports build their app here instead of via ``.run()``,
    because ``.run()`` offers no seam to wrap the app in
    :class:`BearerAuthMiddleware`. ``token`` should be supplied by the caller
    (cmds/serve.py, which also prints it); a missing one is minted rather than
    left off, so no code path can start an unauthenticated socket.
    """
    if transport not in HTTP_TRANSPORTS:
        build_server(store, embedder=embedder, vector_store=vector_store).run(
            transport=transport)
        return

    import uvicorn

    app = build_http_app(
        store, transport=transport, host=host, token=token or resolve_token()[0],
        embedder=embedder, vector_store=vector_store)
    # warning, not the SDK's INFO: cmds/serve.py already prints the one banner
    # line a user needs ("MCP server on http://host:port/path"), and uvicorn's
    # own startup banner plus per-request access log would bury the token line
    # printed right beside it. Errors still surface.
    #
    # --access-log is the deliberate exception: this transport is served by
    # uvicorn rather than by http_base's handler, so honouring the flag means
    # letting uvicorn's own access logger through (its format, its stderr) --
    # which is still a real access log where there was none, and the alternative
    # is a flag that silently does nothing on the one server most likely to be
    # left running.
    access = observability.access_log_enabled()
    uvicorn.run(app, host=host, port=port, access_log=access,
                log_level="info" if access else "warning")
