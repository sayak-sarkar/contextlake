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

import functools
import hmac
import json
import os
import re
import secrets
import threading
from collections import deque
from pathlib import Path
from typing import Literal

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

# How many tool bodies may run at once.
#
# The MCP SDK runs every synchronous tool through
# ``anyio.to_thread.run_sync`` with no limiter (func_metadata.call_fn_with_arg_validation),
# so it takes anyio's default of 40 worker threads. Our tool bodies are graph
# traversals over SQLite, and a traversal is not one query -- it is thousands of
# small round trips through the store. Forty threads interleaving those on one
# connection pool spend their time contending rather than working: measured
# against a real index, twenty concurrent expensive calls cost ~1.97s unbounded
# versus ~50ms at a limit of one, and the same traversal over in-memory dicts
# shows a contention ratio of 1.78 against 53.55 for SQLite. The knee is sharp
# between two and four.
#
# Two, not one: the matrix that produced those numbers ran homogeneous batches,
# where serialising is free. Real editor traffic mixes one slow call with many
# fast ones, and at a limit of one a single multi-second traversal holds the
# only token while every cheap lookup queues behind it. Two keeps a slot free
# for the cheap path and still removes ~93% of the contention.
#
# Re-measured on a much larger store (57,720 nodes / 251,738 edges), the knee
# moves left: eight concurrent blast_radius calls cost 204ms at a limit of one,
# 373ms at two, and 8,705ms at eight. So one is the fastest setting on a big
# graph, and the mixed-traffic argument above -- which was never re-measured --
# is the only thing still recommending two. Two remains the default because
# changing it would be a behaviour change for every user justified by a
# homogeneous-batch benchmark; one is a supported, and now safe, choice.
#
# "Now safe" is load-bearing. A limit of one used to HANG the stdio transport
# outright, and it was the bound being applied through the wrong mechanism that
# did it -- see apply_tool_limiter.
TOOL_CONCURRENCY_ENV = "CONTEXTLAKE_MCP_TOOL_CONCURRENCY"
DEFAULT_TOOL_CONCURRENCY = 2

# Worker threads kept back for transport I/O, over and above the tool bound.
# The SDK's stdio transport wraps stdin and stdout with anyio.wrap_file and
# passes no limiter of its own, so its blocking readline, write and flush all
# draw on the same default thread limiter. Two tasks need one each (stdin_reader
# parked in readline, stdout_writer flushing a reply); the rest is headroom.
_TRANSPORT_IO_RESERVE = 4


def resolve_tool_concurrency(explicit: int | None = None) -> int:
    """How many tool bodies may run at once: flag, then env, then the default.

    A non-numeric or non-positive env value is ignored rather than fatal -- this
    is a performance knob on a server an editor launches, and refusing to start
    over a typo in a shell profile is worse than serving at the default.
    """
    if explicit is not None and explicit > 0:
        return explicit
    raw = (os.environ.get(TOOL_CONCURRENCY_ENV) or "").strip()
    if raw:
        try:
            if (n := int(raw)) > 0:
                return n
        except ValueError:
            pass
    return DEFAULT_TOOL_CONCURRENCY


def apply_tool_limiter(limit: int) -> None:
    """Size the SDK's worker pool. Must run inside the async context.

    This used to BE the tool bound: it set anyio's default thread limiter to
    ``limit`` outright. That limiter is not private to tool bodies. The SDK's
    stdio transport wraps stdin and stdout with ``anyio.wrap_file`` and passes no
    limiter, so ``readline``, ``write`` and ``flush`` borrow from the very same
    tokens. At ``limit = 1`` the stdin_reader task sat inside a blocking
    ``readline`` holding the only token and stdout_writer could never acquire one
    to flush a reply: the server started, printed its banner, and then answered
    nothing at all -- no error, no warning, no timeout, on the default and
    most-used transport, at the value the benchmark above recommends.

    The bound now lives on the tool bodies themselves (see build_server), which
    is what it was always trying to express. This function keeps the pool from
    being needlessly wide, and reserves slots the transport can always get.

    anyio's default thread limiter is stored in a run-scoped variable, so it
    only exists once a loop is running and setting it from module scope would
    either fail or configure a limiter nothing uses.
    """
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = (
        limit + _TRANSPORT_IO_RESERVE)

# What the two graph-walk tools say when neither spelling of their one required
# argument arrives. They took `node_id` while every neighbouring tool took `name`
# or `query`, so the obvious first call failed with a raw pydantic validation dump
# -- an error about a schema, addressed to nobody, in place of an instruction.
_NEEDS_SYMBOL = ("Pass the symbol as `node_id` or `name` -- either a node id "
                 "(e.g. 'src/svc.py::CatalogService') or a bare symbol name "
                 "(e.g. 'CatalogService').")

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
    # Retrieval similarity, 0..1 cosine, only on hits that came from a vector
    # search. The retrieval tools computed this and threw it away, so a caller
    # got k nodes ranked "by similarity" with no similarity to read: the ranking
    # was real and the claim was unfalsifiable at the call site. None everywhere
    # a score is not a meaningful property of the hit (graph walks, exact lookups).
    score: float | None = None


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
    # Set when a bare symbol name matched several definitions and one had to be
    # seeded: the result is that one definition's, not the union of all of them.
    note: str | None = None


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
    # Whether the repo asked about is indexed at all. Without it a mistyped repo
    # id and a known repo with no cross-repo edges were the same answer, and the
    # empty one reads as an architectural fact. See RepoLinksOut/OwnersOut for the
    # same field on the rest of this family; get_wiki/get_readme/get_repo_brief
    # already carried it.
    found: bool = True


class BlastHit(BaseModel):
    id: str
    repo: str
    kind: str
    name: str
    hop: int          # distance from the seed (1 = direct caller/dependent)
    via: str          # the relation traversed
    confidence: str   # verify INFERRED / AMBIGUOUS hits against the cited source
    file: str | None = None       # where the affected symbol is defined
    line: int | None = None
    # The call site the edge was read from: the line an agent opens to check an
    # AMBIGUOUS hit rather than taking the label's word for it.
    via_file: str | None = None
    via_line: int | None = None
    # How many same-named definitions this reference could have meant (None when
    # the store predates the stamp). "1 of 5" is a fact; "ambiguous" alone is not.
    name_candidates: int | None = None


class BlastRadiusOut(BaseModel):
    seed: str
    hops: int
    hits: list[BlastHit]
    total: int
    truncated: bool
    # Same disclosure as NodesOut.note: which definition `seed` actually resolved to
    # when the caller passed a bare name that several definitions share.
    note: str | None = None


class PathOut(BaseModel):
    """A path query has three outcomes and a bare node list rendered them
    identically: a real route, an endpoint the graph has never heard of, and two
    indexed nodes with nothing between them. Only the third is what an empty
    answer reads as, and it was the only one the old docstring described."""
    nodes: list[NodeOut]     # src .. dst in order; empty unless `found`
    found: bool              # a path exists within max_hops
    hops: int                # edges traversed (0 when src == dst, or when not found)
    # Why this is not a whole path: which miss occurred when `found` is false, or
    # which part of a real route could not be materialised. None only when
    # `nodes` is the complete route, so an empty `gap` is a claim in its own right.
    gap: str | None = None


class OwnerOut(BaseModel):
    name: str
    commits: int
    lines: int
    last_active: str   # YYYY-MM-DD of the contributor's most recent commit
    share: float       # 0..1 fraction of the recency-weighted score


class OwnersOut(BaseModel):
    scope: str         # repo (optionally repo:sub-path) the ranking is for
    found: bool = True  # False => no repo with that id is indexed (see RepoEdgesOut)
    owners: list[OwnerOut]
    # Why the list is empty, whenever it is. "No git history was read at all"
    # and "the history was read and attributed nobody" are different answers,
    # and an empty list alone cannot tell them apart -- so a caller that
    # narrates the result would have to guess, and the narration would be a
    # provenance claim it never earned. None whenever `owners` is non-empty.
    ranking_gap: str | None = None


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
    found: bool = True  # False => no repo with that id is indexed (see RepoEdgesOut)
    total: int
    links: dict[str, list[LinkOut]]  # relation (tracked_by/documented_by/…) -> links


class DanglingOut(BaseModel):
    repo: str
    src: str
    relation: str
    dst: str


class GraphHealthOut(BaseModel):
    # Whether this store holds any indexed repo at all. Zero stale, zero dangling
    # and zero parser-stale is the exact output of a perfectly healthy fleet, and
    # it was also the output of a store that has never been indexed: the counts
    # were not wrong, they were unqualified. False means every count below is zero
    # because there is nothing to check, not because everything checked out.
    indexed: bool = True
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
    # Carved out of `stale`, not overlapping it. A repository with no commits has
    # no HEAD to compare against, so it matched the staleness test on every run
    # and no amount of re-indexing could clear it; `unreadable` is a repository
    # whose path is gone or that git will not answer for. Both used to be counted
    # and described as "stale, re-run index", which was true of neither.
    empty: int = 0
    empty_repos: list[str] = Field(default_factory=list)
    shard: int = 0
    shard_repos: list[str] = Field(default_factory=list)
    unreadable: int = 0
    unreadable_repos: list[str] = Field(default_factory=list)


class AskOut(BaseModel):
    """One-shot answer envelope: the router picked a substrate and filled the
    matching field. Read ``route`` to know which field holds the answer, and
    ``note`` for what it is and how much to trust it."""
    question: str
    route: str                       # definition|callers|dependents|impact|owners|explain|search
    target: str | None = None        # the symbol / repo the question resolved to
    note: str                        # plain-language: what answered, and the trust label
    # False when nothing satisfied the question AS ASKED -- the graph established a
    # negative (no such definition / no such repo / no query term is indexed at all).
    # Any `nodes` alongside answered=False are leads from a fallback search, never an
    # answer: read `note` for which. An agent must not present them as the answer.
    answered: bool = True
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


# The one direction vocabulary, shared by the four tools that take the parameter
# and matching the store's own (`sqlite_store.neighbors`). `Direction` puts it in
# each tool's advertised input schema, so the SDK refuses an out-of-vocabulary
# value before a handler runs.
Direction = Literal["in", "out", "both"]
_DIRECTIONS = ("in", "out", "both")


def _repo_side(rows: list[dict], repo: str, direction: str) -> list[dict]:
    """Repo->repo edges touching ``repo`` on the side ``direction`` names.

    Raises on a direction outside the vocabulary, exactly as the store's
    ``neighbors`` does for the same parameter. The three architecture tools used
    to build this filter inline, where a value in neither set simply matched no
    branch: a typo'd direction came back as "this repo has no dependencies / no
    HTTP flow / no event flow", which is a positive architectural claim produced
    by an argument the tool had in fact rejected. Same parameter, same three
    legal values, so the same refusal.
    """
    if direction not in _DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r}")
    return [e for e in rows
            if (direction in ("out", "both") and e["src"] == repo)
            or (direction in ("in", "both") and e["dst"] == repo)]


def _node_out(n: Node, *, score: float | None = None) -> NodeOut:
    s = sanitize_label
    attrs = getattr(n, "attrs", None) or {}
    return NodeOut(
        id=s(n.id), repo=s(n.repo), kind=s(n.kind), name=s(n.name),
        qualified_name=s(n.qualified_name) or None, file=s(n.file) or None,
        line_start=n.line_start, line_end=n.line_end, lang=s(n.lang) or None,
        signature=s(attrs["signature"]) if attrs.get("signature") else None,
        doc=s(attrs["doc"]) if attrs.get("doc") else None,
        score=round(score, 4) if score is not None else None,
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
    tool_concurrency: int | None = None,
) -> MCPServer:
    # host/port/stateless_http/json_response moved to run_server()/.run() --
    # this SDK version's server object no longer takes them at construction
    # (they're streamable-http-transport options, not server-identity options).
    from .. import __version__

    mcp = MCPServer(name, instructions=_INSTRUCTIONS, version=__version__)

    # The tool bound, expressed on the thing it is about. Bounding worker threads
    # instead (see apply_tool_limiter) put transport I/O inside the same budget
    # and hung stdio at a limit of one.
    #
    # Registered wrapper vs. bare function is the load-bearing detail: MCPServer.tool
    # returns the original function, so every name in this scope stays unbounded and
    # `ask` -- which calls find_definition, find_callers, blast_radius and others
    # directly -- cannot deadlock against a bound it is already holding. Only calls
    # arriving over the wire go through the wrapper, which is exactly the traffic the
    # measurement was about.
    _tool_slots = threading.Semaphore(resolve_tool_concurrency(tool_concurrency))

    def bounded_tool(fn):
        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            with _tool_slots:
                return fn(*args, **kwargs)

        mcp.add_tool(guarded)
        return fn

    def _term_anchors(query: str) -> tuple[list[str], bool]:
        """This server's binding of the shared relevance floor (see
        :mod:`contextlake.kb.relevance`).

        The predicate started life as a branch inside ``ask``, which left it not
        applying to the two tools with the same problem: a nearest-neighbour search
        has no notion of "nothing matched", so ``semantic_search`` and
        ``hybrid_search`` returned k confident, cited, structurally-valid hits for a
        query with no possible answer, exactly as ``ask`` had. It then lived here as
        a closure, which left ``kb query --retriever semantic`` -- same store, same
        retriever, different surface -- with the identical defect. It is a module now
        so there is one answer per knowledge base rather than one per transport.
        """
        from .relevance import term_anchors

        return term_anchors(store, query)

    def _below_floor(query: str) -> bool:
        """True when not one content term in ``query`` is indexed anywhere."""
        from .relevance import below_floor

        return below_floor(store, query)

    @bounded_tool
    def graph_stats() -> StatsOut:
        """Counts of indexed repos/nodes/edges and the edge-confidence breakdown."""
        st = store.stats()
        return StatsOut(repos=st.repos, nodes=st.nodes, edges=st.edges,
                        by_confidence=st.by_confidence)

    @bounded_tool
    def who_knows(repo: str, path: str | None = None, limit: int = 10) -> OwnersOut:
        """Likely owners / subject-matter experts for `repo` (optionally a sub-`path`).

        Ranked from the repo's git commit history by a recency-weighted blend of
        commit volume and lines changed, so recent active contributors outrank a
        long-departed prolific author. Names are as committed in the local mirror.
        ``found=False`` when no repo with that id is indexed at all.
        """
        from .ownership import compute_owners
        r = store.get_repo(repo)
        scope = sanitize_label(repo + (f":{path}" if path else ""))
        if not r:
            # An unknown repo id, which the "no local clone" wording below would
            # have misreported as an indexed repo that simply has no checkout.
            return OwnersOut(scope=scope, found=False, owners=[], ranking_gap=(
                "no repository with this id is indexed, so there was nothing to "
                "read a history from"))
        if not r.path:
            # Nothing ran: no local clone is on record, so no git command was
            # ever issued. Returning a bare empty list here let the caller
            # narrate it as a completed ranking (see `ask`'s owners route),
            # which asserted a provenance this branch never obtained.
            return OwnersOut(scope=scope, owners=[], ranking_gap=(
                "no local clone is on record for this repo, so its git history "
                "was never read"))
        owners = compute_owners(r.path, path, limit=max(1, min(limit, 50)))
        return OwnersOut(scope=scope, ranking_gap=(
            None if owners else
            "its git history was read but attributed no commits to anyone"), owners=[
            OwnerOut(name=sanitize_label(o.name), commits=o.commits, lines=o.lines,
                     last_active=o.last_active, share=round(o.share, 4))
            for o in owners])

    @bounded_tool
    def get_node(node_id: str) -> NodeOut | None:
        """Fetch a single graph node by its id."""
        n = store.get_node(node_id)
        return _node_out(n) if n else None

    @bounded_tool
    def get_neighbors(
        node_id: str, relation: str | None = None, direction: Direction = "both",
        limit: int = 50
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

    @bounded_tool
    def search_code(
        query: str, kind: str | None = None, repo: str | None = None, limit: int = 20
    ) -> list[NodeOut]:
        """Search the graph for nodes by name/symbol, with optional kind and repo filters."""
        return [_node_out(n) for n in store.search(query, kind=kind, repo=repo, limit=limit)]

    @bounded_tool
    def find_definition(
        name: str, kind: str | None = None, repo: str | None = None
    ) -> list[NodeOut]:
        """Find definition(s) with an exact name — 'where is X defined?'."""
        return [_node_out(n) for n in store.nodes_by_name(name, kind=kind, repo=repo)]

    def _one_of(*values: str | None) -> str | None:
        """The first non-empty of several spellings of the same argument."""
        return next((v for v in values if v and v.strip()), None)

    def _as_node_id(node_id_or_name: str) -> tuple[str | None, str | None]:
        """Accept a node id OR a bare symbol name, plus the disclosure it needs.

        Agents (and humans) naturally pass a name like ``CatalogService``; resolve
        it to the first matching node id so callers/impact work without a separate
        find_definition round-trip. An exact node id is returned as-is; an unknown
        string yields None.

        The second element is the "N matched, used the first" caveat, from the same
        ``chosen_one_of`` every other surface uses. It exists because the count was
        being computed and thrown away here: a bare name with five same-named
        definitions seeded one of them, and find_callers/blast_radius returned the
        result with nothing to say so, while the `ask` envelope over the very same
        resolution disclosed it. Two surfaces, one store, one question, and only one
        of them mentioned that the answer was about a symbol the caller did not pick.
        """
        if not node_id_or_name:
            return None, None
        if store.get_node(node_id_or_name):
            return node_id_or_name, None
        matches = store.nodes_by_name(node_id_or_name)
        if not matches:
            return None, None
        from .impact import chosen_one_of
        return matches[0].id, chosen_one_of(node_id_or_name, len(matches)) or None

    @bounded_tool
    def find_callers(node_id: str | None = None, name: str | None = None,
                     limit: int = 50) -> NodesOut:
        """Find the definitions that call a node — 'who calls X?' (incoming calls edges).

        Pass the symbol as `node_id` **or** `name`: either a node id or a bare symbol
        name (e.g. ``CatalogService``), resolved to its first matching definition.
        EXTRACTED-first, capped at `limit`; `truncated`/`total` flag hot symbols with
        more callers than returned.
        """
        node_id = _one_of(node_id, name)
        if node_id is None:
            return NodesOut(nodes=[], total=0, truncated=False, note=_NEEDS_SYMBOL)
        nid, why = _as_node_id(node_id)
        if nid is None:
            # Same distinction `find_dependents` already draws, in the same words:
            # "no such symbol is indexed" and "nothing calls this symbol" are
            # different facts, and the second is the more reassuring one to get
            # wrong. An empty list alone cannot tell them apart.
            return NodesOut(nodes=[], total=0, truncated=False,
                            note=f"No indexed symbol named {node_id!r}.")
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
        return NodesOut(nodes=kept, total=total, truncated=truncated,
                        note=(f"Callers of {node_id!r}{why}." if why else None))

    @bounded_tool
    def find_dependents(package: str, limit: int = 50,
                        repo: str | None = None) -> NodesOut:
        """Find files/repos that depend on a package — cross-repo 'who uses X?'.

        `repo` scopes the answer to dependents inside one repository (the package
        node itself is shared across repos, so it is the dependents that get
        filtered). Capped at `limit`; `truncated`/`total` flag widely-used packages.

        An unknown package returns `note` saying so, rather than an empty list that
        reads as "nothing depends on it".
        """
        pkgs = store.nodes_by_name(package, kind="package")
        if not pkgs:
            # "no such package is indexed" and "this package has no dependents" are
            # different facts, and the second is the more reassuring one to get
            # wrong. An empty list alone cannot tell them apart.
            return NodesOut(nodes=[], total=0, truncated=False,
                            note=f"No indexed package named {package!r}.")
        seen: set[str] = set()
        out: list[NodeOut] = []
        for pkg in pkgs:
            for e in store.neighbors(pkg.id, relation="depends_on", direction="in"):
                if e.src in seen:
                    continue
                seen.add(e.src)
                n = store.get_node(e.src)
                if n and (repo is None or n.repo == repo):
                    out.append(_node_out(n))
        kept, total, truncated = _budget(out, limit)
        return NodesOut(nodes=kept, total=total, truncated=truncated)

    @bounded_tool
    def repo_dependencies(repo: str, direction: Direction = "both",
                          limit: int = 50) -> RepoEdgesOut:
        """Repo→repo package dependencies for `repo` (the cross-repo architecture map).

        From the package two-hop (publishes ⨝ depends_on): edges are
        ``dependent --depends_on--> publisher``, weight = shared package count.
        direction: out (what `repo` depends on) | in (who depends on `repo`) | both.
        INFERRED, manifest-derived — a likely undercount; verify against the cited repo.
        ``found=False`` when no repo with that id is indexed, so a mistyped id is
        not read as "this repo depends on nothing".
        """
        from .arch.resolve import repo_dependency_edges
        rows = _repo_side(repo_dependency_edges(store), repo, direction)
        rows.sort(key=lambda e: -e["weight"])
        kept, total, truncated = _budget(rows, limit)
        return RepoEdgesOut(total=total, truncated=truncated,
                            found=store.get_repo(repo) is not None, edges=[
            RepoEdgeOut(src=sanitize_label(e["src"]), dst=sanitize_label(e["dst"]),
                        relation=e["relation"], confidence=e["confidence"],
                        weight=e["weight"]) for e in kept])

    @bounded_tool
    def repo_flow(repo: str, direction: Direction = "both",
                  limit: int = 50) -> RepoEdgesOut:
        """Repo→repo HTTP request flow for `repo` (who calls whom over HTTP).

        From the endpoint two-hop (exposes ⨝ calls_http): edges are
        ``caller --flow--> exposer`` (the direction a request travels), weight =
        shared endpoint count. direction: out (endpoints `repo` calls) | in (callers
        of `repo`'s endpoints) | both. INFERRED, regex+path-matched — an undercount
        that omits async/event coupling; verify against the cited repo.
        ``found=False`` when no repo with that id is indexed.
        """
        from .arch.resolve import repo_http_flow_edges
        rows = _repo_side(repo_http_flow_edges(store), repo, direction)
        rows.sort(key=lambda e: -e["weight"])
        kept, total, truncated = _budget(rows, limit)
        return RepoEdgesOut(total=total, truncated=truncated,
                            found=store.get_repo(repo) is not None, edges=[
            RepoEdgeOut(src=sanitize_label(e["src"]), dst=sanitize_label(e["dst"]),
                        relation=e["relation"], confidence=e["confidence"],
                        weight=e["weight"], context=e.get("context")) for e in kept])

    @bounded_tool
    def repo_event_flow(repo: str, direction: Direction = "both",
                        limit: int = 50) -> RepoEdgesOut:
        """Repo→repo EVENT flow for `repo` (who publishes events that whom consumes).

        From the topic two-hop (publishes_event ⨝ consumes_event): edges are
        ``publisher --flow--> consumer`` (the direction an event travels), weight =
        shared topic count. direction: out (topics `repo` publishes that others consume)
        | in (publishers `repo` consumes from) | both. INFERRED, regex-detected literal
        topics — an undercount that omits config-variable topics; verify against the repo.
        ``found=False`` when no repo with that id is indexed.
        """
        from .arch.resolve import repo_event_flow_edges
        rows = _repo_side(repo_event_flow_edges(store), repo, direction)
        rows.sort(key=lambda e: -e["weight"])
        kept, total, truncated = _budget(rows, limit)
        return RepoEdgesOut(total=total, truncated=truncated,
                            found=store.get_repo(repo) is not None, edges=[
            RepoEdgeOut(src=sanitize_label(e["src"]), dst=sanitize_label(e["dst"]),
                        relation=e["relation"], confidence=e["confidence"],
                        weight=e["weight"], context=e.get("context")) for e in kept])

    @bounded_tool
    def blast_radius(node_id: str | None = None, name: str | None = None, hops: int = 3,
                     relations: list[str] | None = None,
                     limit: int = 100) -> BlastRadiusOut:
        """What could break if you change this node — bounded transitive REVERSE reach.

        Pass the symbol as `node_id` **or** `name`: either a node id or a bare symbol
        name (e.g. ``CatalogService``),
        resolved to its first matching definition. Walks INCOMING edges (who calls /
        depends on / subclasses the node) breadth-first up to `hops`, capped at
        `limit`, over `relations` (default calls + depends_on + inherits).
        Each hit carries its hop distance, the relation, and confidence —
        EXTRACTED-first; verify INFERRED/AMBIGUOUS against the cited source. A
        bounded impact slice, never an exhaustive guarantee (`truncated` says when
        the cap was hit). An unresolvable symbol returns `note` saying so, rather
        than an empty result that reads as "nothing depends on this".
        """
        from .impact import blast_radius as _blast
        node_id = _one_of(node_id, name)
        if node_id is None:
            return BlastRadiusOut(seed="", hops=hops, hits=[], total=0, truncated=False,
                                  note=_NEEDS_SYMBOL)
        if hops < 0:
            # A negative reach is not a smaller question, it is not a question:
            # the walk stops immediately and the empty result reads as "nothing is
            # affected". Zero is a real request (look zero hops out) and keeps
            # answering emptily; below zero is refused.
            raise ValueError(f"hops must be 0 or greater, not {hops}")
        resolved, why = _as_node_id(node_id)
        if resolved is None:
            # The unresolved string used to be used as the seed anyway, so an
            # unknown symbol came back as a well-formed, non-error, bounded impact
            # analysis of a symbol that does not exist. "Nothing depends on this,
            # safe to change" and "I have never heard of this symbol" are opposite
            # answers to a question about whether a change is safe.
            return BlastRadiusOut(seed="", hops=hops, hits=[], total=0, truncated=False,
                                  note=f"No indexed symbol named {node_id!r}.")
        nid = resolved
        hits, truncated = _blast(store, nid, hops=hops, relations=relations, limit=limit)
        return BlastRadiusOut(
            seed=nid, hops=hops, total=len(hits), truncated=truncated,
            note=(f"Blast radius of {node_id!r}{why}." if why else None),
            hits=[BlastHit(id=sanitize_label(h.id), repo=sanitize_label(h.repo),
                           kind=sanitize_label(h.kind), name=sanitize_label(h.name),
                           hop=h.hop, via=sanitize_label(h.via), confidence=h.confidence,
                           file=sanitize_label(h.file) if h.file else None, line=h.line,
                           via_file=sanitize_label(h.via_file) if h.via_file else None,
                           via_line=h.via_line, name_candidates=h.name_candidates)
                  for h in hits])

    def _cluster_is_stale(page: str, namespace: str) -> bool:
        """Whether a cluster page describes members that have since moved on.

        Derived, not asserted. This used to be hardcoded to False, so an agent
        filtering on ``stale`` treated a page nothing had checked as verified
        fresh -- and a cluster page whose members were long gone read as current.
        The page already carries the freshness stamp the generator skips on: the
        fingerprint of its members' (repo, head) pairs. Recompute it from the
        store and compare, which is exactly what the repo path does with a single
        commit. Fails closed for the same reason it does: a page with no stamp is
        one nothing can be compared against, which is not the same as fresh.
        """
        from .wiki.cluster import cluster_fingerprint, members

        stamped = re.search(r"cluster-commits: ([0-9a-f]+)", page)
        if not stamped:
            return True
        heads = {}
        for rid in members(store, namespace):
            r = store.get_repo(rid)
            if r is not None and r.head_commit:
                heads[rid] = r.head_commit
        return stamped.group(1) != cluster_fingerprint({"heads": heads})

    @bounded_tool
    def get_wiki(repo: str) -> WikiOut:
        """The generated LLM-wiki page for a repo, or a namespace's cluster page.

        Pass a repo id for its page, or a namespace prefix (e.g. ``team/api``)
        for the cluster page narrating that group's cross-repo coupling.

        **Advisory, not ground truth** — synthesized text to verify against the
        cited sources/graph; it never outranks EXTRACTED facts. ``stale`` is true
        when a repo wiki was generated from a different commit than the repo's
        current indexed head (or either is unknown), so an agent never cites prose
        that describes code which has since changed. A cluster page is checked the
        same way against its own member-commit fingerprint rather than one commit,
        so ``wiki_commit``/``current_commit`` stay null on that kind while
        ``stale`` still means what it says.
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
                return WikiOut(repo=sanitize_label(repo), found=True,
                               stale=_cluster_is_stale(craw, repo),
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

    @bounded_tool
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

    @bounded_tool
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

    @bounded_tool
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

    @bounded_tool
    def get_repo_links(repo: str) -> RepoLinksOut:
        """A repo's cross-links to external knowledge — Jira / Confluence / Figma /
        GitLab / Slack — grouped by relation (tracked_by / documented_by /
        designed_in / has_merge_request / has_issue / touched_by / discussed_in /
        referenced_in). Populated by `connect`; served offline after.
        ``found=False`` when no repo with that id is indexed, so a mistyped id is
        not read as "this repo has no external links".
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
        return RepoLinksOut(repo=sanitize_label(repo), total=total, links=grouped,
                            found=store.get_repo(repo) is not None)

    @bounded_tool
    def graph_health() -> GraphHealthOut:
        """Knowledge-graph health — stale repos (local HEAD moved past the index),
        repos whose graph an older parser built (re-index to refresh), and dangling
        edges (pointing at a missing node). The dashboard's health panel; offline
        (reads local git HEADs).

        Read ``indexed`` before the counts: a store that has never been indexed
        reports zero of everything, which is also what a perfectly healthy fleet
        reports. ``indexed=false`` says the zeros mean "nothing to check".

        ``empty`` (no commits), ``shard`` (imported from a graph shard, so it never
        had a checkout) and ``unreadable`` (its path is gone, or git will not
        answer for it) are reported apart from ``stale``: re-indexing clears a
        stale repository and cannot clear any of those.
        """
        from .commands import lint_result
        sp = getattr(store, "path", None)
        res = lint_result(store, Path(sp).parent) if sp else {
            # A pathless store has no local HEAD to read and no shard to open, so
            # nothing can be checked -- but how many repos it holds is still
            # knowable, and zeroing that alongside the checks turned "nothing was
            # checked" into "there is nothing here", which is a different answer.
            "repos": len(store.list_repos()), "checked": 0, "stale": 0, "dangling": 0,
            "parser_stale": 0, "empty": 0, "unreadable": 0, "shard": 0,
            "stale_repos": [], "empty_repos": [], "shard_repos": [],
            "unreadable_repos": [], "parser_stale_repos": [],
            "dangling_sample": []}
        return GraphHealthOut(
            # lint_result's own `repos` is len(store.list_repos()), so this is the
            # store's repo count and costs nothing extra.
            indexed=res["repos"] > 0,
            repos=res["repos"], checked=res["checked"], stale=res["stale"],
            dangling=res["dangling"], parser_stale=res["parser_stale"],
            empty=res["empty"], unreadable=res["unreadable"], shard=res["shard"],
            stale_repos=[sanitize_label(x) for x in res["stale_repos"]],
            empty_repos=[sanitize_label(x) for x in res["empty_repos"]],
            shard_repos=[sanitize_label(x) for x in res["shard_repos"]],
            unreadable_repos=[sanitize_label(d["repo"]) for d in res["unreadable_repos"]],
            parser_stale_repos=[sanitize_label(x) for x in res["parser_stale_repos"]],
            dangling_sample=[DanglingOut(
                repo=sanitize_label(d["repo"]), src=sanitize_label(d["src"]),
                relation=d["relation"], dst=sanitize_label(d["dst"]))
                for d in res["dangling_sample"]])

    @bounded_tool
    def shortest_path(src_id: str, dst_id: str, max_hops: int = 6) -> PathOut:
        """Shortest path between two nodes over the graph (<= max_hops).

        `found` says whether there is one; when there is not, `gap` says which of
        the two reasons applies: an id the graph does not hold, or two indexed
        nodes with no route between them inside `max_hops`. This used to return a
        bare list, which rendered a typo and a genuine disconnection identically.
        """
        absent = [f"{label}={nid!r}" for label, nid in
                  (("src_id", src_id), ("dst_id", dst_id)) if not store.get_node(nid)]
        if absent:
            return PathOut(nodes=[], found=False, hops=0,
                           gap="no indexed node with " + " or ".join(absent))
        path_ids = _bfs_path(store, src_id, dst_id, max_hops)
        if not path_ids:
            return PathOut(nodes=[], found=False, hops=0,
                           gap=f"both nodes are indexed, but no path of {max_hops} hop(s) "
                               "or fewer connects them")
        # The walk follows edge endpoints, and an edge can name a node the graph
        # no longer holds -- that is the "dangling" condition `graph_health`
        # counts, so it is a real state, not a hypothetical one. Dropping such a
        # node quietly leaves two nodes that were never adjacent sitting next to
        # each other in the list, under a `found` that says the traversal
        # completed. Report the route's true length and say what is missing from
        # it rather than shortening the path to fit what could be materialised.
        nodes = [_node_out(n) for nid in path_ids if (n := store.get_node(nid))]
        absent_on_path = len(path_ids) - len(nodes)
        return PathOut(
            nodes=nodes, found=True, hops=len(path_ids) - 1,
            gap=(None if not absent_on_path else
                 f"{absent_on_path} node(s) on this path are named by an edge but "
                 "are not in the graph, so `nodes` is shorter than `hops`: the "
                 "adjacencies it shows are not all real ones"))

    if embedder is not None and vector_store is not None:
        @bounded_tool
        def semantic_search(query: str, k: int = 10, repo: str | None = None) -> list[NodeOut]:
            """Semantic (embedding) search over indexed nodes — for natural-language
            queries where exact names are unknown. Results are ranked by similarity,
            and each hit carries its `score` (0..1 cosine) so you can judge the
            ranking rather than take it on trust. Empty when nothing in the query is
            indexed at all: a nearest-neighbour index has no concept of "no match"
            and would otherwise return its k nearest regardless.
            Hits of kind 'wiki'/'document' are ADVISORY prose (LLM-generated or
            ingested), not extracted code facts — verify against the cited file."""
            if not query.strip():
                return []  # matches search_code's empty-query handling, not a crash
            if _below_floor(query):
                return []
            vec = embedder.embed([query])[0]
            out: list[NodeOut] = []
            for node_id, score in vector_store.search(vec, k=k, repo=repo):
                n = store.get_node(node_id)
                if n:
                    out.append(_node_out(n, score=score))
            return out

        @bounded_tool
        def hybrid_search(query: str, k: int = 10, repo: str | None = None) -> list[NodeOut]:
            """Hybrid retrieval: seed with embeddings, then rank by Personalized
            PageRank over the graph. Surfaces structurally-related nodes (callers,
            dependents) that a pure semantic match would miss.

            Each hit carries its `score` (the PageRank mass it was ranked by, not a
            cosine). Empty when nothing in the query is indexed at all."""
            from .embeddings.hybrid import hybrid_search as _hybrid

            if not query.strip():
                return []  # matches search_code's empty-query handling, not a crash
            if _below_floor(query):
                return []
            out: list[NodeOut] = []
            for node_id, score in _hybrid(store, vector_store, embedder, query, k=k, repo=repo):
                n = store.get_node(node_id)
                if n:
                    out.append(_node_out(n, score=score))
            return out

    @bounded_tool
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
            SUBCLASSES,
            classify,
        )

        route, target = classify(question)
        # A negative the graph actually PROVED, kept alive to the end of the handler.
        # The definition and explain routes used to overwrite `route` with SEARCH on a
        # miss and re-answer by embedding the whole question, which deleted the one
        # fact the graph had established -- that nothing by that name is indexed --
        # before the answer left the server. `route` is no longer rewritten.
        established: str | None = None

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
            # One phrasing for this disclosure across every surface -- see
            # impact.chosen_one_of; `kb impact` says the same sentence.
            from .impact import chosen_one_of
            return matches[0].id, chosen_one_of(name, len(matches)) or None

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
            # Still fall through to a search (the definition rule also fires on genuine
            # prose questions like "where is configuration loaded"), but carry the miss
            # out with the answer instead of discarding it.
            established = (f"No definition named {target!r} is indexed." if target
                           else "No symbol was found in the question to look up.")

        if route == CALLERS:
            nid, why = _resolve_id(target)
            if nid is None:
                return _out(f"Couldn't resolve a symbol to find callers of — {why}.")
            res = find_callers(nid, limit=k)
            return _out(f"Callers of {target!r} — incoming calls, EXTRACTED-first"
                        + (why or "") + ".", nodes=res.nodes, truncated=res.truncated)

        if route == DEPENDENTS:
            # The only branch here that used to skip resolution entirely, so an
            # unindexed package came back as an empty list under a note asserting
            # manifest provenance -- telling an agent "nothing depends on it" when
            # the truth was "no such package is indexed", and citing manifests that
            # were never read. With no target at all it printed the word None.
            # Its three sibling branches already had the right shape.
            if not target:
                return _out("Couldn't tell which package to find dependents of "
                            "-- no package named in the question.", answered=False)
            res = find_dependents(target, limit=k, repo=repo)
            if res.note:
                return _out(f"Couldn't resolve a package to find dependents of -- "
                            f"{res.note[0].lower()}{res.note[1:-1]}.", answered=False)
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
            # `k` is advertised on `ask` and honoured by callers, subclasses and
            # dependents; this route dropped it and let blast_radius apply its own
            # default of 100, so an agent asking for one result could be handed a
            # hundred. Capping means truncation is now ordinary rather than rare,
            # so the count says when it is a slice instead of stating a total the
            # cap decided.
            res = blast_radius(nid, hops=3, limit=k)
            reach = (f"the first {res.total} node(s)" if res.truncated
                     else f"{res.total} node(s)")
            return _out(f"Blast radius of {target!r}: {reach} within 3 hops"
                        + (why or "") + ". Reverse reach over calls+depends_on+inherits; "
                        "INFERRED/AMBIGUOUS hits may under- or over-count — verify.",
                        blast=res, truncated=res.truncated)

        if route == OWNERS:
            rid, why = _resolve_repo(target)
            if rid is None:
                return _out(f"Couldn't tell which repo to find owners for — {why}.")
            res = who_knows(rid, limit=k)
            if not res.owners:
                # Derived from whether the ranking actually happened, never
                # asserted: `who_knows` returns early -- before a single git
                # command runs -- for a repo with no clone on record, and this
                # line used to append ", ranked from git history." to that
                # empty result all the same. Nothing was ranked; say which of
                # the two ways that came about.
                return _out(f"No owners could be ranked for {target!r}" + (why or "")
                            + f" — {res.ranking_gap}.", owners=res, answered=False)
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
            # Not a repo we know. Degrade to a search, but say what was established:
            # the question named something, and no indexed repo matches it.
            established = (f"No indexed repo matching {target!r}." if target
                           else "No repo was named in the question.")

        # SEARCH: the classified route for an open question, and the fallback after a
        # definition/explain miss. `route` keeps its classified value either way -- see
        # `established` above for why it is no longer rewritten to SEARCH here.
        #
        # The relevance floor. A nearest-neighbour search has no notion of "nothing
        # matched": it returns the top k however far away they are, which is how a
        # question about a technology that appears nowhere in the fleet came back with
        # eight real, resolvable, unrelated citations. A cosine floor was measured and
        # rejected (see below); the floor is lexical instead: probe the name index for
        # each of the question's content terms and refuse to present an answer built
        # only on terms the graph has never seen.
        #
        # Measured (this repo indexed and embedded, 4752 vectors, potion-base-8M): over
        # 22 questions whose answer IS in the store and 10 whose answer is not, no
        # cosine statistic separated them -- best-hit similarity, best-vs-median-of-top-50,
        # and a z-score against the full background distribution all had the two classes
        # overlapping (present-min 0.356 sat BELOW absent-max 0.554 on raw cosine). Term
        # presence separated cleanly: 0 of 22 present questions were refused, and the
        # absent ones either refused outright or had their unmatched terms named. The
        # lexical probe is also embedder-independent, which a tuned constant is not:
        # this ships four embedding providers and a constant fitted to one is silently
        # wrong for the other three.
        #
        # Known limitation, measured rather than assumed: the probe is the FTS index,
        # which covers name/qualified_name/file but NOT docstrings, while node_text()
        # does embed docstrings. Of 15 rare words sampled from this repo's own prose,
        # 4 (heuristic, interleaved, corrupting, contention) are in embedded text and
        # absent from the name index, so a question whose ONLY terms are such words is
        # refused although the vectors could have answered. Two things keep that
        # acceptable: the refusal names the exact terms, so it is checkable and
        # retryable rather than silent; and one indexed term anywhere in the question
        # is enough to let the hits through, which is why 0 of 22 realistic
        # multi-word questions were refused. Widening the probe properly means
        # indexing docstrings in FTS -- a schema change whose behaviour would then
        # differ between reindexed and not-yet-reindexed stores, which is worse.
        # The predicate is shared with semantic_search and hybrid_search rather than
        # living here: written as a branch inside ask, it left the two tools with
        # the identical defect untouched.
        unmatched, anchored = _term_anchors(question)

        if question.strip() and embedder is not None and vector_store is not None:
            vec = embedder.embed([question])[0]
            out: list[NodeOut] = []
            for nid, _s in vector_store.search(vec, k=k, repo=repo):
                n = store.get_node(nid)
                if n:
                    out.append(_node_out(n, score=_s))
            found = ("Semantic search over the graph (names + signatures + docstrings); "
                     "'wiki'/'document' hits are ADVISORY.")
        else:
            out = search_code(question, repo=repo, limit=k)
            found = "Full-text search over node names (no embeddings configured)."

        # The unmatched terms are named on every path that reaches here, so the same
        # question gets the same disclosure whether it arrived as an open search or as
        # a definition/explain miss.
        missing = (f" No indexed symbol matches {', '.join(repr(t) for t in unmatched)}."
                   if unmatched else "")
        lead = ((established or "") + missing).strip()

        # Below the floor: not one term in the question is indexed. Return no nodes
        # rather than the nearest k -- which is exactly what the lexical tools already
        # do, and what makes them right where this path was wrong.
        if unmatched and not anchored:
            return _out(lead + " No nodes are returned: a nearest-neighbour search "
                        "would still rank a top k, and none of it would be about the "
                        "question.", answered=False, nodes=[])
        if lead:
            tail = (" " + found + " The hits below matched the question's other terms;"
                    " they are leads, NOT an answer to the question as asked."
                    if out else " A fallback search over the whole question found "
                    "nothing either.")
            return _out(lead + tail, answered=False, nodes=out)
        if not out:
            return _out("Nothing in the knowledge base matched this question. "
                        + found, answered=False, nodes=[])
        return _out(found + " No exact route matched.", nodes=out)

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


class _QuietSseRejection:
    """Stops a correctly-refused SSE request from being logged as a crash.

    The SDK's ``connect_sse`` (``mcp/server/sse.py``) sends the Host/Origin
    rejection response and *then* raises ``ValueError("Request validation
    failed")``, so a request the server refused exactly as designed unwinds into
    uvicorn's ASGI error handler and prints a ~50-line traceback. Measured: the
    same three hostile-header probes produced 3 tracebacks and 165 stderr lines
    over SSE against 0 and 10 over streamable HTTP, whose SDK path returns the
    rejection instead of raising. The client-visible 403/421 is identical either
    way; the cost is operator noise, and a log-based alert on "Exception in ASGI
    application" firing on traffic that was handled perfectly.

    Narrow on two axes deliberately. It swallows only once a response has
    already started -- the client has its 403/421 and there is nothing left to
    report -- and only for that exact message, so if the SDK ever reworks this
    path the behaviour degrades to today's noisy traceback rather than to an
    error silently hidden. Anything else, including a ``ValueError`` raised
    before a response went out, propagates untouched.

    SSE only: the streamable-HTTP path does not raise, so wrapping it would add
    a guard with nothing to guard against.
    """

    _MESSAGE = "Request validation failed"

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def _send(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except ValueError as exc:
            if not (started and str(exc) == self._MESSAGE):
                raise


def build_http_app(
    store: Store, *, transport: str, host: str, token: str,
    embedder=None, vector_store=None, tool_concurrency: int | None = None,
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
    tool_concurrency = resolve_tool_concurrency(tool_concurrency)
    server = build_server(store, embedder=embedder, vector_store=vector_store,
                          tool_concurrency=tool_concurrency)
    security = transport_security(host)
    if transport == "sse":
        app = _QuietSseRejection(server.sse_app(transport_security=security, host=host))
    else:
        app = server.streamable_http_app(
            stateless_http=True, json_response=True,
            transport_security=security, host=host)
    return BearerAuthMiddleware(_ToolLimiterLifespan(app, tool_concurrency), token)


class _ToolLimiterLifespan:
    """Applies the tool-concurrency bound on ASGI lifespan startup.

    uvicorn owns the event loop for the HTTP transports, so there is no
    ``anyio.run`` of ours to set the limiter in -- and the limiter is
    run-scoped, so it has to be set inside that loop. Lifespan startup is the
    first thing that runs there.

    Wrapping rather than passing ``lifespan=`` to the SDK: that app already has
    a lifespan managing its session manager, and supplying one would replace it.
    """

    def __init__(self, app, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "lifespan":
            async def _receive():
                message = await receive()
                if message.get("type") == "lifespan.startup":
                    apply_tool_limiter(self.limit)
                return message

            await self.app(scope, _receive, send)
            return
        await self.app(scope, receive, send)


async def _run_stdio(server, limit: int) -> None:
    """The stdio server, plus the two things that need a running loop.

    ``.run(transport="stdio")`` is just ``anyio.run(self.run_stdio_async)``, so
    replacing it costs nothing and buys the only place where the tool limiter
    (run-scoped) and asyncio's signal handling (loop-scoped) can be installed.

    SIGTERM is what a supervisor sends first, and Python's default action for it
    is to die on the spot: the ``finally`` in cmds/serve.py never ran and the
    store was never closed. ``add_signal_handler`` installs asyncio's self-pipe
    wakeup fd, which wakes the selector the main thread parks in, so the request
    loop unwinds normally and that ``finally`` gets to run.

    SIGINT is deliberately left alone. It already arrives as KeyboardInterrupt,
    which cmds/serve.py catches to set ``interrupted`` and skip a noisy
    interpreter shutdown; routing it through a cancel scope here would leave
    that flag False and quietly reintroduce the traceback that fix removed.
    """
    apply_tool_limiter(limit)
    try:
        import asyncio
        import signal

        # Turn SIGTERM into the interrupt SIGINT already is, so both stop the
        # server through the single shutdown path in cmds/serve.py. That handler
        # closes the store and the vector store and then deliberately skips the
        # interpreter's remaining shutdown, because the SDK's stdio transport
        # leaves a non-daemon thread blocked on a stdin read that never returns
        # while the pipe is open. Anything that tries to unwind "cleanly"
        # instead waits on that thread forever -- measured: a task group
        # cancelled on SIGTERM hung rather than exiting.
        #
        # add_signal_handler, not signal.signal: it installs asyncio's self-pipe
        # wakeup fd, which is what wakes the selector the main thread parks in
        # while idle. Without it the handler does not run until something else
        # happens to wake the loop, which on an idle server may be never.
        #
        # The callback raises KeyboardInterrupt itself rather than re-raising
        # SIGINT: asyncio's Handle._run lets BaseException through, so the
        # exception propagates out of the loop and out of anyio.run to that
        # handler. signal.raise_signal(SIGINT) was tried first and does not work
        # here -- the callback ran and returned with no exception raised, and
        # the process hung.
        # SIGINT gets the same treatment, and for the same reason. Ctrl-C on an
        # *idle* stdio server did nothing at all: Python only runs a signal
        # handler at a bytecode boundary in the main thread, and that thread is
        # parked in the selector with no work coming, so the interrupt sat
        # unhandled until some traffic happened to arrive. Measured on the
        # unmodified server, one SIGINT and three rapid ones both hung.
        #
        # The callback raises the same KeyboardInterrupt the default handler
        # would, so cmds/serve.py still sets `interrupted` and still skips the
        # thread join -- the three-rapid-Ctrl-C fix documented there keeps
        # working rather than being quietly traded away for this one.
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, _interrupt_on_signal)
        loop.add_signal_handler(signal.SIGINT, _interrupt_on_signal)
    except (AttributeError, NotImplementedError, ValueError, RuntimeError):
        # No SIGTERM to install here (Windows, or not the main thread). The
        # server still runs; terminate just keeps its old default action.
        pass

    await server.run_stdio_async()


def _interrupt_on_signal() -> None:
    raise KeyboardInterrupt


def run_server(
    store: Store, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765,
    embedder=None, vector_store=None, token: str | None = None,
    tool_concurrency: int | None = None,
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
    limit = resolve_tool_concurrency(tool_concurrency)
    if transport not in HTTP_TRANSPORTS:
        import anyio

        server = build_server(store, embedder=embedder, vector_store=vector_store,
                              tool_concurrency=limit)
        anyio.run(_run_stdio, server, limit)
        return

    import uvicorn

    # uvicorn installs its own SIGTERM/SIGINT handlers and shuts down gracefully
    # on both (verified), so the signal work above is stdio-only -- double
    # handling here would break the shutdown that already works.
    app = build_http_app(
        store, transport=transport, host=host, token=token or resolve_token()[0],
        embedder=embedder, vector_store=vector_store, tool_concurrency=limit)
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
