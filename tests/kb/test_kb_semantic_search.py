"""Round-trip test for the optional semantic_search MCP tool."""

import asyncio

from mcp.shared.memory import create_connected_server_and_client_session as connect

from contextlake.kb.embeddings.store import VectorStore
from contextlake.kb.model import Node
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore


class _FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        # "order"-ish text -> x axis (near node a); otherwise y axis (near node b)
        return [[1.0, 0.0] if "order" in t.lower() else [0.0, 1.0] for t in texts]


def _unwrap(structured):
    if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
        return structured["result"]
    return structured


async def _call(server, tool, args):
    async with connect(server) as client:
        return await client.call_tool(tool, args)


async def _tool_names(server):
    async with connect(server) as client:
        return {t.name for t in (await client.list_tools()).tools}


def _store_with_vectors(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    store.upsert_nodes("r", [
        Node(id="a", repo="r", kind="function", name="OrderService"),
        Node(id="b", repo="r", kind="function", name="charge"),
    ])
    vs = VectorStore(tmp_path / "embeddings.sqlite")
    vs.upsert([("a", "r", [1.0, 0.0]), ("b", "r", [0.0, 1.0])])
    return store, vs


def test_semantic_search_ranks_and_maps_to_nodes(tmp_path):
    store, vs = _store_with_vectors(tmp_path)
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        res = asyncio.run(_call(server, "semantic_search", {"query": "the order workflow", "k": 1}))
        items = _unwrap(res.structuredContent)
        assert [n["id"] for n in items] == ["a"]  # nearest to the query vector
    finally:
        vs.close()
        store.close()


def test_semantic_search_absent_without_embedder(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    try:
        names = asyncio.run(_tool_names(build_server(store)))
        assert "semantic_search" not in names
        assert "search_code" in names  # graph tools still present
    finally:
        store.close()
