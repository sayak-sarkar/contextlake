"""MCP server for the knowledge layer.

Exposes the graph to AI agents as MCP *tools* (model-invoked) over stdio or
Streamable HTTP. Every text field returned is passed through ``sanitize_label``
first, so hostile repo content can't inject into an agent's context. Results are
structured + cited (each edge carries its source file and verified date); the
graph is an index, so inferred edges should be verified against the source.
"""

from __future__ import annotations

import json
from collections import deque

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
    store: Store, *, name: str = "gitlab-sync-kb", host: str = "127.0.0.1", port: int = 8765
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
        node_id: str, relation: str | None = None, direction: str = "both"
    ) -> list[EdgeOut]:
        """List edges incident to a node. direction: in | out | both; optional relation filter."""
        return [
            _edge_out(e)
            for e in store.neighbors(node_id, relation=relation, direction=direction)
        ]

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
    def find_callers(node_id: str) -> list[NodeOut]:
        """Find the definitions that call a node — 'who calls X?' (incoming calls edges)."""
        seen: set[str] = set()
        out: list[NodeOut] = []
        for e in store.neighbors(node_id, relation="calls", direction="in"):
            if e.src in seen:
                continue
            seen.add(e.src)
            n = store.get_node(e.src)
            if n:
                out.append(_node_out(n))
        return out

    @mcp.tool()
    def shortest_path(src_id: str, dst_id: str, max_hops: int = 6) -> list[NodeOut]:
        """Shortest path between two nodes over the graph (<= max_hops). Empty if none."""
        path_ids = _bfs_path(store, src_id, dst_id, max_hops)
        return [_node_out(n) for nid in path_ids if (n := store.get_node(nid))]

    @mcp.resource("kb://stats")
    def stats_resource() -> str:
        st = store.stats()
        return json.dumps(
            {"repos": st.repos, "nodes": st.nodes, "edges": st.edges,
             "by_confidence": st.by_confidence}
        )

    return mcp


def run_server(
    store: Store, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765
) -> None:
    """Build and run the MCP server (blocking)."""
    build_server(store, host=host, port=port).run(transport=transport)
