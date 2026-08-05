"""Round-trip test for the optional semantic_search MCP tool."""

import asyncio

from mcp import Client

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
    async with Client(server) as client:
        return await client.call_tool(tool, args)


async def _tool_names(server):
    async with Client(server) as client:
        return {t.name for t in (await client.list_tools()).tools}


def _store_with_vectors(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    store.upsert_nodes("r", [
        Node(id="a", repo="r", kind="function", name="CatalogService"),
        Node(id="b", repo="r", kind="function", name="charge"),
    ])
    vs = VectorStore(tmp_path / "embeddings.sqlite")
    vs.upsert([("a", "r", [1.0, 0.0]), ("b", "r", [0.0, 1.0])])
    return store, vs


def test_semantic_search_ranks_and_maps_to_nodes(tmp_path):
    store, vs = _store_with_vectors(tmp_path)
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        # "CatalogService" anchors the query in the index; "order" is what steers
        # the fake embedder onto node a's axis. The query used to be "the order
        # workflow", which names nothing this store holds -- so it now trips the
        # relevance floor, which is the point of the floor rather than a conflict
        # with it.
        res = asyncio.run(_call(server, "semantic_search",
                                {"query": "the order workflow in CatalogService", "k": 1}))
        items = _unwrap(res.structured_content)
        assert [n["id"] for n in items] == ["a"]  # nearest to the query vector
        assert items[0]["score"] is not None      # the ranking is readable, not just claimed
    finally:
        vs.close()
        store.close()


def test_semantic_search_returns_nothing_when_no_query_term_is_indexed(tmp_path):
    """A vector index has no concept of "no match": it returns its k nearest
    however far away they are, so a query with no possible answer came back with
    k confident, cited, structurally-valid hits. This is the same defect the
    `ask` route had, in a tool `ask` does not go through -- which is why the
    floor is a shared predicate and not a branch inside `ask`.

    The queries are deliberately plausible rather than gibberish. A floor that
    only catches keyboard mashing is worthless: the queries that produce
    confident wrong answers in practice are well-formed, ordinary-looking names
    for things the store simply does not contain.
    """
    store, vs = _store_with_vectors(tmp_path)
    plausible_but_absent = [
        "PaymentGatewayAdapter",              # reads exactly like an indexed class
        "where is the retry scheduler configured",
        "SamlAssertionValidator",
    ]
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        for tool in ("semantic_search", "hybrid_search"):
            for query in plausible_but_absent:
                res = asyncio.run(_call(server, tool, {"query": query, "k": 3}))
                assert _unwrap(res.structured_content) == [], f"{tool}: {query}"
    finally:
        vs.close()
        store.close()


def test_semantic_search_reports_the_similarity_it_ranks_by(tmp_path):
    """The handler computed a score and dropped it, so a caller got k nodes
    ranked "by similarity" with no similarity to read: the ranking was real and
    the claim unfalsifiable at the call site."""
    store, vs = _store_with_vectors(tmp_path)
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        res = asyncio.run(_call(server, "semantic_search",
                                {"query": "CatalogService", "k": 2}))
        items = _unwrap(res.structured_content)
        assert items, "the query names an indexed symbol, so it must return hits"
        assert all(n["score"] is not None for n in items)
        # ranked best-first, and the score has to agree with that order
        assert [n["score"] for n in items] == sorted(
            (n["score"] for n in items), reverse=True)
    finally:
        vs.close()
        store.close()


def test_semantic_search_empty_query_returns_empty_not_crash(tmp_path):
    """embedder.embed([""])[0] used to reach vector_store.search()'s scoring and
    crash with a raw TypeError instead of degrading gracefully, the way
    search_code already handles an empty query."""
    store, vs = _store_with_vectors(tmp_path)
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        res = asyncio.run(_call(server, "semantic_search", {"query": "   "}))
        assert _unwrap(res.structured_content) == []
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
