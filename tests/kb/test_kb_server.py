"""MCP server round-trip tests using the in-memory client/server harness."""

import asyncio
from datetime import date

import pytest
from mcp import Client

from contextlake.kb import server as server_mod
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore


def _seed(store):
    store.upsert_nodes("team/api", [
        Node(id="a", repo="team/api", kind="function", name="CatalogService", file="svc.py"),
        Node(id="b", repo="team/api", kind="function", name="charge"),
    ])
    store.upsert_edges("team/api", [Edge(
        src="a", dst="b", relation="calls", confidence=Confidence.EXTRACTED,
        provenance=Provenance(source_file="svc.py", source_line=5, verified_at=date(2026, 6, 21)),
    )])


def _unwrap(structured):
    """MCPServer wraps non-object returns (lists, Optionals) under a 'result' key."""
    if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
        return structured["result"]
    return structured


async def _list_tools(server):
    async with Client(server) as client:
        return await client.list_tools()


async def _call(server, tool, args):
    async with Client(server) as client:
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
    res = asyncio.run(_call(server, "find_definition", {"name": "CatalogService"}))
    items = _unwrap(res.structured_content)
    assert any(n["id"] == "a" for n in items)


def test_find_callers(server):
    # the seeded edge is a --calls--> b, so b's caller is a
    res = asyncio.run(_call(server, "find_callers", {"node_id": "b"}))
    out = _unwrap(res.structured_content)
    assert [n["id"] for n in out["nodes"]] == ["a"]
    assert out["total"] == 1 and out["truncated"] is False


def test_shortest_path(server):
    res = asyncio.run(_call(server, "shortest_path", {"src_id": "a", "dst_id": "b"}))
    out = _unwrap(res.structured_content)
    assert [n["id"] for n in out["nodes"]] == ["a", "b"]
    assert out["found"] is True and out["hops"] == 1 and out["gap"] is None


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
    out = _unwrap(res.structured_content)
    assert [n["repo"] for n in out["nodes"]] == ["consumer"]
    s.close()


def test_get_node_round_trip(server):
    res = asyncio.run(_call(server, "get_node", {"node_id": "a"}))
    assert not res.is_error
    node = _unwrap(res.structured_content)
    assert node["name"] == "CatalogService"
    assert node["repo"] == "team/api"


def test_graph_stats(server):
    res = asyncio.run(_call(server, "graph_stats", {}))
    assert res.structured_content["nodes"] == 2
    assert res.structured_content["by_confidence"] == {"EXTRACTED": 1}


def test_search_code(server):
    res = asyncio.run(_call(server, "search_code", {"query": "catalog"}))
    items = _unwrap(res.structured_content)
    assert any(n["name"] == "CatalogService" for n in items)


def test_get_neighbors_with_provenance(server):
    res = asyncio.run(_call(server, "get_neighbors", {"node_id": "a", "direction": "out"}))
    out = _unwrap(res.structured_content)
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
    out = _unwrap(res.structured_content)
    assert len(out["edges"]) == 3 and out["total"] == 10 and out["truncated"] is True
    s.close()


def _seed_cross_repo(s):
    # repoB depends_on a package repoA publishes; repoB also calls an endpoint repoA exposes
    # (both repos get a repos row, as real indexing writes one -- see cmds/index.py)
    s.upsert_repo(Repo(id="repoA", path="/a"))
    s.upsert_repo(Repo(id="repoB", path="/b"))
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
        return _unwrap(res.structured_content)["edges"]

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
    out = _unwrap(asyncio.run(_call(srv, "get_readme", {"repo": "r"})).structured_content)
    assert out["found"] and out["path"] == "README.md" and "Does the thing" in out["markdown"]
    # a repo with no clone / no README -> found False, never an error
    absent = _unwrap(asyncio.run(_call(srv, "get_readme", {"repo": "nope"})).structured_content)
    assert absent["found"] is False
    s.close()


def test_get_repo_brief_from_shard(tmp_path):
    from contextlake.kb.store.shards import GraphShard, write_shard
    nodes = [
        Node(id="svc", repo="r", kind="class", name="CatalogService", file="svc.py"),
        Node(id="chg", repo="r", kind="function", name="charge", file="svc.py", lang="python"),
        Node(id="pkg", repo="(packages)", kind="package", name="requests")]
    prov = Provenance(source_file="svc.py", source_line=1, verified_at=date(2026, 6, 21))
    edges = [Edge(src="svc", dst="chg", relation="calls", confidence=Confidence.EXTRACTED,
                  provenance=prov)]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="abc", nodes=nodes, edges=edges))
    s = SqliteStore(tmp_path / "kb.sqlite")
    srv = build_server(s)
    out = _unwrap(asyncio.run(_call(srv, "get_repo_brief", {"repo": "r"})).structured_content)
    assert out["found"] and out["node_count"] == 3 and out["head"] == "abc"
    assert out["kinds"].get("class") == 1 and "requests" in out["packages"]
    missing = _unwrap(asyncio.run(_call(srv, "get_repo_brief", {"repo": "x"})).structured_content)
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
    out = _unwrap(asyncio.run(_call(srv, "graph_health", {})).structured_content)
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
    res = asyncio.run(_call(srv, "get_repo_links", {"repo": "team/api"}))
    out = _unwrap(res.structured_content)
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
    out = _unwrap(asyncio.run(_call(srv, "get_node", {"node_id": "fn"})).structured_content)
    assert out["doc"] == "Charge a card." and out["signature"] == "(amount, currency)"
    s.close()


def test_list_repos_with_stats(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_repo(Repo(id="team/a", path="/a", head_commit="aaa"))
    s.upsert_repo(Repo(id="team/b", path="/b", head_commit="bbb"))
    s.upsert_nodes("team/a", [Node(id="n1", repo="team/a", kind="function", name="f"),
                              Node(id="n2", repo="team/a", kind="class", name="C")])
    srv = build_server(s)
    out = _unwrap(asyncio.run(_call(srv, "list_repos", {})).structured_content)
    assert out["total"] == 2
    by_id = {r["id"]: r for r in out["repos"]}
    assert by_id["team/a"]["node_count"] == 2 and by_id["team/b"]["node_count"] == 0
    assert by_id["team/a"]["head_commit"] == "aaa"
    s.close()


def test_output_is_sanitized(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_nodes("r", [Node(id="x", repo="r", kind="function", name="ev\x1bil\x00name")])
    res = asyncio.run(_call(build_server(s), "get_node", {"node_id": "x"}))
    name = _unwrap(res.structured_content)["name"]
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
        _call(build_server(s), "blast_radius", {"node_id": "c", "hops": 3})).structured_content)
    # changing c could break b (direct caller, hop 1) and a (transitive, hop 2)
    assert {h["id"]: h["hop"] for h in out["hits"]} == {"b": 1, "a": 2}
    assert out["total"] == 2 and out["truncated"] is False
    # a 1-hop radius stops at the direct caller
    out1 = _unwrap(asyncio.run(
        _call(build_server(s), "blast_radius", {"node_id": "c", "hops": 1})).structured_content)
    assert [h["id"] for h in out1["hits"]] == ["b"]
    s.close()


def test_blast_radius_hits_cite_the_call_site_and_their_ambiguity(tmp_path):
    # The MCP surface had the same gap as `kb impact --json`: hits sharing a name
    # were indistinguishable, and an AMBIGUOUS label carried nothing an agent could
    # check. Both are facts the walk already holds.
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_nodes("r", [
        Node(id="callee", repo="r", kind="method", name="close", file="src/db.py",
             line_start=40),
        Node(id="caller", repo="r", kind="function", name="shutdown", file="src/app.py",
             line_start=7)])
    s.upsert_edges("r", [Edge(
        src="caller", dst="callee", relation="calls", confidence=Confidence.AMBIGUOUS,
        attrs={"name_candidates": 3},
        provenance=Provenance(source_file="src/app.py", source_line=9,
                              verified_at=date(2026, 8, 5)))])
    out = _unwrap(asyncio.run(
        _call(build_server(s), "blast_radius", {"node_id": "callee"})).structured_content)
    hit = out["hits"][0]
    assert hit["file"] == "src/app.py" and hit["line"] == 7
    assert hit["via_file"] == "src/app.py" and hit["via_line"] == 9
    assert hit["name_candidates"] == 3
    s.close()


def test_get_wiki_serves_prose_with_staleness(tmp_path):
    s = SqliteStore(tmp_path / "k.sqlite")          # store.path.parent == tmp_path
    s.upsert_repo(Repo(id="team/api", path="/a", head_commit="abc123"))
    wdir = tmp_path / "wiki"
    wdir.mkdir()
    (wdir / "team__api.md").write_text(
        "# team/api\n\nThe catalog service.\n\n"
        "*Generated from the knowledge graph of `team/api` at commit `abc123` on 2026-06-25.*\n")

    out = _unwrap(asyncio.run(
        _call(build_server(s), "get_wiki", {"repo": "team/api"})).structured_content)
    assert out["found"] and out["stale"] is False   # wiki commit == current head
    assert "The catalog service." in out["markdown"]
    assert out["wiki_commit"] == "abc123"

    # repo moves on -> the wiki is now stale
    s.upsert_repo(Repo(id="team/api", path="/a", head_commit="def456"))
    out2 = _unwrap(asyncio.run(
        _call(build_server(s), "get_wiki", {"repo": "team/api"})).structured_content)
    assert out2["stale"] is True and out2["current_commit"] == "def456"

    # no wiki page -> found=False, stale=True (nothing to trust)
    out3 = _unwrap(asyncio.run(
        _call(build_server(s), "get_wiki", {"repo": "team/missing"})).structured_content)
    assert out3["found"] is False and out3["stale"] is True
    s.close()


# --- ask router -----------------------------------------------------------

def test_ask_routes_definition(server):
    res = asyncio.run(_call(server, "ask", {"question": "where is CatalogService defined"}))
    out = res.structured_content
    assert out["route"] == "definition"
    assert out["target"] == "CatalogService"
    assert [n["name"] for n in out["nodes"]] == ["CatalogService"]


def test_ask_routes_callers(server):
    res = asyncio.run(_call(server, "ask", {"question": "who calls charge"}))
    out = res.structured_content
    assert out["route"] == "callers"
    # CatalogService (node a) calls charge (node b)
    assert "CatalogService" in [n["name"] for n in out["nodes"]]


def test_ask_routes_impact(server):
    res = asyncio.run(_call(server, "ask", {"question": "what breaks if I change charge"}))
    out = res.structured_content
    assert out["route"] == "impact"
    assert out["blast"] is not None and out["blast"]["total"] >= 1


def test_ask_falls_back_to_search(server):
    res = asyncio.run(_call(server, "ask", {"question": "find the checkout flow logic"}))
    out = res.structured_content
    assert out["route"] == "search"           # no exact route matched
    assert "note" in out and out["note"]


def test_ask_empty_question_with_embedder_configured_does_not_crash(tmp_path):
    """An empty/whitespace question falling through to the SEARCH route used to
    call embedder.embed([""])[0] unconditionally -- embedding an empty string
    crashed downstream in vector_store.search()'s scoring (float - None) instead
    of degrading gracefully like search_code already does."""
    from contextlake.kb.embeddings.store import VectorStore

    class _FakeEmbedder:
        name = "fake"

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    store = SqliteStore(tmp_path / "kb.sqlite")
    _seed(store)
    vs = VectorStore(tmp_path / "embeddings.sqlite")
    vs.upsert([("a", "team/api", [1.0, 0.0]), ("b", "team/api", [0.0, 1.0])])
    try:
        srv = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        res = asyncio.run(_call(srv, "ask", {"question": "   "}))
        out = res.structured_content
        assert out["route"] == "search"
        assert out["nodes"] == []
    finally:
        vs.close()
        store.close()


def test_ask_reports_a_definition_miss_as_a_miss(tmp_path):
    """The headline acceptance failure: asked where a symbol that does not exist
    is defined, `ask` returned real, resolvable, unrelated citations and no
    statement that nothing matched, while `find_definition` on the same name
    correctly returned empty.

    The handler classified the question as `definition`, missed, then overwrote
    the route with `search` and re-answered by embedding the whole question, so
    the established negative never reached the client.
    """
    from contextlake.kb.embeddings.store import VectorStore

    class _FakeEmbedder:
        name = "fake"

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    store = SqliteStore(tmp_path / "kb.sqlite")
    _seed(store)
    vs = VectorStore(tmp_path / "embeddings.sqlite")
    # every node is a perfect cosine match, so the fallback search cannot come
    # back empty by accident -- the miss has to be reported despite the hits
    vs.upsert([("a", "team/api", [1.0, 0.0]), ("b", "team/api", [1.0, 0.0])])
    try:
        srv = build_server(store, embedder=_FakeEmbedder(), vector_store=vs)
        res = asyncio.run(
            _call(srv, "ask", {"question": "Where is FrobnicateTheWidget defined?"}))
        out = res.structured_content

        # find_definition is the control: it is already correct
        direct = asyncio.run(
            _call(srv, "find_definition", {"name": "FrobnicateTheWidget"})).structured_content
        assert direct["result"] == []

        assert out["route"] == "definition"       # not rewritten to "search"
        assert out["answered"] is False
        assert "FrobnicateTheWidget" in out["note"]
        assert out["note"].lower().startswith("no definition named")
        # any nodes carried alongside must be labelled as leads, not an answer
        if out["nodes"]:
            assert "not an answer" in out["note"].lower()
    finally:
        vs.close()
        store.close()


def _server_with_perfect_embedder(tmp_path):
    """A store whose vector search ALWAYS returns every node at cosine 1.0.

    The point of the relevance floor is that a nearest-neighbour search has no
    notion of "nothing matched" -- it ranks a top k however far away they are.
    A fake embedder that matches everything perfectly is that property taken to
    its limit, so a test that still expects no nodes back is testing the floor
    and nothing else.
    """
    from contextlake.kb.embeddings.store import VectorStore

    class _FakeEmbedder:
        name = "fake"

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    store = SqliteStore(tmp_path / "kb.sqlite")
    _seed(store)
    vs = VectorStore(tmp_path / "embeddings.sqlite")
    vs.upsert([("a", "team/api", [1.0, 0.0]), ("b", "team/api", [1.0, 0.0])])
    return build_server(store, embedder=_FakeEmbedder(), vector_store=vs), store, vs


def test_ask_returns_no_nodes_when_no_query_term_is_indexed(tmp_path):
    """A question made only of terms the graph has never seen must come back
    empty, not with the nearest k.

    The seeded store holds CatalogService and charge. Nothing in it is about
    SAML, so ranking a top k over it produces citations that all resolve and
    none of which are about the question -- the exact failure this product
    exists to prevent.
    """
    srv, store, vs = _server_with_perfect_embedder(tmp_path)
    try:
        out = asyncio.run(_call(srv, "ask", {
            "question": "Which repository implements the SAML SSO flow?"})).structured_content
        assert out["answered"] is False
        assert out["nodes"] == []
        assert "SAML" in out["note"]
    finally:
        vs.close()
        store.close()


def test_ask_still_answers_when_a_query_term_is_indexed(tmp_path):
    """The floor must not swallow real questions: one indexed term is enough to
    let the hits through, so the guard cannot degrade into a blanket refusal."""
    srv, store, vs = _server_with_perfect_embedder(tmp_path)
    try:
        out = asyncio.run(_call(srv, "ask", {
            "question": "CatalogService charge"})).structured_content
        assert out["answered"] is True
        assert out["nodes"] != []
    finally:
        vs.close()
        store.close()


def test_ask_names_the_terms_that_matched_nothing(tmp_path):
    """Partially anchored: hits are real but cannot be about the missing terms,
    and the answer has to say which ones those are."""
    srv, store, vs = _server_with_perfect_embedder(tmp_path)
    try:
        out = asyncio.run(_call(srv, "ask", {
            "question": "CatalogService kerberos delegation"})).structured_content
        assert out["answered"] is False
        assert out["nodes"] != []                  # the anchored term still retrieves
        assert "kerberos" in out["note"].lower()
        assert "delegation" in out["note"].lower()
    finally:
        vs.close()
        store.close()


def test_ask_handles_unresolvable_symbol(server):
    # a callers question about a symbol that isn't indexed must not raise
    res = asyncio.run(_call(server, "ask", {"question": "who calls NotARealSymbol"}))
    out = res.structured_content
    assert out["route"] == "callers"
    assert out["nodes"] == [] and "resolve" in out["note"].lower()


def test_ask_explain_falls_back_to_repo_brief_when_no_wiki(tmp_path):
    # "explain <repo>" with no generated wiki should return the grounded anatomy
    # (repo brief), not a blind semantic search.
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    s = SqliteStore(tmp_path / "kb.sqlite")
    nodes = [Node(id="svc", repo="acme/orders", kind="class", name="CatalogService",
                  file="svc.py")]
    s.upsert_repo(Repo(id="acme/orders", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="acme/orders", head_commit="h1",
                                     nodes=nodes, edges=[]))
    reindex_shard(s, tmp_path, "acme/orders")
    res = asyncio.run(_call(build_server(s), "ask",
                            {"question": "explain the acme/orders repo"}))
    out = res.structured_content
    assert out["route"] == "explain"
    assert out["brief"] is not None and out["brief"]["found"] is True
    assert out["brief"]["repo"] == "acme/orders"
    assert out["wiki"] is None
    s.close()


def test_ask_explain_resolves_a_short_name_to_the_full_host_qualified_repo_id(tmp_path):
    """A person naturally says "explain the catalog-api", not the full
    host-qualified id (`gitlab.example.com/acme/catalog-api`) contextlake
    actually stores repos under -- the router's extract_target only pulls
    that trailing segment out of the question, so explain must resolve it
    back to the real repo id rather than treat it as an unknown repo."""
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    s = SqliteStore(tmp_path / "kb.sqlite")
    repo_id = "gitlab.example.com/acme/catalog-api"
    nodes = [Node(id="svc", repo=repo_id, kind="class", name="CatalogService", file="svc.py")]
    s.upsert_repo(Repo(id=repo_id, path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo=repo_id, head_commit="h1", nodes=nodes, edges=[]))
    reindex_shard(s, tmp_path, repo_id)
    res = asyncio.run(_call(build_server(s), "ask", {"question": "explain the catalog-api"}))
    out = res.structured_content
    assert out["route"] == "explain"
    assert out["brief"] is not None and out["brief"]["found"] is True
    assert out["brief"]["repo"] == repo_id
    s.close()


def test_ask_owners_resolves_a_short_repo_name_too(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    repo_id = "gitlab.example.com/acme/catalog-api"
    s.upsert_repo(Repo(id=repo_id, path=str(tmp_path), head_commit="h1"))
    res = asyncio.run(_call(build_server(s), "ask", {"question": "who owns catalog-api"}))
    out = res.structured_content
    assert out["route"] == "owners"
    assert repo_id in out["note"] or "catalog-api" in out["note"]
    s.close()


def test_ask_explain_reports_when_no_repo_matches_at_all(server):
    """A target that resolves to no repo still degrades to a search, but the miss
    has to survive the degrade.

    This used to assert route == "search": the handler overwrote the classified
    route on the way to the fallback, so the one thing the graph had established
    -- no indexed repo is named zzz-nonexistent-repo -- was gone by the time the
    answer left the server, and the fallback's hits read as an answer.
    """
    res = asyncio.run(_call(server, "ask", {"question": "explain the zzz-nonexistent-repo"}))
    out = res.structured_content
    assert out["route"] == "explain"          # not rewritten to "search"
    assert out["answered"] is False
    assert "zzz-nonexistent-repo" in out["note"]
    assert out["note"].lower().startswith("no indexed repo matching")


def test_ask_routes_subclasses(tmp_path):
    # "what extends X" returns the types with an incoming inherits edge to X
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    s = SqliteStore(tmp_path / "kb.sqlite")
    prov = Provenance(source_file="m.py", source_line=1, verified_at=date(2026, 6, 21))
    nodes = [
        Node(id="base", repo="r", kind="class", name="Embedder"),
        Node(id="a", repo="r", kind="class", name="OllamaEmbedder"),
        Node(id="b", repo="r", kind="class", name="BuiltinEmbedder"),
    ]
    edges = [
        Edge(src="a", dst="base", relation="inherits", confidence=Confidence.INFERRED,
             provenance=prov),
        Edge(src="b", dst="base", relation="inherits", confidence=Confidence.INFERRED,
             provenance=prov),
    ]
    s.upsert_repo(Repo(id="r", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h1", nodes=nodes, edges=edges))
    reindex_shard(s, tmp_path, "r")
    res = asyncio.run(_call(build_server(s), "ask", {"question": "what extends Embedder"}))
    out = res.structured_content
    assert out["route"] == "subclasses"
    assert set(n["name"] for n in out["nodes"]) == {"OllamaEmbedder", "BuiltinEmbedder"}
    s.close()


def test_find_callers_and_blast_radius_resolve_a_bare_name(tmp_path):
    # Agents pass a symbol NAME, not an internal node id. find_callers / blast_radius
    # must resolve the name (not silently return empty) — the realistic MCP path.
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    s = SqliteStore(tmp_path / "kb.sqlite")
    prov = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))
    nodes = [
        Node(id="svc", repo="r", kind="class", name="CatalogService"),
        Node(id="c1", repo="r", kind="function", name="checkout"),
        Node(id="c2", repo="r", kind="function", name="refund"),
    ]
    edges = [
        Edge(src="c1", dst="svc", relation="calls", confidence=Confidence.EXTRACTED,
             provenance=prov),
        Edge(src="c2", dst="svc", relation="calls", confidence=Confidence.EXTRACTED,
             provenance=prov),
    ]
    s.upsert_repo(Repo(id="r", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h1", nodes=nodes, edges=edges))
    reindex_shard(s, tmp_path, "r")
    srv = build_server(s)
    # by NAME (what an agent passes) — not by the internal id "svc"
    callers = asyncio.run(_call(srv, "find_callers", {"node_id": "CatalogService"}))
    assert callers.structured_content["total"] == 2
    blast = asyncio.run(_call(srv, "blast_radius", {"node_id": "CatalogService"}))
    assert blast.structured_content["total"] == 2
    # an unknown name resolves to an empty (not an error) result
    empty = asyncio.run(_call(srv, "find_callers", {"node_id": "NoSuchSymbol"}))
    assert empty.structured_content["total"] == 0
    s.close()


def test_find_callers_and_blast_radius_disclose_a_name_collision(tmp_path):
    """Two definitions share a name; the tools seed one and return its result.

    Measured on a real store: both definitions of one name returned the SAME
    six-caller list, so the answer was the union and precision was 0.33 and 0.67
    depending on which one the caller meant. `ask` over the identical resolution
    said "N matched, used the first"; find_callers and blast_radius called
    directly over MCP returned a bare list with no note at all, and the MCP tool
    surface is what agents actually consume.
    """
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    s = SqliteStore(tmp_path / "kb.sqlite")
    prov = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))
    nodes = [
        Node(id="a::classify", repo="r", kind="function", name="classify", file="a.py"),
        Node(id="b::classify", repo="r", kind="function", name="classify", file="b.py"),
        Node(id="caller", repo="r", kind="function", name="scan_one", file="a.py"),
    ]
    edges = [Edge(src="caller", dst="a::classify", relation="calls",
                  confidence=Confidence.EXTRACTED, provenance=prov)]
    s.upsert_repo(Repo(id="r", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h1", nodes=nodes, edges=edges))
    reindex_shard(s, tmp_path, "r")
    srv = build_server(s)
    try:
        callers = asyncio.run(
            _call(srv, "find_callers", {"node_id": "classify"})).structured_content
        assert callers["note"] and "2 matched" in callers["note"]
        assert "used the first" in callers["note"]

        blast = asyncio.run(
            _call(srv, "blast_radius", {"node_id": "classify"})).structured_content
        assert blast["note"] and "2 matched" in blast["note"]

        # an unambiguous node id must stay quiet: a note on every call is noise,
        # and noise is how a real disclosure gets ignored
        exact = asyncio.run(
            _call(srv, "find_callers", {"node_id": "a::classify"})).structured_content
        assert exact["note"] is None
    finally:
        s.close()


def test_graph_walk_tools_accept_the_name_argument_too(tmp_path):
    """find_definition takes `name`, search_code takes `query`, and these two took
    `node_id` -- so the obvious first call from an agent failed with a raw pydantic
    validation dump, an error about a schema addressed to nobody. `name` is now an
    accepted alias, and a call with neither gets an instruction instead of a dump.
    """
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    s = SqliteStore(tmp_path / "kb.sqlite")
    prov = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))
    nodes = [
        Node(id="svc", repo="r", kind="class", name="CatalogService"),
        Node(id="c1", repo="r", kind="function", name="checkout"),
    ]
    edges = [Edge(src="c1", dst="svc", relation="calls",
                  confidence=Confidence.EXTRACTED, provenance=prov)]
    s.upsert_repo(Repo(id="r", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h1", nodes=nodes, edges=edges))
    reindex_shard(s, tmp_path, "r")
    srv = build_server(s)
    try:
        for tool, key in (("find_callers", "total"), ("blast_radius", "total")):
            by_name = asyncio.run(
                _call(srv, tool, {"name": "CatalogService"})).structured_content
            by_id = asyncio.run(
                _call(srv, tool, {"node_id": "CatalogService"})).structured_content
            assert by_name[key] == by_id[key] == 1, tool

            # neither spelling supplied: an instruction, not a validation dump
            empty = asyncio.run(_call(srv, tool, {})).structured_content
            assert empty[key] == 0
            assert "node_id" in empty["note"] and "name" in empty["note"], tool
    finally:
        s.close()


def test_get_wiki_serves_cluster_page_by_namespace(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    _seed(s)
    wiki = tmp_path / "wiki"
    (wiki / "_clusters").mkdir(parents=True)
    (wiki / "_clusters" / "acme__pay.md").write_text(
        "# acme/pay (cluster)\n\nThe pay cluster talks over HTTP.\n\n---\n"
        "*cluster-commits: abc123def456.*\n", encoding="utf-8")
    server = build_server(s)
    try:
        res = asyncio.run(_call(server, "get_wiki", {"repo": "acme/pay"}))
        out = res.structured_content
        assert out["found"] is True and out["kind"] == "cluster"
        assert "pay cluster talks over HTTP" in out["markdown"]
        # an unknown target (no repo page, no cluster page) is not found
        miss = asyncio.run(_call(server, "get_wiki", {"repo": "no/such"}))
        assert miss.structured_content["found"] is False
    finally:
        s.close()


# --- run_server transport dispatch ------------------------------------------
#
# The transports are NOT interchangeable at the SDK-kwarg level: sse_app() takes
# (sse_path, message_path, transport_security, host) and would raise
# TypeError('unexpected keyword argument') on streamable-http-only options like
# stateless_http/json_response. And only the HTTP family builds an ASGI app at
# all -- stdio goes straight to MCPServer.run_stdio_async(). These tests pin the
# exact kwargs each transport gets. (Who may *reach* those apps is
# test_serve_matrix.py's second half.)

class _FakeServer:
    def __init__(self):
        self.calls = []
        self.apps = []
        self.stdio_runs = 0

    def run(self, **kwargs):
        self.calls.append(kwargs)

    async def run_stdio_async(self):
        self.stdio_runs += 1

    def sse_app(self, **kwargs):
        self.apps.append(("sse_app", kwargs))
        return object()

    def streamable_http_app(self, **kwargs):
        self.apps.append(("streamable_http_app", kwargs))
        return object()


def _fake_uvicorn(monkeypatch):
    """Capture uvicorn.run's args instead of binding a socket and blocking."""
    import uvicorn

    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append((app, kw)))
    return served


def test_run_server_stdio_passes_no_network_kwargs(monkeypatch):
    fake = _FakeServer()
    monkeypatch.setattr(server_mod, "build_server", lambda *a, **kw: fake)

    server_mod.run_server(store=None, transport="stdio")

    # run_stdio_async, not .run(transport="stdio"): the SDK's .run() is exactly
    # `anyio.run(self.run_stdio_async)`, and contextlake now owns that anyio.run
    # so it can set the tool-thread limiter and install a SIGTERM handler, both
    # of which only exist inside a running loop. What stdio gets is otherwise
    # unchanged, which is what the rest of this test pins.
    assert fake.stdio_runs == 1
    assert fake.calls == []
    assert fake.apps == []  # stdio builds no ASGI app and needs no token


def test_run_server_streamable_http_passes_stateless_and_json_response(monkeypatch):
    fake = _FakeServer()
    monkeypatch.setattr(server_mod, "build_server", lambda *a, **kw: fake)
    served = _fake_uvicorn(monkeypatch)

    server_mod.run_server(store=None, transport="streamable-http", host="0.0.0.0",
                          port=9999, token="t-synthetic")

    assert fake.calls == []  # the HTTP family no longer goes through .run()
    (name, kwargs), = fake.apps
    assert name == "streamable_http_app"
    assert kwargs["stateless_http"] is True and kwargs["json_response"] is True
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["transport_security"].enable_dns_rebinding_protection is True

    (app, serve_kwargs), = served
    assert isinstance(app, server_mod.BearerAuthMiddleware)
    assert serve_kwargs["host"] == "0.0.0.0" and serve_kwargs["port"] == 9999


def test_run_server_sse_passes_host_port_only(monkeypatch):
    """Regression guard: sse must NOT receive stateless_http/json_response --
    the SDK's sse_app() has no **kwargs catch-all and would raise
    TypeError('unexpected keyword argument') if it did."""
    fake = _FakeServer()
    monkeypatch.setattr(server_mod, "build_server", lambda *a, **kw: fake)
    served = _fake_uvicorn(monkeypatch)

    server_mod.run_server(store=None, transport="sse", host="0.0.0.0", port=9999,
                          token="t-synthetic")

    (name, kwargs), = fake.apps
    assert name == "sse_app"
    assert set(kwargs) == {"transport_security", "host"}
    assert kwargs["host"] == "0.0.0.0"

    (app, serve_kwargs), = served
    assert isinstance(app, server_mod.BearerAuthMiddleware)
    assert serve_kwargs["host"] == "0.0.0.0" and serve_kwargs["port"] == 9999


def test_run_server_mints_a_token_when_the_caller_forgets_one(monkeypatch):
    """No code path may start an unauthenticated socket, even one that omits
    the token cmds/serve.py normally supplies (and prints)."""
    fake = _FakeServer()
    monkeypatch.setattr(server_mod, "build_server", lambda *a, **kw: fake)
    monkeypatch.delenv(server_mod.TOKEN_ENV, raising=False)
    served = _fake_uvicorn(monkeypatch)

    server_mod.run_server(store=None, transport="streamable-http")

    (app, _), = served
    assert isinstance(app, server_mod.BearerAuthMiddleware)
    assert len(app._token) >= 32


def test_sse_app_builds_a_real_asgi_app_with_the_expected_routes(tmp_path):
    """Live smoke test of the real (unmocked) sse transport code path: a full
    client/server MCP round-trip over sse needs a running event loop juggling a
    server task and a client task concurrently, which isn't practical in a
    synchronous unit test -- but building the actual Starlette ASGI app via the
    same `mcp.server.mcpserver.MCPServer.sse_app()` method run_server()'s
    "sse" branch drives is, and it exercises the real SDK, not a mock."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    try:
        app = build_server(s).sse_app(host="127.0.0.1")
        paths = {route.path for route in app.routes}
        assert "/sse" in paths
        assert "/messages" in paths
    finally:
        s.close()


def test_ask_dependents_reports_an_unknown_package_as_unknown(tmp_path):
    """The one branch in `ask` that skipped resolution: an unindexed package came
    back as an empty list under a note asserting manifest provenance, which tells
    an agent "nothing depends on it" when the truth is "no such package is
    indexed" -- a different and much more actionable fact -- while citing
    manifests that were never read. With no target at all it printed the word
    None into the note.
    """
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    prov = Provenance(source_file="p.json", source_line=1, verified_at=date(2026, 6, 21))
    s = SqliteStore(tmp_path / "kb.sqlite")
    nodes = [
        Node(id="pkg:known", repo="r", kind="package", name="known-pkg"),
        Node(id="f:a", repo="r", kind="file", name="a.py", file="a.py"),
    ]
    edges = [Edge(src="f:a", dst="pkg:known", relation="depends_on",
                  confidence=Confidence.INFERRED, provenance=prov)]
    s.upsert_repo(Repo(id="r", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h1", nodes=nodes, edges=edges))
    reindex_shard(s, tmp_path, "r")
    srv = build_server(s)
    try:
        unknown = asyncio.run(_call(srv, "ask", {
            "question": "what depends on totally-unknown-package"})).structured_content
        assert unknown["answered"] is False
        assert unknown["nodes"] == []
        assert "totally-unknown-package" in unknown["note"]
        assert "manifest" not in unknown["note"].lower(), \
            "must not assert provenance for a lookup that never happened"

        # no symbol at all in the question: never the literal word None
        none_target = asyncio.run(_call(srv, "ask", {
            "question": "what depends on it"})).structured_content
        assert none_target["answered"] is False
        assert "None" not in none_target["note"]

        # the real package still answers, and still carries its provenance label
        hit = asyncio.run(_call(srv, "ask", {
            "question": "what depends on known-pkg"})).structured_content
        assert hit["answered"] is True
        assert [n["name"] for n in hit["nodes"]] == ["a.py"]
        assert "INFERRED from manifests" in hit["note"]
    finally:
        s.close()


def test_find_dependents_honours_repo_scope_and_reports_an_unknown_package(tmp_path):
    """`ask` advertises a `repo` parameter and scopes five routes by it, but
    find_dependents had no repo parameter at all, so this one route silently
    answered fleet-wide."""
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    prov = Provenance(source_file="p.json", source_line=1, verified_at=date(2026, 6, 21))
    s = SqliteStore(tmp_path / "kb.sqlite")
    for repo_id, fid in (("r1", "f:1"), ("r2", "f:2")):
        nodes = [
            Node(id="pkg:shared", repo=repo_id, kind="package", name="shared-pkg"),
            Node(id=fid, repo=repo_id, kind="file", name=f"{repo_id}.py",
                 file=f"{repo_id}.py"),
        ]
        edges = [Edge(src=fid, dst="pkg:shared", relation="depends_on",
                      confidence=Confidence.INFERRED, provenance=prov)]
        s.upsert_repo(Repo(id=repo_id, path=str(tmp_path), head_commit="h1"))
        d = tmp_path / repo_id
        d.mkdir()
        write_shard(d, GraphShard(repo=repo_id, head_commit="h1", nodes=nodes, edges=edges))
        reindex_shard(s, d, repo_id)
    srv = build_server(s)
    try:
        both = asyncio.run(_call(srv, "find_dependents", {
            "package": "shared-pkg"})).structured_content
        assert both["total"] == 2

        scoped = asyncio.run(_call(srv, "find_dependents", {
            "package": "shared-pkg", "repo": "r2"})).structured_content
        assert [n["name"] for n in scoped["nodes"]] == ["r2.py"]

        missing = asyncio.run(_call(srv, "find_dependents", {
            "package": "no-such-pkg"})).structured_content
        assert missing["total"] == 0
        assert missing["note"] and "no-such-pkg" in missing["note"]
    finally:
        s.close()


def test_ask_owners_never_claims_a_ranking_that_did_not_run(tmp_path):
    """`who_knows` returns an empty list BEFORE issuing a single git command when
    the repo has no local clone on record. The owners route appended ", ranked
    from git history." to that result unconditionally, so an answer that read no
    history at all was labelled as though it had read and ranked one -- a
    provenance claim asserted rather than derived."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    try:
        s.upsert_repo(Repo(id="team/api", path=""))
        srv = build_server(s)
        out = asyncio.run(_call(srv, "ask", {
            "question": "who owns `team/api`?"})).structured_content
        assert out["route"] == "owners"
        assert out["owners"]["owners"] == []
        assert "ranked from git history" not in out["note"]
        assert "no local clone" in out["note"]
        assert out["answered"] is False
    finally:
        s.close()


def test_ask_owners_still_credits_git_history_when_the_ranking_ran(tmp_path, monkeypatch):
    """The honest branch must not swallow the real answer: a repo whose history
    was read and attributed keeps the git-history provenance line."""
    from contextlake.kb import ownership

    monkeypatch.setattr(ownership, "compute_owners", lambda *a, **k: [
        ownership.Owner(name="Ada", email="ada@example.com", commits=3, lines=40,
                        last_active="2026-01-02", share=1.0, score=1.0)])
    s = SqliteStore(tmp_path / "kb.sqlite")
    try:
        s.upsert_repo(Repo(id="team/api", path=str(tmp_path / "clone")))
        srv = build_server(s)
        out = asyncio.run(_call(srv, "ask", {
            "question": "who owns `team/api`?"})).structured_content
        assert out["answered"] is True
        assert [o["name"] for o in out["owners"]["owners"]] == ["Ada"]
        assert "ranked from git history" in out["note"]
        assert out["owners"]["ranking_gap"] is None
    finally:
        s.close()


def test_ask_impact_honours_the_k_it_advertises(tmp_path):
    """`ask` advertises `k` and every other route honours it. The impact route
    dropped it and let `blast_radius` apply its own default of 100, so a
    small-context agent asking for one result could receive a hundred."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    try:
        s.upsert_nodes("r", [Node(id="hot", repo="r", kind="function", name="charge_order")]
                       + [Node(id=f"c{i}", repo="r", kind="function", name=f"c{i}")
                          for i in range(30)])
        prov = Provenance(source_file="f", source_line=1, verified_at=date(2026, 8, 5))
        s.upsert_edges("r", [Edge(src=f"c{i}", dst="hot", relation="calls",
                                  confidence=Confidence.EXTRACTED, provenance=prov)
                             for i in range(30)])
        srv = build_server(s)
        out = asyncio.run(_call(srv, "ask", {
            "question": "what breaks if I change charge_order", "k": 3})).structured_content
        assert out["route"] == "impact"
        assert len(out["blast"]["hits"]) == 3
        assert out["blast"]["truncated"] is True and out["truncated"] is True
        # and the note must not report the capped count as if it were the total
        assert "the first 3 node(s)" in out["note"]

        wide = asyncio.run(_call(srv, "ask", {
            "question": "what breaks if I change charge_order", "k": 30})).structured_content
        assert len(wide["blast"]["hits"]) == 30 and wide["blast"]["truncated"] is False
    finally:
        s.close()


@pytest.mark.parametrize("tool,args", [
    ("get_neighbors", {"node_id": "a"}),
    ("repo_dependencies", {"repo": "team/api"}),
    ("repo_flow", {"repo": "team/api"}),
    ("repo_event_flow", {"repo": "team/api"}),
])
def test_an_invalid_direction_is_refused_by_every_tool_that_takes_one(server, tool, args):
    """`get_neighbors` raised for a direction outside the vocabulary while the three
    architecture tools matched no branch and returned an empty edge list -- so a
    typo'd direction read as "this repo has no dependencies / no HTTP flow / no
    event flow", a positive architectural claim produced by a rejected argument.
    All four now carry the vocabulary in their advertised schema and refuse."""
    bad = asyncio.run(_call(server, tool, {**args, "direction": "sideways"}))
    assert bad.is_error, f"{tool} answered an invalid direction instead of refusing it"
    assert "'in', 'out' or 'both'" in bad.content[0].text

    tools = {t.name: t for t in asyncio.run(_list_tools(server)).tools}
    assert tools[tool].input_schema["properties"]["direction"]["enum"] == ["in", "out", "both"]

    # the legal values still answer
    for good in ("in", "out", "both"):
        ok = asyncio.run(_call(server, tool, {**args, "direction": good}))
        assert not ok.is_error, f"{tool} rejected direction={good!r}"


def test_repo_side_refuses_an_out_of_vocabulary_direction_directly():
    """The schema enum catches this over the wire; the shared filter refuses it
    too, so an in-process caller of the closure cannot get a silent empty list
    where the store's own `neighbors` would have raised."""
    rows = [{"src": "a", "dst": "b"}]
    assert server_mod._repo_side(rows, "a", "out") == rows
    with pytest.raises(ValueError, match="invalid direction: 'sideways'"):
        server_mod._repo_side(rows, "a", "sideways")


def test_an_unresolvable_symbol_is_reported_as_unknown_not_as_unaffected(tmp_path):
    """`blast_radius` seeded the walk with the caller's raw string when it did not
    resolve, so an unknown symbol came back as a well-formed, non-error, bounded
    impact analysis of a symbol that does not exist -- "nothing depends on this,
    safe to change" rendered identically to "I have never heard of this symbol".
    `find_callers` returned the same empty success without fabricating a seed."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    _seed(s)
    srv = build_server(s)
    try:
        blast = asyncio.run(_call(srv, "blast_radius", {
            "node_id": "NoSuchSymbolAnywhere"})).structured_content
        assert blast["total"] == 0 and blast["hits"] == []
        assert blast["note"] == "No indexed symbol named 'NoSuchSymbolAnywhere'."
        assert blast["seed"] == "", "an unresolvable seed must not be echoed as a node id"

        callers = asyncio.run(_call(srv, "find_callers", {
            "node_id": "NoSuchSymbolAnywhere"})).structured_content
        assert callers["total"] == 0
        assert callers["note"] == "No indexed symbol named 'NoSuchSymbolAnywhere'."

        # a real symbol with no callers keeps saying nothing: the note is the
        # miss disclosure, not a label on every empty answer
        quiet = asyncio.run(_call(srv, "find_callers", {"node_id": "a"})).structured_content
        assert quiet["total"] == 0 and quiet["note"] is None
    finally:
        s.close()


def test_a_negative_hops_is_refused_rather_than_answered_emptily(server):
    """`hops: -1` walked nowhere and returned a clean "nothing is affected" --
    a nonsense input rendered as a reassuring answer. Zero hops is a real
    request and still answers."""
    bad = asyncio.run(_call(server, "blast_radius", {"node_id": "b", "hops": -1}))
    assert bad.is_error and "hops must be 0 or greater" in bad.content[0].text

    zero = asyncio.run(_call(server, "blast_radius", {"node_id": "b", "hops": 0}))
    assert not zero.is_error and zero.structured_content["total"] == 0


def test_cluster_wiki_staleness_is_derived_not_asserted(tmp_path):
    """`stale` was hardcoded to False for a cluster page: nothing was checked, and
    the reassuring value was returned anyway, so an agent filtering on the field
    treated an unverifiable page as verified fresh. A cluster page carries the
    freshness stamp its generator skips on -- the fingerprint of its members'
    (repo, head) pairs -- so it can be recomputed and compared."""
    from contextlake.kb.wiki.cluster import cluster_fingerprint

    s = SqliteStore(tmp_path / "kb.sqlite")
    try:
        s.upsert_repo(Repo(id="acme/pay/api", path="/a", head_commit="h1"))
        s.upsert_repo(Repo(id="acme/pay/web", path="/b", head_commit="h2"))
        fresh = cluster_fingerprint({"heads": {"acme/pay/api": "h1", "acme/pay/web": "h2"}})
        clusters = tmp_path / "wiki" / "_clusters"
        clusters.mkdir(parents=True)
        page = clusters / "acme__pay.md"
        page.write_text(f"# acme/pay (cluster)\n\nThey talk over HTTP.\n\n---\n"
                        f"*cluster-commits: {fresh}.*\n", encoding="utf-8")

        out = asyncio.run(_call(build_server(s), "get_wiki", {
            "repo": "acme/pay"})).structured_content
        assert out["found"] is True and out["kind"] == "cluster"
        assert out["stale"] is False, "members unmoved: the page really is fresh"

        # a member's head moves on -> the page describes code that has changed
        s.upsert_repo(Repo(id="acme/pay/web", path="/b", head_commit="h9"))
        moved = asyncio.run(_call(build_server(s), "get_wiki", {
            "repo": "acme/pay"})).structured_content
        assert moved["stale"] is True

        # no stamp at all -> nothing to compare against, so fail closed
        page.write_text("# acme/pay (cluster)\n\nNo provenance footer.\n", encoding="utf-8")
        unstamped = asyncio.run(_call(build_server(s), "get_wiki", {
            "repo": "acme/pay"})).structured_content
        assert unstamped["found"] is True and unstamped["stale"] is True
    finally:
        s.close()


def test_repo_scoped_tools_say_when_the_repo_does_not_exist(tmp_path):
    """`get_wiki`, `get_readme` and `get_repo_brief` all carry `found`; these five
    echoed the caller's own string back with an empty payload, so a mistyped repo
    id was indistinguishable from a known repo with no data -- five confident
    "nothing here" answers instead of one "no such repo"."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    _seed_cross_repo(s)                    # indexes repoA and repoB
    s.upsert_repo(Repo(id="team/api", path=""))
    srv = build_server(s)
    try:
        for tool, args in (("who_knows", {"repo": "no/such/repo"}),
                           ("get_repo_links", {"repo": "no/such/repo"}),
                           ("repo_dependencies", {"repo": "no/such/repo"}),
                           ("repo_flow", {"repo": "no/such/repo"}),
                           ("repo_event_flow", {"repo": "no/such/repo"})):
            out = _unwrap(asyncio.run(_call(srv, tool, args)).structured_content)
            assert out["found"] is False, f"{tool} claimed an unknown repo exists"

        # a known repo with genuinely no data is a different answer
        for tool, args in (("get_repo_links", {"repo": "team/api"}),
                           ("repo_dependencies", {"repo": "team/api"}),
                           ("repo_flow", {"repo": "team/api"}),
                           ("repo_event_flow", {"repo": "team/api"})):
            out = _unwrap(asyncio.run(_call(srv, tool, args)).structured_content)
            assert out["found"] is True, f"{tool} lost a repo it has indexed"
        known = _unwrap(asyncio.run(
            _call(srv, "repo_dependencies", {"repo": "repoB"})).structured_content)
        assert known["found"] is True and known["total"] == 1
    finally:
        s.close()


def test_who_knows_separates_an_unknown_repo_from_one_with_no_clone(tmp_path):
    """The two empty cases had one wording between them: an unindexed repo was
    reported as "no local clone is on record for this repo", which asserts the
    repo is indexed. They are different facts and now read differently."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    try:
        s.upsert_repo(Repo(id="team/api", path=""))
        srv = build_server(s)
        unknown = _unwrap(asyncio.run(
            _call(srv, "who_knows", {"repo": "no/such/repo"})).structured_content)
        assert unknown["found"] is False
        assert "no repository with this id is indexed" in unknown["ranking_gap"]

        no_clone = _unwrap(asyncio.run(
            _call(srv, "who_knows", {"repo": "team/api"})).structured_content)
        assert no_clone["found"] is True
        assert "no local clone" in no_clone["ranking_gap"]
    finally:
        s.close()


def test_shortest_path_distinguishes_a_typo_from_a_disconnection(tmp_path):
    """The return type was a bare `list[NodeOut]`, the only tool in the file whose
    output shape could not express a miss: a typo'd node id and a genuine "these
    two are unconnected" were the same empty list, and the docstring's "Empty if
    none" described only the second."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    _seed(s)
    # an island: indexed, but no edge reaches it from the seeded pair
    s.upsert_nodes("team/api", [Node(id="island", repo="team/api", kind="function",
                                     name="orphan")])
    srv = build_server(s)
    try:
        typo = _unwrap(asyncio.run(_call(srv, "shortest_path", {
            "src_id": "a", "dst_id": "no-such-node"})).structured_content)
        assert typo["found"] is False and typo["nodes"] == []
        assert "no indexed node with" in typo["gap"] and "dst_id" in typo["gap"]

        both = _unwrap(asyncio.run(_call(srv, "shortest_path", {
            "src_id": "nope-a", "dst_id": "nope-b"})).structured_content)
        assert both["found"] is False
        assert "src_id" in both["gap"] and "dst_id" in both["gap"]

        apart = _unwrap(asyncio.run(_call(srv, "shortest_path", {
            "src_id": "a", "dst_id": "island"})).structured_content)
        assert apart["found"] is False and apart["nodes"] == []
        assert "both nodes are indexed" in apart["gap"]
        assert apart["gap"] != typo["gap"], "a typo and a disconnection must not read alike"

        same = _unwrap(asyncio.run(_call(srv, "shortest_path", {
            "src_id": "a", "dst_id": "a"})).structured_content)
        assert same["found"] is True and same["hops"] == 0
    finally:
        s.close()


def test_graph_health_says_when_there_is_nothing_to_be_healthy_about(tmp_path):
    """Zero stale, zero dangling, zero parser-stale is the exact output of a
    perfectly healthy fleet, and it was also the output of a store that has never
    been indexed. The counts were not wrong, they were unqualified: an operator
    asking "is the knowledge base healthy?" was told yes about one that does not
    exist."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    try:
        empty = _unwrap(asyncio.run(
            _call(build_server(s), "graph_health", {})).structured_content)
        assert empty["indexed"] is False
        assert empty["repos"] == 0 and empty["stale"] == 0 and empty["dangling"] == 0

        s.upsert_repo(Repo(id="team/api", path=str(tmp_path)))
        real = _unwrap(asyncio.run(
            _call(build_server(s), "graph_health", {})).structured_content)
        assert real["indexed"] is True
    finally:
        s.close()
