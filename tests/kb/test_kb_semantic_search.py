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
        # "grid"-ish text -> x axis (near node a); otherwise y axis (near node b)
        return [[1.0, 0.0] if "grid" in t.lower() else [0.0, 1.0] for t in texts]


def _nodes(structured):
    """The `nodes` list out of the envelope these two verbs now return.

    They used to return a bare list, which left a stale-store drop with nowhere to be
    reported. The envelope (`nodes`/`total`/`truncated`/`note`) matches every other
    node-returning verb and carries the disclosure.
    """
    return _unwrap(structured)["nodes"]


def _note(structured):
    return _unwrap(structured).get("note")


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
        Node(id="a", repo="r", kind="function", name="ForecastService"),
        Node(id="b", repo="r", kind="function", name="load_readings"),
    ])
    vs = VectorStore(tmp_path / "embeddings.sqlite")
    vs.upsert([("a", "r", [1.0, 0.0]), ("b", "r", [0.0, 1.0])])
    return store, vs


def test_a_k_past_the_sqlite_vec_window_limit_is_not_a_tool_error(tmp_path):
    """`semantic_search` and `ask` took any `k`, and sqlite-vec refused a window over 4096.

    The store asks it for `k * 8` rows, so `k=513` raised "k value in knn query too
    large" and the MCP client saw only "Error executing tool". Run through the client,
    with the real sqlite-vec store, because the brute store has no window to refuse.
    """
    import pytest

    pytest.importorskip("sqlite_vec")
    from contextlake.kb.embeddings.store import MAX_SEARCH_K, SqliteVecStore

    store = SqliteStore(tmp_path / "kb.sqlite")
    store.upsert_nodes("r", [
        Node(id="a", repo="r", kind="function", name="ForecastService"),
        Node(id="b", repo="r", kind="function", name="load_readings"),
    ])
    vs = SqliteVecStore(tmp_path / "embeddings.sqlite")
    vs.upsert([("a", "r", [1.0, 0.0]), ("b", "r", [0.0, 1.0])])
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        for tool, args in (("semantic_search", {"query": "the grid sampling in ForecastService"}),
                           ("ask", {"question": "the grid sampling in ForecastService"})):
            for k in (MAX_SEARCH_K, MAX_SEARCH_K + 1, 600):
                res = asyncio.run(_call(server, tool, {**args, "k": k}))
                assert not res.is_error, (tool, k)
                assert {n["id"] for n in _nodes(res.structured_content)} == {"a", "b"}, (tool, k)
    finally:
        vs.close()
        store.close()


def test_semantic_search_ranks_and_maps_to_nodes(tmp_path):
    store, vs = _store_with_vectors(tmp_path)
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        # "ForecastService" anchors the query in the index; "grid" is what steers
        # the fake embedder onto node a's axis. The query used to be "the grid
        # sampling", which names nothing this store holds -- so it now trips the
        # relevance floor, which is the point of the floor rather than a conflict
        # with it.
        res = asyncio.run(_call(server, "semantic_search",
                                {"query": "the grid sampling in ForecastService", "k": 1}))
        items = _nodes(res.structured_content)
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
        "SensorGatewayAdapter",               # reads like an indexed class
        "where is the retry scheduler configured",
        "SamlAssertionValidator",
    ]
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        for tool in ("semantic_search", "hybrid_search"):
            for query in plausible_but_absent:
                res = asyncio.run(_call(server, tool, {"query": query, "k": 3}))
                assert _nodes(res.structured_content) == [], f"{tool}: {query}"
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
                                {"query": "ForecastService", "k": 2}))
        items = _nodes(res.structured_content)
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
        assert _nodes(res.structured_content) == []
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


# --- a negative k ---------------------------------------------------------------------------

def _ledger_store(tmp_path):
    """Ten vectors, and a class with three subclasses, for the tools that take `k`.

    Ten and three are the counts a negative slice makes visible: `[:-3]` of ten is seven, and
    `[:-1]` of three is two, where `[:0]` is none of them."""
    from datetime import date

    from contextlake.kb.model import Confidence, Edge, Provenance

    store = SqliteStore(tmp_path / "kb.sqlite")
    funcs = [Node(id=f"fn{i}", repo="r", kind="function", name=f"Ledger{i}") for i in range(10)]
    classes = [Node(id="BaseLedger", repo="r", kind="class", name="BaseLedger", file="b.py",
                    line_start=1)]
    classes += [Node(id=f"SubLedger{i}", repo="r", kind="class", name=f"SubLedger{i}",
                     file="c.py", line_start=i) for i in range(1, 4)]
    store.upsert_nodes("r", funcs + classes)
    store.upsert_edges("r", [
        Edge(src=f"SubLedger{i}", dst="BaseLedger", relation="inherits",
             confidence=Confidence.EXTRACTED,
             provenance=Provenance(source_file="c.py", source_line=i,
                                   verified_at=date(2026, 10, 1)))
        for i in range(1, 4)])
    vs = VectorStore(tmp_path / "embeddings.sqlite")
    vs.upsert([(f"fn{i}", "r", [1.0, 0.01 * i]) for i in range(10)])
    return store, vs


def _ledger_call(tmp_path, tool, args):
    store, vs = _ledger_store(tmp_path)
    try:
        server = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        return _unwrap(asyncio.run(_call(server, tool, args)).structured_content)
    finally:
        vs.close()
        store.close()


def test_semantic_and_hybrid_search_cut_a_negative_k_to_nothing(tmp_path):
    """A negative `k` sliced the ranking from the end: `k=-3` over ten vectors returned the
    top seven with `truncated: false`, so the caller read a shortened list as the answer."""
    for tool in ("semantic_search", "hybrid_search"):
        full = _ledger_call(tmp_path, tool, {"query": "Ledger", "k": 10})
        assert len(full["nodes"]) == 10, tool        # the control: ten exist to be cut
        for k in (-1, -3):
            res = _ledger_call(tmp_path, tool, {"query": "Ledger", "k": k})
            assert res["nodes"] == [], (tool, k)


def test_ask_cuts_a_negative_k_to_nothing_on_the_search_and_subclass_routes(tmp_path):
    """`ask` had two more sites: `subs[:k]` dropped one subclass per unit of -k, and
    `len(subs) > k` read True with no subclass at all."""
    full = _ledger_call(tmp_path, "ask", {"question": "what extends BaseLedger", "k": 8})
    assert full["route"] == "subclasses"
    assert len(full["nodes"]) == 3 and full["truncated"] is False   # the control

    cut = _ledger_call(tmp_path, "ask", {"question": "what extends BaseLedger", "k": -1})
    assert cut["nodes"] == [] and cut["truncated"] is True          # three exist, none shown

    none = _ledger_call(tmp_path, "ask", {"question": "what extends SubLedger1", "k": -1})
    assert none["route"] == "subclasses"
    assert none["nodes"] == [] and none["truncated"] is False       # nothing was cut

    vector = _ledger_call(tmp_path, "ask", {"question": "Ledger", "k": -3})
    assert vector["nodes"] == []


def test_the_vector_store_and_the_ranking_do_not_slice_from_the_end(tmp_path):
    """The two library slices, called directly: the tools floor `k` first, so these only see a
    negative one from another caller."""
    from contextlake.kb.embeddings.hybrid import hybrid_search

    store, vs = _ledger_store(tmp_path)
    try:
        assert len(vs.search([1.0, 0.0], k=10)) == 10
        assert vs.search([1.0, 0.0], k=-3) == []
        assert hybrid_search(store, vs, _FakeEmbedder(), "Ledger", k=-3) == []
    finally:
        vs.close()
        store.close()
