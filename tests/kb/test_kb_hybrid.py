"""Tests for HippoRAG-style hybrid retrieval (embeddings seeds + graph PPR)."""

import asyncio
from datetime import date

from mcp.shared.memory import create_connected_server_and_client_session as connect

from gitlab_sync.kb.embeddings.hybrid import _expand, _ppr, hybrid_search
from gitlab_sync.kb.embeddings.store import VectorStore
from gitlab_sync.kb.model import Confidence, Edge, Node, Provenance
from gitlab_sync.kb.server import build_server
from gitlab_sync.kb.store.sqlite_store import SqliteStore


class _FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]  # query points along the "seed" axis


def _setup(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    store.upsert_nodes("r", [
        Node(id="seed", repo="r", kind="function", name="OrderBuilder"),
        Node(id="neighbor", repo="r", kind="function", name="apply_tax"),
        Node(id="island", repo="r", kind="function", name="log_metric"),
    ])
    store.upsert_edges("r", [Edge(
        src="seed", dst="neighbor", relation="calls", confidence=Confidence.EXTRACTED,
        provenance=Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21)),
    )])
    vs = VectorStore(tmp_path / "e.sqlite")
    # only "seed" matches the query direction; neighbor/island are semantically far
    vs.upsert([("seed", "r", [1.0, 0.0]), ("neighbor", "r", [0.0, 1.0]),
               ("island", "r", [0.0, 1.0])])
    return store, vs


# --- PPR internals ---------------------------------------------------------

def test_ppr_propagates_to_connected_node():
    nodes = {"s", "n", "i"}
    adjacency = {"s": {"n"}, "n": {"s"}}  # i is isolated
    scores = _ppr(nodes, adjacency, {"s": 1.0}, damping=0.5, iters=30)
    assert scores["s"] > scores["n"] > scores["i"]


def test_expand_collects_bounded_subgraph(tmp_path):
    store, _ = _setup(tmp_path)
    try:
        visited, adjacency = _expand(store, ["seed"], hops=1)
        assert visited == {"seed", "neighbor"}  # island not reachable
        assert "neighbor" in adjacency["seed"]
    finally:
        store.close()


# --- end-to-end hybrid ranking --------------------------------------------

def test_hybrid_surfaces_graph_neighbor_over_island(tmp_path):
    store, vs = _setup(tmp_path)
    try:
        ranked = hybrid_search(store, vs, _FakeEmbedder(), "build an order", k=3)
        ids = [nid for nid, _ in ranked]
        assert ids[0] == "seed"
        # the graph-connected neighbour outranks the isolated node, though both are
        # equally (un)matched semantically — the structural signal breaks the tie
        assert ids.index("neighbor") < ids.index("island")
    finally:
        vs.close()
        store.close()


def test_hybrid_empty_without_seeds(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    vs = VectorStore(tmp_path / "e.sqlite")  # empty
    try:
        assert hybrid_search(store, vs, _FakeEmbedder(), "anything") == []
    finally:
        vs.close()
        store.close()


# --- MCP tool round-trip ---------------------------------------------------

def _unwrap(structured):
    if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
        return structured["result"]
    return structured


def test_hybrid_search_tool(tmp_path):
    store, vs = _setup(tmp_path)
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)

        async def go():
            async with connect(server) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                assert "hybrid_search" in names
                return await client.call_tool("hybrid_search", {"query": "order", "k": 3})

        res = asyncio.run(go())
        items = _unwrap(res.structuredContent)
        assert items[0]["id"] == "seed"
    finally:
        vs.close()
        store.close()
