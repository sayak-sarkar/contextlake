"""MCP server round-trip tests using the in-memory client/server harness."""

import asyncio
from datetime import date

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
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
        "repo_dependencies", "repo_flow", "repo_event_flow", "blast_radius", "get_wiki",
        "get_readme", "get_repo_brief", "list_repos", "get_repo_links", "graph_health",
        "ask",
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
    # event flow: repoB publishes a topic repoA consumes -> repoB --flow--> repoA
    s.upsert_nodes("repoB", [Node(id="topic:orders", repo="(topics)",
                                  kind="topic", name="orders.created")])
    s.upsert_edges("repoB", [e("B:cli", "topic:orders", "publishes_event", Confidence.INFERRED)])
    s.upsert_edges("repoA", [e("A:man", "topic:orders", "consumes_event", Confidence.INFERRED)])


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
    # repoB publishes an event repoA consumes (out): publisher --flow--> consumer
    assert any(x["src"] == "repoB" and x["dst"] == "repoA" and x["relation"] == "flow"
               for x in call("repo_event_flow"))
    s.close()


def test_get_readme_reads_local_clone(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "README.md").write_text("# My Service\nDoes the thing.\n")
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_repo(Repo(id="r", path=str(clone)))
    srv = build_server(s)
    out = _unwrap(asyncio.run(_call(srv, "get_readme", {"repo": "r"})).structuredContent)
    assert out["found"] and out["path"] == "README.md" and "Does the thing" in out["markdown"]
    # a repo with no clone / no README -> found False, never an error
    absent = _unwrap(asyncio.run(_call(srv, "get_readme", {"repo": "nope"})).structuredContent)
    assert absent["found"] is False
    s.close()


def test_get_repo_brief_from_shard(tmp_path):
    from contextlake.kb.store.shards import GraphShard, write_shard
    nodes = [
        Node(id="svc", repo="r", kind="class", name="OrderService", file="svc.py"),
        Node(id="chg", repo="r", kind="function", name="charge", file="svc.py", lang="python"),
        Node(id="pkg", repo="(packages)", kind="package", name="requests")]
    prov = Provenance(source_file="svc.py", source_line=1, verified_at=date(2026, 6, 21))
    edges = [Edge(src="svc", dst="chg", relation="calls", confidence=Confidence.EXTRACTED,
                  provenance=prov)]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="abc", nodes=nodes, edges=edges))
    s = SqliteStore(tmp_path / "kb.sqlite")
    srv = build_server(s)
    out = _unwrap(asyncio.run(_call(srv, "get_repo_brief", {"repo": "r"})).structuredContent)
    assert out["found"] and out["node_count"] == 3 and out["head"] == "abc"
    assert out["kinds"].get("class") == 1 and "requests" in out["packages"]
    missing = _unwrap(asyncio.run(_call(srv, "get_repo_brief", {"repo": "x"})).structuredContent)
    assert missing["found"] is False
    s.close()


def test_graph_health_detects_dangling(tmp_path):
    from contextlake.kb.store.shards import GraphShard, write_shard
    nodes = [Node(id="a", repo="r", kind="function", name="a")]
    prov = Provenance(source_file="f", verified_at=date(2026, 6, 21))
    # edge to a node that was never upserted -> dangling
    edges = [Edge(src="a", dst="ghost", relation="calls",
                  confidence=Confidence.EXTRACTED, provenance=prov)]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="abc", nodes=nodes, edges=edges))
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_repo(Repo(id="r", path=str(tmp_path / "clone"), head_commit="abc"))
    s.upsert_nodes("r", nodes)        # only 'a' exists in the store; 'ghost' does not
    srv = build_server(s)
    out = _unwrap(asyncio.run(_call(srv, "graph_health", {})).structuredContent)
    assert out["repos"] == 1 and out["checked"] == 1
    assert out["dangling"] == 1 and out["dangling_sample"][0]["dst"] == "ghost"
    s.close()


def test_get_repo_links_grouped(tmp_path):
    from contextlake.kb.ids import make_id
    s = SqliteStore(tmp_path / "k.sqlite")
    rid = make_id("repo", "team/api")
    s.upsert_nodes("@connect:team/api", [
        Node(id=rid, repo="team/api", kind="repo", name="team/api"),
        Node(id="iss:PROJ-1", repo="team/api", kind="issue", name="PROJ-1",
             attrs={"url": "https://example.atlassian.net/browse/PROJ-1",
                    "summary": "Fix the thing", "status": "Open"}),
        Node(id="pg:42", repo="team/api", kind="page", name="Design Doc",
             attrs={"url": "https://example.atlassian.net/wiki/42", "title": "Design Doc"})])
    prov = Provenance(source_file="connect", verified_at=date(2026, 6, 21))

    def ed(dst, rel):
        return Edge(src=rid, dst=dst, relation=rel,
                    confidence=Confidence.EXTRACTED, provenance=prov)
    s.upsert_edges("@connect:team/api", [ed("iss:PROJ-1", "tracked_by"),
                                         ed("pg:42", "documented_by")])
    srv = build_server(s)
    out = _unwrap(asyncio.run(_call(srv, "get_repo_links", {"repo": "team/api"})).structuredContent)
    assert out["total"] == 2
    assert "tracked_by" in out["links"] and "documented_by" in out["links"]
    assert out["links"]["tracked_by"][0]["title"] == "Fix the thing"      # summary -> title
    assert out["links"]["tracked_by"][0]["status"] == "Open"
    s.close()


def test_get_node_surfaces_doc_and_signature(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_nodes("r", [Node(id="fn", repo="r", kind="function", name="charge",
                              attrs={"doc": "Charge a card.", "signature": "(amount, currency)"})])
    srv = build_server(s)
    out = _unwrap(asyncio.run(_call(srv, "get_node", {"node_id": "fn"})).structuredContent)
    assert out["doc"] == "Charge a card." and out["signature"] == "(amount, currency)"
    s.close()


def test_list_repos_with_stats(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_repo(Repo(id="team/a", path="/a", head_commit="aaa"))
    s.upsert_repo(Repo(id="team/b", path="/b", head_commit="bbb"))
    s.upsert_nodes("team/a", [Node(id="n1", repo="team/a", kind="function", name="f"),
                              Node(id="n2", repo="team/a", kind="class", name="C")])
    srv = build_server(s)
    out = _unwrap(asyncio.run(_call(srv, "list_repos", {})).structuredContent)
    assert out["total"] == 2
    by_id = {r["id"]: r for r in out["repos"]}
    assert by_id["team/a"]["node_count"] == 2 and by_id["team/b"]["node_count"] == 0
    assert by_id["team/a"]["head_commit"] == "aaa"
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


def test_get_wiki_serves_prose_with_staleness(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")          # store.path.parent == tmp_path
    s.upsert_repo(Repo(id="team/api", path="/a", head_commit="abc123"))
    wdir = tmp_path / "wiki"
    wdir.mkdir()
    (wdir / "team__api.md").write_text(
        "# team/api\n\nThe order service.\n\n"
        "*Generated from the knowledge graph of `team/api` at commit `abc123` on 2026-06-25.*\n")

    out = _unwrap(asyncio.run(
        _call(build_server(s), "get_wiki", {"repo": "team/api"})).structuredContent)
    assert out["found"] and out["stale"] is False   # wiki commit == current head
    assert "The order service." in out["markdown"]
    assert out["wiki_commit"] == "abc123"

    # repo moves on -> the wiki is now stale
    s.upsert_repo(Repo(id="team/api", path="/a", head_commit="def456"))
    out2 = _unwrap(asyncio.run(
        _call(build_server(s), "get_wiki", {"repo": "team/api"})).structuredContent)
    assert out2["stale"] is True and out2["current_commit"] == "def456"

    # no wiki page -> found=False, stale=True (nothing to trust)
    out3 = _unwrap(asyncio.run(
        _call(build_server(s), "get_wiki", {"repo": "team/missing"})).structuredContent)
    assert out3["found"] is False and out3["stale"] is True
    s.close()


# --- ask router -----------------------------------------------------------

def test_ask_routes_definition(server):
    res = asyncio.run(_call(server, "ask", {"question": "where is OrderService defined"}))
    out = res.structuredContent
    assert out["route"] == "definition"
    assert out["target"] == "OrderService"
    assert [n["name"] for n in out["nodes"]] == ["OrderService"]


def test_ask_routes_callers(server):
    res = asyncio.run(_call(server, "ask", {"question": "who calls charge"}))
    out = res.structuredContent
    assert out["route"] == "callers"
    # OrderService (node a) calls charge (node b)
    assert "OrderService" in [n["name"] for n in out["nodes"]]


def test_ask_routes_impact(server):
    res = asyncio.run(_call(server, "ask", {"question": "what breaks if I change charge"}))
    out = res.structuredContent
    assert out["route"] == "impact"
    assert out["blast"] is not None and out["blast"]["total"] >= 1


def test_ask_falls_back_to_search(server):
    res = asyncio.run(_call(server, "ask", {"question": "find the checkout flow logic"}))
    out = res.structuredContent
    assert out["route"] == "search"           # no exact route matched
    assert "note" in out and out["note"]


def test_ask_handles_unresolvable_symbol(server):
    # a callers question about a symbol that isn't indexed must not raise
    res = asyncio.run(_call(server, "ask", {"question": "who calls NotARealSymbol"}))
    out = res.structuredContent
    assert out["route"] == "callers"
    assert out["nodes"] == [] and "resolve" in out["note"].lower()


def test_ask_explain_falls_back_to_repo_brief_when_no_wiki(tmp_path):
    # "explain <repo>" with no generated wiki should return the grounded anatomy
    # (repo brief), not a blind semantic search.
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    s = SqliteStore(tmp_path / "kb.sqlite")
    nodes = [Node(id="svc", repo="acme/orders", kind="class", name="OrderService",
                  file="svc.py")]
    s.upsert_repo(Repo(id="acme/orders", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="acme/orders", head_commit="h1",
                                     nodes=nodes, edges=[]))
    reindex_shard(s, tmp_path, "acme/orders")
    res = asyncio.run(_call(build_server(s), "ask",
                            {"question": "explain the acme/orders repo"}))
    out = res.structuredContent
    assert out["route"] == "explain"
    assert out["brief"] is not None and out["brief"]["found"] is True
    assert out["brief"]["repo"] == "acme/orders"
    assert out["wiki"] is None
    s.close()
