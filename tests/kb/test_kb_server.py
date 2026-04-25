"""MCP server round-trip tests using the in-memory client/server harness."""

import asyncio
from datetime import date

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore


def _seed(store):
    store.upsert_nodes("team/api", [
        Node(id="a", repo="team/api", kind="function", name="OrderService", file="svc.py"),
        Node(id="b", repo="team/api", kind="function", name="charge"),
    ])
    store.upsert_edges("team/api", [Edge(
        src="a", dst="b", relation="calls", confidence=Confidence.EXTRACTED,
        provenance=Provenance(source_file="svc.py", source_line=5, verified_at=date(2026, 6, 21)),
    )])


def _unwrap(structured):
    """FastMCP wraps non-object returns (lists, Optionals) under a 'result' key."""
    if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
        return structured["result"]
    return structured


async def _list_tools(server):
    async with connect(server) as client:
        return await client.list_tools()


async def _call(server, tool, args):
    async with connect(server) as client:
        return await client.call_tool(tool, args)


@pytest.fixture
def server(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    _seed(s)
    yield build_server(s)
    s.close()


def test_lists_expected_tools(server):
    tools = asyncio.run(_list_tools(server))
    names = {t.name for t in tools.tools}
    assert {
        "graph_stats", "get_node", "get_neighbors", "search_code",
        "find_definition", "find_callers", "shortest_path",
        "repo_dependencies", "repo_flow", "blast_radius",
    } <= names


def test_find_definition_exact(server):
    res = asyncio.run(_call(server, "find_definition", {"name": "OrderService"}))
    items = _unwrap(res.structuredContent)
    assert any(n["id"] == "a" for n in items)


def test_find_callers(server):
    # the seeded edge is a --calls--> b, so b's caller is a
    res = asyncio.run(_call(server, "find_callers", {"node_id": "b"}))
    out = _unwrap(res.structuredContent)
    assert [n["id"] for n in out["nodes"]] == ["a"]
    assert out["total"] == 1 and out["truncated"] is False


def test_shortest_path(server):
    res = asyncio.run(_call(server, "shortest_path", {"src_id": "a", "dst_id": "b"}))
    items = _unwrap(res.structuredContent)
    assert [n["id"] for n in items] == ["a", "b"]


def test_find_dependents(tmp_path):
    # consumer's manifest depends_on package 'libx'
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_nodes("consumer", [
        Node(id="consumer:pyproject", repo="consumer", kind="file", name="pyproject.toml"),
        Node(id="pkg:libx", repo="(packages)", kind="package", name="libx"),
    ])
    s.upsert_edges("consumer", [Edge(
        src="consumer:pyproject", dst="pkg:libx", relation="depends_on",
        confidence=Confidence.EXTRACTED,
        provenance=Provenance(source_file="pyproject.toml", verified_at=date(2026, 6, 21)),
    )])
    res = asyncio.run(_call(build_server(s), "find_dependents", {"package": "libx"}))
    out = _unwrap(res.structuredContent)
    assert [n["repo"] for n in out["nodes"]] == ["consumer"]
    s.close()


def test_get_node_round_trip(server):
    res = asyncio.run(_call(server, "get_node", {"node_id": "a"}))
    assert not res.isError
    node = _unwrap(res.structuredContent)
    assert node["name"] == "OrderService"
    assert node["repo"] == "team/api"


def test_graph_stats(server):
    res = asyncio.run(_call(server, "graph_stats", {}))
    assert res.structuredContent["nodes"] == 2
    assert res.structuredContent["by_confidence"] == {"EXTRACTED": 1}


def test_search_code(server):
    res = asyncio.run(_call(server, "search_code", {"query": "order"}))
    items = _unwrap(res.structuredContent)
    assert any(n["name"] == "OrderService" for n in items)


def test_get_neighbors_with_provenance(server):
    res = asyncio.run(_call(server, "get_neighbors", {"node_id": "a", "direction": "out"}))
    out = _unwrap(res.structuredContent)
    edges = out["edges"]
    assert edges[0]["dst"] == "b"
    assert edges[0]["confidence"] == "EXTRACTED"
    assert edges[0]["verified_at"] == "2026-06-21"
    assert out["total"] == 1 and out["truncated"] is False


def test_get_neighbors_budgets_and_reports_truncation(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_nodes("r", [Node(id="h", repo="r", kind="function", name="hub")]
                   + [Node(id=f"c{i}", repo="r", kind="function", name=f"c{i}") for i in range(10)])
    s.upsert_edges("r", [Edge(src="h", dst=f"c{i}", relation="calls",
                              confidence=Confidence.EXTRACTED,
                              provenance=Provenance(source_file="f", verified_at=date(2026, 6, 21)))
                         for i in range(10)])
    res = asyncio.run(_call(build_server(s), "get_neighbors",
                            {"node_id": "h", "direction": "out", "limit": 3}))
    out = _unwrap(res.structuredContent)
    assert len(out["edges"]) == 3 and out["total"] == 10 and out["truncated"] is True
    s.close()


def _seed_cross_repo(s):
    # repoB depends_on a package repoA publishes; repoB also calls an endpoint repoA exposes
    s.upsert_nodes("repoA", [
        Node(id="A:man", repo="repoA", kind="file", name="pkg.json"),
        Node(id="pkg:lib", repo="(packages)", kind="package", name="lib"),
        Node(id="ep:/api/x", repo="repoA", kind="endpoint", name="/api/x")])
    s.upsert_nodes("repoB", [
        Node(id="B:man", repo="repoB", kind="file", name="pkg.json"),
        Node(id="B:cli", repo="repoB", kind="file", name="client.ts")])
    prov = Provenance(source_file="f", verified_at=date(2026, 6, 21))
    e = lambda src, dst, rel, c: Edge(src=src, dst=dst, relation=rel, confidence=c, provenance=prov)  # noqa: E731
    # exposes/publishes edges originate from repoA nodes so the two-hop attributes them to repoA
    s.upsert_edges("repoA", [
        e("A:man", "pkg:lib", "publishes", Confidence.EXTRACTED),
        e("A:man", "ep:/api/x", "exposes", Confidence.INFERRED)])
    s.upsert_edges("repoB", [
        e("B:man", "pkg:lib", "depends_on", Confidence.EXTRACTED),
        e("B:cli", "ep:/api/x", "calls_http", Confidence.INFERRED)])


def test_repo_dependencies_and_flow_tools(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")
    _seed_cross_repo(s)
    srv = build_server(s)

    def call(tool):
        res = asyncio.run(_call(srv, tool, {"repo": "repoB", "direction": "out"}))
        return _unwrap(res.structuredContent)["edges"]

    # repoB depends on repoA (out)
    assert any(x["src"] == "repoB" and x["dst"] == "repoA" and x["relation"] == "depends_on"
               for x in call("repo_dependencies"))
    # repoB calls repoA over HTTP (out): caller --flow--> exposer
    assert any(x["src"] == "repoB" and x["dst"] == "repoA" and x["relation"] == "flow"
               for x in call("repo_flow"))
    s.close()


def test_output_is_sanitized(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_nodes("r", [Node(id="x", repo="r", kind="function", name="ev\x1bil\x00name")])
    res = asyncio.run(_call(build_server(s), "get_node", {"node_id": "x"}))
    name = _unwrap(res.structuredContent)["name"]
    assert "\x1b" not in name and "\x00" not in name and "evilname" in name
    s.close()


def test_blast_radius_reverse_reach(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_nodes("r", [Node(id=x, repo="r", kind="function", name=x) for x in ("a", "b", "c")])
    prov = Provenance(source_file="f", source_line=1, verified_at=date(2026, 6, 25))
    # a --calls--> b --calls--> c
    s.upsert_edges("r", [
        Edge(src="a", dst="b", relation="calls", confidence=Confidence.INFERRED, provenance=prov),
        Edge(src="b", dst="c", relation="calls", confidence=Confidence.INFERRED, provenance=prov)])
    out = _unwrap(asyncio.run(
        _call(build_server(s), "blast_radius", {"node_id": "c", "hops": 3})).structuredContent)
    # changing c could break b (direct caller, hop 1) and a (transitive, hop 2)
    assert {h["id"]: h["hop"] for h in out["hits"]} == {"b": 1, "a": 2}
    assert out["total"] == 2 and out["truncated"] is False
    # a 1-hop radius stops at the direct caller
    out1 = _unwrap(asyncio.run(
        _call(build_server(s), "blast_radius", {"node_id": "c", "hops": 1})).structuredContent)
    assert [h["id"] for h in out1["hits"]] == ["b"]
    s.close()
