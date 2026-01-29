"""MCP server round-trip tests using the in-memory client/server harness."""

import asyncio
from datetime import date

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from gitlab_sync.kb.model import Confidence, Edge, Node, Provenance
from gitlab_sync.kb.server import build_server
from gitlab_sync.kb.store.sqlite_store import SqliteStore


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
    } <= names


def test_find_definition_exact(server):
    res = asyncio.run(_call(server, "find_definition", {"name": "OrderService"}))
    items = _unwrap(res.structuredContent)
    assert any(n["id"] == "a" for n in items)


def test_find_callers(server):
    # the seeded edge is a --calls--> b, so b's caller is a
    res = asyncio.run(_call(server, "find_callers", {"node_id": "b"}))
    items = _unwrap(res.structuredContent)
    assert [n["id"] for n in items] == ["a"]


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
    items = _unwrap(res.structuredContent)
    assert [n["repo"] for n in items] == ["consumer"]
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
    edges = _unwrap(res.structuredContent)
    assert edges[0]["dst"] == "b"
    assert edges[0]["confidence"] == "EXTRACTED"
    assert edges[0]["verified_at"] == "2026-06-21"


def test_output_is_sanitized(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_nodes("r", [Node(id="x", repo="r", kind="function", name="ev\x1bil\x00name")])
    res = asyncio.run(_call(build_server(s), "get_node", {"node_id": "x"}))
    name = _unwrap(res.structuredContent)["name"]
    assert "\x1b" not in name and "\x00" not in name and "evilname" in name
    s.close()
