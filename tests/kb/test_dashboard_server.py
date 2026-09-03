"""The dashboard HTTP server (kb/dashboard/server.py): JSON API + SPA shell routes.

Starts the server on an ephemeral port in a thread, hits the endpoints, shuts it down.
"""

import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import date

import pytest

from contextlake.kb.dashboard.server import TOKEN_HEADER, build_dashboard_server, serve_dashboard
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))


@pytest.fixture
def served(tmp_path, monkeypatch):
    # /api/settings resolves the real config precedence chain, including
    # ~/.contextlake/kb.toml -- isolate HOME so a machine with a real, populated
    # global config (e.g. from manually testing `contextlake init`) can't leak
    # real [[sources]] into what every test here expects to be an empty default.
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    s = SqliteStore(tmp_path / "index.sqlite")
    nodes = [
        Node(id="svc", repo="team/app", kind="class", name="CatalogService", lang="python"),
        Node(id="caller", repo="team/app", kind="function", name="checkout", lang="python"),
    ]
    edges = [Edge(src="caller", dst="svc", relation="calls",
                  confidence=Confidence.EXTRACTED, provenance=_PROV)]
    s.upsert_repo(Repo(id="team/app", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="team/app", head_commit="h1",
                                     nodes=nodes, edges=edges))
    reindex_shard(s, tmp_path, "team/app")
    s.mark_indexed("team/app", "h1", "2026-06-01T00:00:00Z")

    port = _free_port()
    srv = build_dashboard_server(s, tmp_path, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        s.close()


def test_overview_endpoint(served):
    body = json.loads(_get(served + "/api/overview"))
    assert body["stats"]["repos"] == 1
    assert {r["id"] for r in body["repos"]} == {"team/app"}


def test_health_endpoint(served):
    body = json.loads(_get(served + "/api/health"))
    assert body["repos"] == 1
    assert set(body) >= {"stale", "dangling", "checked"}


def test_search_and_impact_endpoints(served):
    res = json.loads(_get(served + "/api/search?q=CatalogService"))
    assert "CatalogService" in {n["name"] for n in res["results"]}
    imp = json.loads(_get(served + "/api/impact?node=svc"))
    assert imp["found"] and "checkout" in {h["name"] for h in imp["hits"]}


def test_symbol_sequence_diagram_endpoint(served):
    body = json.loads(_get(served + "/api/impact/diagram?node=caller"))
    assert body["format"] == "sequencediagram"
    assert "checkout" in body["text"] and "CatalogService" in body["text"]
    missing = json.loads(_get(served + "/api/impact/diagram?node=does-not-exist"))
    assert missing["error"] == "node not found"


def test_repo_detail_and_rel_endpoints(served):
    detail = json.loads(_get(served + "/api/repo/team/app"))
    assert detail["brief"]["node_count"] == 2
    rel = json.loads(_get(served + "/api/repo/team/app/rel"))
    assert set(rel) == {"dependencies", "http_flow", "event_flow"}


def test_fleet_relationships_endpoint(served):
    # Row shape is unit-tested in test_dashboard_data.py; this only proves the
    # route is wired to kb.dashboard.data.fleet_relationships (WCAG 1.1.1's
    # fleet-wide "Overview" table equivalent).
    body = json.loads(_get(served + "/api/relationships"))
    assert set(body) == {"dependencies", "http_flow", "event_flow", "truncated"}
    assert body["truncated"] is False


def test_repo_data_flow_endpoint(served):
    # row-shape detail (file/line/table/relation) is unit-tested in test_dashboard_data.py;
    # this only proves the route is wired to kb.dashboard.data.data_flow.
    body = json.loads(_get(served + "/api/repo/team/app/data-flow"))
    assert body == {"rows": [], "truncated": False}


def test_repo_diagram_endpoint(served):
    body = json.loads(_get(served + "/api/repo/team/app/diagram?format=classdiagram"))
    assert body["format"] == "classdiagram"
    assert "CatalogService" in body["text"]
    # default format (no ?format=) is the generic mermaid relation graph
    default = json.loads(_get(served + "/api/repo/team/app/diagram"))
    assert default["format"] == "mermaid"
    unknown = json.loads(_get(served + "/api/repo/team/app/diagram?format=bogus"))
    assert unknown["error"] == "unknown format"


def test_repo_diagram_endpoint_module_param_scopes_the_view(served):
    # both fixture nodes are under no file path in `served`, so scoping to a
    # module that doesn't exist should just yield an (honestly) empty slice,
    # not an error -- proves the query param reaches data.diagram() at all.
    scoped = json.loads(_get(served + "/api/repo/team/app/diagram?format=mermaid&module=nope"))
    assert scoped["format"] == "mermaid"
    assert "CatalogService" not in scoped["text"]


def test_repo_wiki_endpoint(served, tmp_path):
    # Whole-repo + a module page written straight to the store's wiki/ dir
    # (same layout `contextlake wiki` itself writes) -- no server restart
    # needed since the route reads the filesystem fresh per request.
    from contextlake.kb.cmds.wiki import _module_wiki_filename

    (tmp_path / "wiki").mkdir(exist_ok=True)
    (tmp_path / "wiki" / "team__app.md").write_text(
        "# team/app\n\nGenerated at commit `h1`.\n\nThe **whole repo** page.\n")
    modules_dir = tmp_path / "wiki" / "_modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    (modules_dir / _module_wiki_filename("team/app", "src")).write_text(
        "# team/app: src\n\nGenerated at commit `h1`.\n\nThe **src** subsystem.\n")

    whole = json.loads(_get(served + "/api/repo/team/app/wiki"))
    assert whole["found"] and whole["module"] is None
    assert "<strong>whole repo</strong>" in whole["html"]

    scoped = json.loads(_get(served + "/api/repo/team/app/wiki?module=src"))
    assert scoped["found"] and scoped["module"] == "src"
    assert "<strong>src</strong>" in scoped["html"]

    missing = json.loads(_get(served + "/api/repo/team/app/wiki?module=nope"))
    assert missing["found"] is False and missing["module"] == "nope"


@pytest.fixture
def served_with_wiki_collision(tmp_path, monkeypatch):
    # Two repos: one literally named "team/wiki" (the collision case) and an
    # ordinary "team/app" (the control case, still exercising the real /wiki
    # sub-route). Shared by both the raw-slash and percent-encoded variants of
    # test_repo_wiki_route_does_not_hijack_a_repo_literally_named_wiki below.
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    s = SqliteStore(tmp_path / "index.sqlite")
    nodes = [
        Node(id="wiki_sym", repo="team/wiki", kind="class", name="WikiRepoSymbol", lang="python"),
        Node(id="app_sym", repo="team/app", kind="class", name="AppRepoSymbol", lang="python"),
    ]
    s.upsert_repo(Repo(id="team/wiki", path=str(tmp_path), head_commit="hw"))
    s.upsert_repo(Repo(id="team/app", path=str(tmp_path), head_commit="ha"))
    write_shard(tmp_path, GraphShard(repo="team/wiki", head_commit="hw",
                                     nodes=[nodes[0]], edges=[]))
    write_shard(tmp_path, GraphShard(repo="team/app", head_commit="ha",
                                     nodes=[nodes[1]], edges=[]))
    reindex_shard(s, tmp_path, "team/wiki")
    reindex_shard(s, tmp_path, "team/app")
    s.mark_indexed("team/wiki", "hw", "2026-06-01T00:00:00Z")
    s.mark_indexed("team/app", "ha", "2026-06-01T00:00:00Z")
    port = _free_port()
    srv = build_dashboard_server(s, tmp_path, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        s.close()


def test_repo_wiki_route_does_not_hijack_a_repo_literally_named_wiki(served_with_wiki_collision):
    # Regression for the Task 17 fix-round-1 blocking finding: a real repo whose
    # id itself ends in "/wiki" (a plausible name, e.g. a subproject literally
    # called "wiki") must still get its own repo-detail payload from
    # GET /api/repo/team/wiki, not be silently misrouted into the /wiki
    # sub-route as if "team" were the repo id and "wiki" were the sub-route
    # marker.
    #
    # This MUST use a raw, literal, unencoded "/" between "team" and "wiki" --
    # not a percent-encoded "%2F" -- because that is the actual wire format a
    # real browser sends: dashboard.js's encPath() (`id.split("/").map(
    # encodeURIComponent).join("/")`) percent-encodes each path SEGMENT but
    # rejoins with a literal "/", never encoding the separators themselves.
    # The server's dispatch computes
    # `rest = urllib.parse.urlparse(self.path).path[len("/api/repo/"):]`
    # directly off the raw request-line path -- urlparse never decodes %2F --
    # so only a request with a literal "/" byte produces a `rest` value that
    # actually ends with the literal string "/wiki" and reaches the guarded
    # branch. A %2F-encoded request (see the sibling test below) never reaches
    # it at all, encoded or not, fixed or buggy -- it is not a valid regression
    # test for this bug by itself.
    base = served_with_wiki_collision
    body = json.loads(_get(base + "/api/repo/team/wiki"))
    # The correct repo-detail payload for team/wiki, not a wiki-content
    # payload for team (which would have no "repo"/"brief" keys shaped
    # like this, and would report repo=="team").
    assert body["repo"] == "team/wiki"
    assert body["brief"]["node_count"] == 1
    assert "WikiRepoSymbol" in {t_["name"] for t_ in body["brief"]["top_symbols"]}
    # team/app's own /wiki-suffixed request path is unaffected -- still a
    # real sub-route since "team/app/wiki" is not itself a known repo id.
    wiki_out = json.loads(_get(base + "/api/repo/team/app/wiki"))
    assert wiki_out["repo"] == "team/app" and "brief" not in wiki_out


def test_repo_wiki_route_percent_encoded_id_also_resolves_correctly(served_with_wiki_collision):
    # Companion case, NOT a regression test for the collision bug itself (see
    # the docstring above): a client that properly percent-encodes the "/"
    # inside a repo id (GET /api/repo/team%2Fwiki) produces a `rest` value of
    # the literal characters "team%2Fwiki", which does not end with the
    # literal string "/wiki" -- so this request never reaches the guarded
    # branch at all, regardless of whether the guard exists. It falls straight
    # through to the pre-existing, always-correct fallback
    # (`repo_id = urllib.parse.unquote(rest)`), which is what this test checks
    # still resolves to the right repo. Kept as an additional, weaker case
    # documenting that well-behaved (non-browser) API clients that do escape
    # slashes also get the right answer -- neither test subsumes the other,
    # since they exercise different `rest` values through different branches.
    base = served_with_wiki_collision
    body = json.loads(_get(base + "/api/repo/team%2Fwiki"))
    assert body["repo"] == "team/wiki"
    assert body["brief"]["node_count"] == 1
    assert "WikiRepoSymbol" in {t_["name"] for t_ in body["brief"]["top_symbols"]}


def test_repo_modules_endpoint(served):
    body = json.loads(_get(served + "/api/repo/team/app/modules"))
    assert body["repo"] == "team/app"
    assert body["modules"] == []  # fixture nodes have no `file` set -- honestly empty


def test_repo_modules_endpoint_wiki_param_flags_only_modules_with_a_generated_page(
        tmp_path, monkeypatch):
    # Regression for the Task 17 fix-round-1 Important finding: the module
    # picker must only ever offer modules that wiki generation actually wrote a
    # page for -- not the raw structural module list, which can be larger than
    # generation's own _MAX_MODULE_PAGES_PER_REPO cap (a module beyond the cap
    # is permanently stranded, per cmds.wiki's own docstring) or can simply
    # include a module the council never accepted a page for.
    from contextlake.kb.cmds.wiki import _module_wiki_filename

    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    s = SqliteStore(tmp_path / "index.sqlite")
    # three qualifying modules (>= repo_modules' default min_nodes=5 each)
    nodes = [Node(id=f"n{i}", repo="team/big", kind="function", name=f"n{i}",
                  lang="python", file=f) for i, f in enumerate(
        (["src/foo/a.py"] * 6) + (["src/bar/b.py"] * 6) + (["src/baz/c.py"] * 6))]
    s.upsert_repo(Repo(id="team/big", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="team/big", head_commit="h1", nodes=nodes, edges=[]))
    reindex_shard(s, tmp_path, "team/big")
    s.mark_indexed("team/big", "h1", "2026-06-01T00:00:00Z")
    # only "src/foo" got a generated page -- "src/bar" and "src/baz" are
    # structurally qualifying modules but have no page on disk (stranded past
    # the cap, or simply not yet generated/rejected).
    modules_dir = tmp_path / "wiki" / "_modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    (modules_dir / _module_wiki_filename("team/big", "src/foo")).write_text(
        "# team/big: src/foo\n\nGenerated at commit `h1`.\n\nThe **foo** subsystem.\n")
    port = _free_port()
    srv = build_dashboard_server(s, tmp_path, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        deeper = json.loads(_get(base + "/api/repo/team%2Fbig/modules?within=src&wiki=true"))
        by_prefix = {m["prefix"]: m["has_page"] for m in deeper["modules"]}
        assert by_prefix == {"src/foo": True, "src/bar": False, "src/baz": False}
    finally:
        srv.shutdown()
        s.close()


def test_repo_modules_endpoint_within_param_drills_one_level_deeper(tmp_path, monkeypatch):
    # `served`'s own fixture nodes have no `file`, so a dedicated store is used
    # here to prove `?within=` reaches data.repo_modules() and returns the
    # NEXT level down, not the same top-level answer again.
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    s = SqliteStore(tmp_path / "index.sqlite")
    # both >= repo_modules' default min_nodes=5
    nodes = [Node(id=f"n{i}", repo="team/big", kind="function", name=f"n{i}",
                  lang="python", file=f) for i, f in enumerate(
        (["src/foo/a.py"] * 6) + (["src/bar/b.py"] * 5))]
    s.upsert_repo(Repo(id="team/big", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="team/big", head_commit="h1", nodes=nodes, edges=[]))
    reindex_shard(s, tmp_path, "team/big")
    s.mark_indexed("team/big", "h1", "2026-06-01T00:00:00Z")
    port = _free_port()
    srv = build_dashboard_server(s, tmp_path, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        top = json.loads(_get(base + "/api/repo/team/big/modules"))
        assert [m["prefix"] for m in top["modules"]] == ["src"]
        deeper = json.loads(_get(base + "/api/repo/team/big/modules?within=src"))
        assert {m["prefix"] for m in deeper["modules"]} == {"src/foo", "src/bar"}
    finally:
        srv.shutdown()
        s.close()


def test_path_endpoint(served):
    found = json.loads(_get(served + "/api/path?from=checkout&to=CatalogService"))
    assert found["found"] and found["hops"] == 1
    missing = json.loads(_get(served + "/api/path?from=checkout&to=does-not-exist"))
    assert missing["found"] is False and missing["which"] == "to"
    # missing `to` -> a real 400, not a 500/traceback
    try:
        urllib.request.urlopen(served + "/api/path?from=checkout", timeout=5)
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_send_swallows_client_disconnect_errors(tmp_path):
    # a client (browser tab, curl) disconnecting mid-write must not surface a traceback --
    # ThreadingHTTPServer already isolates it to its own request thread, this only checks
    # send_bytes() itself doesn't propagate the write error.
    s = SqliteStore(tmp_path / "index.sqlite")
    try:
        srv = build_dashboard_server(s, tmp_path, host="127.0.0.1", port=_free_port())
        try:
            handler = object.__new__(srv.RequestHandlerClass)

            class _BrokenWfile:
                def write(self, _data):
                    raise BrokenPipeError()

            handler.wfile = _BrokenWfile()
            handler.send_response = lambda *a, **k: None
            handler.send_header = lambda *a, **k: None
            handler.end_headers = lambda: None
            handler.send_bytes(200, "text/plain", b"hello")  # must not raise
        finally:
            srv.server_close()
    finally:
        s.close()


def test_rail_links_keep_an_accessible_name_when_their_visible_label_is_hidden():
    # C2 (WCAG 4.1.2 Name/Role/Value, 2.4.4 Link Purpose): .cl-rail__label is
    # display:none whenever the rail is collapsed, the viewport narrows below
    # 1280px, or the page is zoomed to 200% (dashboard.css). The icon is
    # aria-hidden, so with the label also gone from the render tree, an
    # aria-label on the <a> itself is the only remaining name source. It must
    # mirror the visible text exactly, not paraphrase it -- a paraphrase fixes
    # 4.1.2 by introducing a fresh 2.5.3 Label in Name failure.
    from importlib.resources import files

    html = (files("contextlake.kb.dashboard") / "static" / "dashboard.html").read_text(
        encoding="utf-8")
    items = re.findall(r'<a class="cl-rail__item"[^>]*data-lens="[^"]*"[^>]*>.*?</a>', html)
    assert len(items) == 10  # Fleet, Repo, Architecture, Blast radius, Path,
    # Health, Search, Chat, MCP, Settings -- fails loudly if a lens is added
    # without carrying this fix forward, rather than silently passing on zero.
    for item in items:
        aria = re.search(r'aria-label="([^"]*)"', item)
        label = re.search(r'class="cl-rail__label">([^<]*)</span>', item)
        assert aria and label, item
        assert aria.group(1) == label.group(1), item


def test_shell_and_graph_routes(served):
    shell = _get(served + "/").lower()
    assert b"<html" in shell and b'id="app"' in shell
    graph = _get(served + "/graph/overview").lower()
    assert b"<html" in graph and b"cytoscape" in graph


def test_mermaid_asset_served_for_diagrams_tab(served):
    # vendored offline (kb/dashboard/static/), lazy-loaded client-side by
    # dashboard.js only when the Diagrams tab is first opened
    body = _get(served + "/mermaid.min.js")
    assert len(body) > 1_000_000  # the full ~3.5MB bundle, not a stub/404 page


def test_mcp_console_endpoint(served):
    body = json.loads(_get(served + "/api/mcp"))
    assert body["tool_count"] > 0
    names = {t["name"] for t in body["tools"]}
    assert "graph_stats" in names and "who_knows" in names
    from contextlake.launcher import launch_argv
    _cmd = launch_argv(portable=True)[0]
    assert body["mcp_json"]["mcpServers"]["contextlake"]["command"] == _cmd
    assert body["vscode_mcp_json"]["servers"]["contextlake"]["command"] == _cmd


def test_settings_endpoint(served):
    body = json.loads(_get(served + "/api/settings"))
    assert body["store_size_bytes"] > 0
    assert body["schema_version"]["running"] == body["schema_version"]["stored"]
    # Unset means all supported languages. Reported as "all" rather than [] so the
    # settings view cannot read as "no languages are indexed".
    assert body["languages"] == "all"
    assert set(body["embeddings"]) == {"enabled", "provider", "model"}
    assert body["sources"] == []


@pytest.fixture
def served_with_config(tmp_path):
    """Like ``served``, but started with a real --config so /api/mcp and
    /api/settings have a non-default kb.toml to reflect."""
    s = SqliteStore(tmp_path / "index.sqlite")
    s.upsert_repo(Repo(id="team/app", path=str(tmp_path), head_commit="h1"))

    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path.as_posix()}"\nlanguages = ["python", "go"]\n\n'
        '[[sources]]\nname = "jira"\ntype = "atlassian"\nenabled = true\n'
    )

    port = _free_port()
    srv = build_dashboard_server(s, tmp_path, host="127.0.0.1", port=port,
                                 config_path=str(cfg))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        s.close()


def test_settings_reflects_the_config_path_the_dashboard_was_started_with(served_with_config):
    body = json.loads(_get(served_with_config + "/api/settings"))
    assert body["languages"] == ["python", "go"]
    assert body["sources"] == [{"name": "jira", "type": "atlassian", "enabled": True}]
    assert body["mirror_root"] is not None


def test_mcp_json_snippet_carries_the_config_path(served_with_config):
    body = json.loads(_get(served_with_config + "/api/mcp"))
    args = body["mcp_json"]["mcpServers"]["contextlake"]["args"]
    assert "--config" in args
    assert args[args.index("--config") + 1].endswith("kb.toml")


# --- mutating routes (--allow-mutations) ------------------------------------

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "a.py").write_text("def a():\n    pass\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def served_with_mutations(tmp_path):
    """Like ``served``, but --allow-mutations, over a real git clone (mutating
    routes actually pull/index), with the per-launch token pulled out of the
    served JS the same way a real browser would pick it up. The store's repo
    path is a *clone* of ``origin`` -- ``origin`` is what the test commits new
    changes into, mirroring an upstream remote the clone can ``pull`` from."""
    origin = _init_git_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "index.sqlite")
    s.upsert_repo(Repo(id="acme/origin", path=str(clone), head_commit=head))
    s.mark_indexed("acme/origin", head, "2026-01-01T00:00:00Z")

    port = _free_port()
    srv = build_dashboard_server(s, store_dir, host="127.0.0.1", port=port,
                                 allow_mutations=True, workspace=str(tmp_path / "ws"))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        js = _get(base + "/dashboard.js").decode("utf-8")
        token = re.search(r'__CL_TOKEN__=("(?:[^"\\]|\\.)*")', js).group(1)
        token = json.loads(token)
        yield base, port, token, origin
    finally:
        srv.shutdown()
        s.close()


@pytest.fixture
def served_with_wiki_mutations(tmp_path, monkeypatch):
    """Like ``served_with_mutations``, but with an explicit --config pointed at
    this fixture's own isolated store. Required for /api/wiki/generate, which
    spawns a real ``contextlake wiki`` subprocess (see wiki_generate_start's
    docstring): without an explicit config, that subprocess falls back to the
    normal discovery chain, which on a real dev machine can resolve a
    real/production store -- HOME is isolated too, for the same reason
    ``served`` isolates it (defense in depth, not relying on --config alone).
    """
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "index.sqlite")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')

    port = _free_port()
    srv = build_dashboard_server(s, store_dir, host="127.0.0.1", port=port,
                                 allow_mutations=True, workspace=str(tmp_path / "ws"),
                                 config_path=str(cfg))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        js = _get(base + "/dashboard.js").decode("utf-8")
        token = re.search(r'__CL_TOKEN__=("(?:[^"\\]|\\.)*")', js).group(1)
        token = json.loads(token)
        yield base, token, store_dir
    finally:
        srv.shutdown()
        s.close()


def test_dashboard_sends_security_response_headers(served):
    """The dashboard is the origin that matters most: it serves the graph pages and
    the script carrying the per-process mutation/LLM token. Headers come from the
    shared base (kb/http_base.py), so this asserts the base is actually reached on
    both an HTML route and a JSON one -- including that ``connect-src 'self'``, the
    clause that blunts token exfiltration, is present."""
    for route in ("/", "/api/overview", "/dashboard.js"):
        with urllib.request.urlopen(served + route, timeout=5) as r:
            csp = r.headers["Content-Security-Policy"]
            assert csp, f"no CSP on {route}"
            assert "default-src 'none'" in csp
            assert "connect-src 'self'" in csp
            assert "frame-src 'self'" in csp
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["Referrer-Policy"] == "no-referrer"


def _get_status(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get_with_host(url, *, host=None):
    """GET returning (status, raw body). A custom "Host" makes http.client skip its
    own default and send this instead over the SAME real connection to 127.0.0.1 --
    the DNS-rebinding shape (attacker domain resolves here; only the header lies)."""
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_wiki_status_and_estimate_are_gated_by_mutations(served):
    # No --allow-mutations: status degrades to not-running, estimate is 404
    # (previewing a mutating action that isn't available makes no sense).
    assert json.loads(_get(served + "/api/wiki/status")) == {"running": False}
    status, _ = _get_status(served + "/api/wiki/estimate")
    assert status == 404


def test_wiki_estimate_endpoint(served_with_wiki_mutations):
    base, _token, store_dir = served_with_wiki_mutations
    status, body = _get_status(base + "/api/wiki/estimate")
    assert status == 200
    assert body == {"total": 0, "would_regenerate": 0, "unchanged": 0}


def test_wiki_generate_route_starts_and_reports_status(served_with_wiki_mutations):
    base, token, store_dir = served_with_wiki_mutations
    status, body = _post(base + "/api/wiki/generate", {}, token=token)
    assert status == 200
    assert body["ok"] is True
    assert "pid" in body
    for _ in range(50):
        poll = json.loads(_get(base + "/api/wiki/status"))
        if poll.get("finished"):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("wiki generation did not finish in time")
    assert poll["running"] is False
    # Regression: /api/wiki/generate must NOT be dispatched through _mutate's
    # store-lock window -- the spawned `contextlake wiki` child takes that same
    # lock itself (via _guard_store) at startup, as a different pid, and would
    # lose the race and exit with "store is busy" if the parent request handler
    # still held it. See _wiki_generate's docstring in server.py.
    assert "busy" not in poll["log_tail"].lower()


def test_wiki_generate_route_requires_mutations(served):
    status, body = _post(served + "/api/wiki/generate", {})
    assert status == 404


def _post(url, body, *, token=None, host=None):
    # A custom "Host" header in `headers` makes http.client skip its own default
    # Host and send this value instead, on the SAME real TCP connection the URL's
    # netloc resolves to -- exactly the DNS-rebinding shape the server guards
    # against (attacker domain resolves to 127.0.0.1; only the Host header lies).
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[TOKEN_HEADER] = token
    if host is not None:
        headers["Host"] = host
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            return e.code, json.loads(body_bytes or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


def test_chat_endpoint_works_without_any_flag(served):
    """Chat is not a mutation -- it must work on the plain `served` fixture
    (no --allow-mutations, no --llm-chat, no token) at the same risk level as
    any other read-only /api/* route."""
    status, body = _post(served + "/api/chat", {"question": "who calls CatalogService?"})
    assert status == 200
    assert body["llm_used"] is False
    assert body["structured"]["route"] == "callers"


def test_chat_requires_a_question(served):
    status, body = _post(served + "/api/chat", {})
    assert status == 400
    assert "question" in body["error"]


@pytest.fixture
def served_with_llm_chat(tmp_path, monkeypatch):
    """Like `served`, but --llm-chat with a stubbed LlmClient -- proves the
    endpoint's token gating and prose-synthesis wiring without a real provider
    or network call. An explicit (empty) --config keeps load_kb_config from
    touching this machine's real ambient config."""
    import contextlake.kb.llm.base as llm_base

    class _StubLlm:
        def generate(self, prompt, *, system=None):
            return "STUBBED ANSWER"

    monkeypatch.setattr(llm_base, "build_llm", lambda cfg: _StubLlm())

    s = SqliteStore(tmp_path / "index.sqlite")
    nodes = [
        Node(id="svc", repo="team/app", kind="class", name="CatalogService", lang="python"),
        Node(id="caller", repo="team/app", kind="function", name="checkout", lang="python"),
    ]
    edges = [Edge(src="caller", dst="svc", relation="calls",
                  confidence=Confidence.EXTRACTED, provenance=_PROV)]
    s.upsert_repo(Repo(id="team/app", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="team/app", head_commit="h1", nodes=nodes, edges=edges))
    reindex_shard(s, tmp_path, "team/app")

    cfg_file = tmp_path / "kb.toml"
    cfg_file.write_text("")

    port = _free_port()
    srv = build_dashboard_server(s, tmp_path, host="127.0.0.1", port=port,
                                 config_path=str(cfg_file), llm_chat=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        js = _get(base + "/dashboard.js").decode("utf-8")
        token = json.loads(re.search(r'__CL_TOKEN__=("(?:[^"\\]|\\.)*")', js).group(1))
        assert "window.__CL_LLM_CHAT__=true;" in js
        yield base, token
    finally:
        srv.shutdown()
        s.close()


def test_chat_requires_token_when_llm_chat_enabled(served_with_llm_chat):
    base, _token = served_with_llm_chat
    status, _body = _post(base + "/api/chat", {"question": "who calls CatalogService?"})
    assert status == 403


def test_chat_rejects_wrong_token_when_llm_chat_enabled(served_with_llm_chat):
    base, _token = served_with_llm_chat
    status, _body = _post(base + "/api/chat", {"question": "who calls CatalogService?"},
                          token="not-the-real-token")
    assert status == 403


def test_chat_with_correct_token_returns_llm_synthesized_answer(served_with_llm_chat):
    base, token = served_with_llm_chat
    status, body = _post(base + "/api/chat", {"question": "who calls CatalogService?"}, token=token)
    assert status == 200
    assert body["llm_used"] is True
    assert body["answer"] == "STUBBED ANSWER"
    assert body["structured"]["route"] == "callers"  # citations still present


def test_post_disabled_without_allow_mutations(served):
    status, _body = _post(served + "/api/repo/team/app/sync", {}, token="whatever")
    assert status == 404


def test_post_requires_token(served_with_mutations):
    base, _port, _token, _origin = served_with_mutations
    status, _ = _post(base + "/api/repo/acme/origin/sync", {})
    assert status == 403


def test_post_rejects_wrong_token(served_with_mutations):
    base, _port, _token, _origin = served_with_mutations
    status, _ = _post(base + "/api/repo/acme/origin/sync", {}, token="not-the-real-token")
    assert status == 403


def test_post_body_with_a_bad_int_field_is_400_not_a_dropped_connection(
        served_with_mutations):
    """`int(payload["port"])` inside _mutate is the same unguarded-int shape the
    query params had; the shared guard turns it into a 400 (and never starts an
    MCP server, since the parse fails first)."""
    base, _port, token, _origin = served_with_mutations
    status, body = _post(base + "/api/mcp/serve", {"action": "start", "port": "abc"},
                         token=token)
    assert status == 400 and body == {"error": "bad request"}


def test_post_rejects_mismatched_host_header(served_with_mutations):
    base, port, token, _origin = served_with_mutations
    status, _ = _post(base + "/api/repo/acme/origin/sync", {}, token=token, host="evil.example.com")
    assert status == 403


def test_wildcard_bind_says_so_at_startup():
    """A wildcard bind + Host pinning means the LAN address 403s with nothing on
    screen to explain it, so the bind says it once at startup instead."""
    from contextlake.kb.http_base import host_pinning_hint

    assert host_pinning_hint("127.0.0.1", 8765) is None
    assert host_pinning_hint("192.0.2.10", 8765) is None
    hint = host_pinning_hint("0.0.0.0", 8765)
    assert hint and "localhost:8765" in hint and "--host" in hint


def test_get_rejects_mismatched_host_header(served):
    """DNS rebinding: GET used to skip the Host check do_POST has always had, so a
    page on an attacker domain that re-resolved to 127.0.0.1 could read the whole
    graph cross-origin (CORS doesn't help -- rebinding makes the browser believe
    it is same-origin). Every GET route is pinned now, assets included."""
    for route in ("/api/overview", "/api/search?q=CatalogService", "/graph/overview", "/"):
        status, _body = _get_with_host(served + route, host="evil.example.com")
        assert status == 403, route
        assert _get_with_host(served + route)[0] == 200, route


def test_get_host_check_covers_the_token_carrying_asset(served_with_mutations):
    """dashboard.js embeds the per-process mutation token, so the asset routes are
    the LAST place an exemption could be justified: leaking it hands a rebinding
    page the key to the mutating routes."""
    base, _port, token, _origin = served_with_mutations
    status, body = _get_with_host(base + "/dashboard.js")
    assert status == 200 and token.encode() in body
    assert _get_with_host(base + "/dashboard.js", host="evil.example.com")[0] == 403


def test_non_numeric_query_param_is_400_json_not_a_dropped_connection(served):
    """`int(q["depth"][0])` with no try raised inside the handler thread, so the
    client got a traceback on stderr and a closed socket instead of a response."""
    status, body = _get_status(served + "/api/overview?depth=abc")
    assert status == 400
    assert "depth" in body["error"]
    # an absent/empty param still means "use the default", not 400
    assert _get_status(served + "/api/overview")[0] == 200
    assert _get_status(served + "/api/overview?depth=")[0] == 200


def test_out_of_range_query_params_clamp_rather_than_error(served):
    status, body = _get_status(served + "/api/impact?node=svc&hops=99999&limit=99999")
    assert status == 200 and body["found"] is True
    assert _get_status(served + "/api/search?q=CatalogService&limit=-5")[0] == 200
    assert _get_status(served + "/api/path?from=caller&to=svc&max_hops=99999")[0] == 200


def test_internal_error_is_a_generic_500_with_the_traceback_only_in_the_log(
        served, monkeypatch, gls_logs):
    import contextlake.kb.dashboard.data as kbdata

    def _boom(*_a, **_k):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(kbdata, "fleet_overview", _boom)
    status, body = _get_status(served + "/api/overview")
    assert status == 500
    assert body == {"error": "internal server error"}
    assert "secret-internal-detail" not in json.dumps(body)
    assert "secret-internal-detail" in gls_logs.text and "Traceback" in gls_logs.text


def test_sync_route_pulls_and_reindexes(served_with_mutations):
    base, _port, token, origin = served_with_mutations
    (origin / "b.py").write_text("def b():\n    pass\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "add b")

    status, body = _post(base + "/api/repo/acme/origin/sync", {}, token=token)
    assert status == 200
    assert body["ok"] is True
    assert body["changed"] is True


def test_add_repo_route_end_to_end(served_with_mutations, tmp_path):
    base, _port, token, _origin = served_with_mutations
    second = _init_git_repo(tmp_path / "second-origin")
    status, body = _post(base + "/api/repo/add", {"url": str(second)}, token=token)
    # A bare local path is rejected by validate_clone_url (see test_kb_dashboard_mutations.py) --
    # this proves the *route* enforces the same policy the unit tests cover directly.
    assert status == 200
    assert body["ok"] is False
    assert "unsupported URL" in body["error"]


def test_add_repo_route_requires_url(served_with_mutations):
    base, _port, token, _origin = served_with_mutations
    status, body = _post(base + "/api/repo/add", {}, token=token)
    assert status == 400


def test_mcp_serve_route_requires_known_action(served_with_mutations):
    base, _port, token, _origin = served_with_mutations
    status, body = _post(base + "/api/mcp/serve", {"action": "explode"}, token=token)
    assert status == 400


@pytest.mark.parametrize("action", ["start", "restart"])
@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.0.2.10", "::", "evil.example.com",
     # A JSON body is arbitrary client input: a wrong-typed host must read as a
     # rejected request (400), never a server fault (500). An unhashable value
     # would raise TypeError out of the `in` test if it weren't type-checked.
     ["0.0.0.0"], {"host": "0.0.0.0"}, 1234, True],
)
def test_mcp_serve_route_refuses_a_non_loopback_bind(served_with_mutations, action, host):
    """The MCP server this route spawns is unauthenticated, so its bind address
    can't be caller-controlled: one token (read out of /dashboard.js, or used by
    script injected into the page) would otherwise turn a loopback dashboard
    into a public graph server that outlives it. ``restart`` is covered too --
    it is ``mcp_start`` behind a different name, and a guard on only one of the
    two spawning actions is no guard. Every cell here 400s *before* any process
    is spawned, which is why this test can sweep them without cleaning up."""
    base, _port, token, _origin = served_with_mutations
    status, body = _post(base + "/api/mcp/serve", {"action": action, "host": host},
                         token=token)
    assert status == 400
    assert "loopback" in body["error"]


@pytest.mark.parametrize("port", [80, 443, 1023, -1, 70000])
def test_mcp_serve_route_refuses_a_privileged_or_out_of_range_port(
        served_with_mutations, port):
    """Nothing this tool spawns should want a privileged port; a request for one
    is a mistake or an attempt to squat a service port. (0 and "" are absent
    from this list on purpose: ``payload.get("port") or 8766`` reads both as
    "unset", so they take the default -- asserting a refusal for them would
    encode a spawn-the-real-server path as a requirement.)"""
    base, _port, token, _origin = served_with_mutations
    status, body = _post(base + "/api/mcp/serve", {"action": "start", "port": port},
                         token=token)
    assert status == 400
    assert "port must be" in body["error"]


@pytest.mark.parametrize("action", ["start", "restart"])
def test_mcp_serve_route_dispatches_the_right_spawner_with_loopback_defaults(
        served_with_mutations, monkeypatch, action):
    """The accepting half of the bind guard: start and restart share one branch,
    so a swapped ternary there would send every 'restart' to mcp_start (and vice
    versa) while every refusal test above still passed. Stubs stand in for the
    real spawners -- this pins the dispatch and the defaults a request that omits
    host/port lands on, without leaving an MCP process behind."""
    seen = {}

    def _fake(name):
        def spawn(store_dir, **kw):
            seen.update(name=name, **kw)
            return {"ok": True, "running": True}
        return spawn

    # Patched on the mutations module, not on server.py: server.py holds a
    # reference to the module and resolves the attribute per call.
    monkeypatch.setattr("contextlake.kb.dashboard.mutations.mcp_start", _fake("start"))
    monkeypatch.setattr("contextlake.kb.dashboard.mutations.mcp_restart", _fake("restart"))

    base, _port, token, _origin = served_with_mutations
    status, body = _post(base + "/api/mcp/serve", {"action": action}, token=token)
    assert status == 200 and body["ok"] is True
    assert seen["name"] == action
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8766


def test_mutation_returns_409_when_store_is_locked(served_with_mutations, tmp_path):
    # StoreLock treats same-pid re-entry as the same writer, not a live peer (a
    # single process's own threads must never deadlock each other) -- so a real
    # "busy" test needs a lock file recording a DIFFERENT, genuinely-alive pid.
    # The test runner's parent process fits: alive, and never equal to our own.
    base, _port, token, _origin = served_with_mutations
    lock_path = tmp_path / "store" / ".contextlake.lock"
    lock_path.write_text(json.dumps({
        "pid": os.getppid(), "command": "some-other-writer",
        "host": socket.gethostname(), "started": int(time.time()),
    }))
    try:
        status, body = _post(base + "/api/repo/acme/origin/sync", {}, token=token)
        assert status == 409
        assert body["error"] == "store is busy"
    finally:
        lock_path.unlink(missing_ok=True)


def test_mcp_console_reports_mutations_capability(served_with_mutations):
    base, _port, _token, _origin = served_with_mutations
    body = json.loads(_get(base + "/api/mcp"))
    assert body["mutations"] is True
    assert body["http_server"] == {"running": False}


def test_settings_reports_mutations_disabled_by_default(served):
    body = json.loads(_get(served + "/api/settings"))
    assert body["mutations"] is False


def test_capabilities_endpoint(served, served_with_mutations):
    off = json.loads(_get(served + "/api/capabilities"))
    assert off == {"mutations": False}
    base, _port, _token, _origin = served_with_mutations
    on = json.loads(_get(base + "/api/capabilities"))
    assert on == {"mutations": True}


def test_serve_dashboard_port_in_use_fails_clean_not_traceback(tmp_path, monkeypatch, gls_logs):
    """A port collision (the common case: a previous `dashboard --serve` still
    running) used to propagate a raw OSError all the way to a traceback at the
    CLI -- found live, running against a real repo. serve_dashboard must catch
    it, log something actionable, and return a failure code instead."""
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    port = _free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        rc = serve_dashboard(tmp_path, host="127.0.0.1", port=port)
        assert rc == 1
        assert "could not start the dashboard" in gls_logs.text.lower()
    finally:
        blocker.close()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url, tries=50):
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - server may still be starting
            last = e
            time.sleep(0.05)
    raise AssertionError(f"request failed: {last}")


# --- generated documents (docs/api, docs/design) -------------------------------------


def _write_doc(store_dir, kind, repo_id, commit, body):
    """A generated page written the way `kb docs` writes one.

    Uses the product's own `stamp()` and directory constants rather than a hand-typed
    marker, so a change to either shape breaks this test instead of quietly making it
    describe a format nothing writes any more.
    """
    from contextlake.kb.cmds.docs import API_DIR, DESIGN_DIR
    from contextlake.kb.docs.stamp import stamp
    from contextlake.kb.visualize import repo_slug

    d = store_dir.joinpath(*(API_DIR if kind == "api" else DESIGN_DIR))
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# {repo_id}", "", *stamp(kind, repo_id, commit), body, ""]
    f = d / (repo_slug(repo_id) + ".md")
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


def test_repo_docs_endpoint_serves_both_kinds(served, tmp_path):
    _write_doc(tmp_path, "api", "team/app", "h1", "The **api** reference.")
    _write_doc(tmp_path, "design", "team/app", "h1", "The **design** notes.")

    api = json.loads(_get(served + "/api/repo/team/app/docs"))
    assert api["found"] and api["kind"] == "api" and api["stale"] is False
    assert "<strong>api</strong>" in api["html"]
    assert api["doc_commit"] == "h1" and api["current_commit"] == "h1"

    design = json.loads(_get(served + "/api/repo/team/app/docs?kind=design"))
    assert design["found"] and design["kind"] == "design" and design["stale"] is False
    assert "<strong>design</strong>" in design["html"]


def test_repo_docs_reports_stale_when_the_page_describes_another_commit(served, tmp_path):
    # The repo's indexed head is h1; this page was generated from h0.
    _write_doc(tmp_path, "api", "team/app", "h0", "Written before the code moved.")
    out = json.loads(_get(served + "/api/repo/team/app/docs"))
    assert out["found"] is True
    assert out["stale"] is True, "a page generated from a different commit must read stale"
    assert out["doc_commit"] == "h0" and out["current_commit"] == "h1"


def test_repo_docs_treats_an_unstamped_page_as_stale(served, tmp_path):
    # A page written before generated documents carried a commit. Not knowing and being
    # out of date are the same risk to a caller, so it must not read as fresh.
    from contextlake.kb.cmds.docs import API_DIR
    from contextlake.kb.visualize import repo_slug

    d = tmp_path.joinpath(*API_DIR)
    d.mkdir(parents=True, exist_ok=True)
    (d / (repo_slug("team/app") + ".md")).write_text("# team/app\n\nNo marker here.\n")

    out = json.loads(_get(served + "/api/repo/team/app/docs"))
    assert out["found"] is True and out["stale"] is True
    assert out["doc_commit"] is None


def test_repo_docs_refuses_an_unknown_kind_by_name(served, tmp_path):
    # "no such kind" and "not generated yet" are different answers and a caller acts on
    # them differently, so the note must not claim the page is merely missing.
    _write_doc(tmp_path, "api", "team/app", "h1", "present")
    out = json.loads(_get(served + "/api/repo/team/app/docs?kind=bogus"))
    assert out["found"] is False and out["stale"] is True
    assert "not a kind" in out["note"]
    assert "kb docs" not in out["note"], "an unknown kind is not a 'run kb docs' situation"


def test_repo_docs_reports_a_missing_page_without_claiming_the_repo_is_unknown(served):
    out = json.loads(_get(served + "/api/repo/team/app/docs"))
    assert out["found"] is False and out["html"] is None
    assert "kb docs" in out["note"]


@pytest.fixture
def served_with_docs_collision(tmp_path, monkeypatch):
    # A repo literally named "team/docs" alongside an ordinary "team/app", so the
    # sub-route marker and a real repo id are the same string. Mirrors
    # served_with_wiki_collision; the two sub-routes share the collision, so they
    # share the shape of the regression test.
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    s = SqliteStore(tmp_path / "index.sqlite")
    nodes = [
        Node(id="docs_sym", repo="team/docs", kind="class", name="DocsRepoSymbol", lang="python"),
        Node(id="app_sym", repo="team/app", kind="class", name="AppRepoSymbol", lang="python"),
    ]
    s.upsert_repo(Repo(id="team/docs", path=str(tmp_path), head_commit="hd"))
    s.upsert_repo(Repo(id="team/app", path=str(tmp_path), head_commit="ha"))
    write_shard(tmp_path, GraphShard(repo="team/docs", head_commit="hd",
                                     nodes=[nodes[0]], edges=[]))
    write_shard(tmp_path, GraphShard(repo="team/app", head_commit="ha",
                                     nodes=[nodes[1]], edges=[]))
    reindex_shard(s, tmp_path, "team/docs")
    reindex_shard(s, tmp_path, "team/app")
    s.mark_indexed("team/docs", "hd", "2026-06-01T00:00:00Z")
    s.mark_indexed("team/app", "ha", "2026-06-01T00:00:00Z")
    port = _free_port()
    srv = build_dashboard_server(s, tmp_path, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        s.close()


def test_repo_docs_route_does_not_hijack_a_repo_literally_named_docs(served_with_docs_collision):
    # GET /api/repo/team/docs must return team/docs's OWN repo-detail payload, not be
    # misrouted into the sub-route as if "team" were the repo and "docs" the marker.
    # Full-match-wins, the precedence the /wiki block established.
    base = served_with_docs_collision
    detail = json.loads(_get(base + "/api/repo/team/docs"))
    assert detail.get("repo") == "team/docs", "a real repo named team/docs was hijacked"
    assert "found" not in detail or "kind" not in detail, "got a docs payload, not repo detail"

    # The control: the sub-route still works for a repo that is NOT a collision.
    scoped = json.loads(_get(base + "/api/repo/team/app/docs"))
    assert scoped["kind"] == "api" and scoped["found"] is False


# --- /api/docs/generate and /api/docs/status ---------------------------------


@pytest.fixture
def served_with_docs_mutations(tmp_path, monkeypatch):
    """Like ``served_with_wiki_mutations``, for the docs routes.

    Same two isolations and the same reason: ``/api/docs/generate`` spawns a real
    ``contextlake kb docs`` subprocess, and without an explicit ``--config`` that
    child falls back to the normal discovery chain, which on a real dev machine can
    resolve a production store.

    Holds ONE indexed repo "r" rather than an empty store. An empty store writes 0
    documents, and 0 is also what a dead counter returns, so every count asserted
    against it held at any value the counter could produce. With one indexed repo
    the two scopes give different numbers: unscoped writes 3 (both documents plus
    the whole-store fleet page), scoped to "r" writes 2.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "index.sqlite")
    s.upsert_repo(Repo(id="r", path=str(tmp_path)))
    write_shard(store_dir, GraphShard(
        repo="r", head_commit="abc123",
        nodes=[Node(id="a", repo="r", kind="function", name="a", file="a.py"),
               Node(id="b", repo="r", kind="function", name="b", file="a.py")],
        edges=[Edge(src="a", dst="b", relation="calls",
                    confidence=Confidence.EXTRACTED, provenance=_PROV)]))
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')

    port = _free_port()
    srv = build_dashboard_server(s, store_dir, host="127.0.0.1", port=port,
                                 allow_mutations=True, workspace=str(tmp_path / "ws"),
                                 config_path=str(cfg))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        js = _get(base + "/dashboard.js").decode("utf-8")
        token = re.search(r'__CL_TOKEN__=("(?:[^"\\]|\\.)*")', js).group(1)
        token = json.loads(token)
        yield base, token, store_dir
    finally:
        srv.shutdown()
        s.close()


def test_docs_generate_route_starts_and_reports_status(served_with_docs_mutations):
    base, token, store_dir = served_with_docs_mutations
    status, body = _post(base + "/api/docs/generate", {}, token=token)
    assert status == 200
    assert body["ok"] is True
    assert "pid" in body
    for _ in range(50):
        poll = json.loads(_get(base + "/api/docs/status"))
        if poll.get("finished"):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("docs generation did not finish in time")
    assert poll["running"] is False
    # An unscoped run over the fixture's one indexed repo writes both its documents
    # and the whole-store fleet page: 3. Asserted as 3 and not 0 so the row cannot
    # be satisfied by a counter that returns a constant -- the scoped row below
    # asserts 2 on the same store.
    assert poll["documents_written"] == 3
    assert (store_dir / "docs" / "fleet" / "design.md").is_file()
    # Regression, the same one /api/wiki/generate carries: this route must NOT be
    # dispatched through _mutate's store-lock window. The spawned `contextlake kb
    # docs` child takes that same lock itself (via _guard_store) at startup, as a
    # different pid, and loses the race if the parent request handler still holds
    # it. See _docs_generate's docstring.
    #
    # Asserted on the words _guard_store actually prints. The wiki test beside this
    # one probes for "busy", which is the StoreBusy exception's own wording and is
    # NOT what the child logs (_common.py:128-133 prints "refusing to run
    # concurrently"), so that probe cannot see the defect it is written for.
    assert "refusing to run concurrently" not in poll["log_tail"].lower(), poll["log_tail"]


def test_docs_generate_route_is_registered_only_behind_mutations_and_the_token(
        served_with_docs_mutations, served):
    """Three assertions, and only the first one discriminates.

    (a) DISCRIMINATING: mutations on plus the right token returns 200. On unmodified
        code this path reached ``_mutate`` and fell through to its own 404.
    (b) GUARD ONLY, passes on unmodified code: a wrong token is 403, because
        ``do_POST`` checks the token before any path dispatch.
    (c) GUARD ONLY, passes on unmodified code: with mutations off the POST is 404,
        from the ``if not allow_mutations`` check above that dispatch.

    (b) and (c) are not tests of this change. They catch a future edit that
    registers this route or its handler above those two gates, which is the only way
    they could start failing.
    """
    base, token, _store_dir = served_with_docs_mutations

    status, body = _post(base + "/api/docs/generate", {}, token=token)
    assert status == 200 and body["ok"] is True

    status, _ = _post(base + "/api/docs/generate", {}, token="not-the-token")
    assert status == 403

    status, _ = _post(served + "/api/docs/generate", {})
    assert status == 404


def test_docs_status_degrades_to_not_running_without_mutations(served):
    """Without mutations the status route answers, it does not 404.

    A 404 here is invisible in the UI: ``docsRegenerateCard``'s poll would report
    the failure, but the wiki card's equivalent ``.catch`` swallows it and leaves
    the card reading "Idle" with the run finished. Matching the wiki route's own
    degradation keeps the two consistent."""
    assert json.loads(_get(served + "/api/docs/status")) == {"running": False}


def _poll_until_finished(base, tries=100):
    for _ in range(tries):
        poll = json.loads(_get(base + "/api/docs/status"))
        if poll.get("finished"):
            return poll
        time.sleep(0.1)
    raise AssertionError("docs generation did not finish in time")


def test_docs_generate_route_carries_the_repo_the_card_sent(served_with_docs_mutations):
    """The repo the card sent has to reach the child, not be dropped on the way.

    Dropping it is silent and it inverts the button's meaning: a per-repo control
    starts a fleet-wide run, which on a 480-repo store is the failure this whole
    change exists to remove. Both new route rows used to POST ``{}``, so the field
    was never carried end to end by any test.

    Three assertions, each of which a dropped repo breaks: the pidfile records the
    scope it was started with, a scoped run writes 2 documents and not 3, and it
    writes no whole-store fleet page (``cmd_docs`` writes that only for a run that
    covered every indexed repo).
    """
    base, token, store_dir = served_with_docs_mutations
    status, body = _post(base + "/api/docs/generate", {"repo": "r"}, token=token)
    assert status == 200 and body["ok"] is True

    poll = _poll_until_finished(base)
    assert poll["repo"] == "r"
    assert poll["documents_written"] == 2
    assert (store_dir / "docs" / "api" / "r.md").is_file()
    assert not (store_dir / "docs" / "fleet" / "design.md").exists()


@pytest.mark.parametrize("repo", ["--llm=openai", "--max-symbols=1", "--no-such-flag",
                                  "-r", "../../etc/passwd", "/etc/passwd", "a/../b"])
def test_docs_generate_refuses_a_repo_that_is_not_a_repo_id(served_with_docs_mutations, repo):
    """Argv injection through an HTTP endpoint, refused before the spawn.

    ``repo`` becomes a positional word in ``contextlake kb docs <word>``, and
    argparse reads a word beginning with ``-`` as a FLAG. ``--llm=openai`` turned on
    the model tier ``docs_generate_start``'s own docstring says it withholds, and
    the child made a real outbound request to a provider (measured: an HTTP 401 from
    the orientation path in the child's log).

    The status code alone would not prove the spawn was prevented, so this also
    asserts the two files a spawn always creates are absent, and it asserts the
    specific error body: a check that merely stripped a leading dash would still
    accept ``../../etc/passwd`` and ``/etc/passwd``.
    """
    base, token, store_dir = served_with_docs_mutations
    status, body = _post(base + "/api/docs/generate", {"repo": repo}, token=token)
    assert status == 400
    assert body == {"error": "invalid repo id"}
    assert not (store_dir / "dashboard" / "docs-gen.pid").exists()
    assert not (store_dir / "dashboard" / "docs-gen.log").exists()


@pytest.mark.parametrize("repo", ["--llm=openai", "-r", "../../etc/passwd"])
def test_wiki_generate_refuses_a_repo_that_is_not_a_repo_id(served_with_wiki_mutations, repo):
    """The wiki route shares the docs route's spawn shape and had the same hole:
    the same unchecked value reaching ``contextlake kb wiki <word>``. Fixed in both,
    so a reader who arrives at either one finds the check."""
    base, token, store_dir = served_with_wiki_mutations
    status, body = _post(base + "/api/wiki/generate", {"repo": repo}, token=token)
    assert status == 400
    assert body == {"error": "invalid repo id"}
    assert not (store_dir / "dashboard" / "wiki-gen.pid").exists()


@pytest.mark.parametrize("raw", ["   ", "\t", "\n"])
def test_docs_generate_refuses_a_whitespace_only_repo(served_with_docs_mutations, raw):
    """A whitespace-only value is not an absent one.

    Every shipped caller OMITS the field for a fleet-wide run (dashboard.js:
    ``if (repoId) body.repo = repoId``), so whitespace means a caller tried to name
    a repo and got it wrong. Reading it as absent silently escalates a per-repo
    button to the whole fleet, which is the same outcome as round 1's dropped-repo
    defect reached through a different value.
    """
    base, token, store_dir = served_with_docs_mutations
    status, body = _post(base + "/api/docs/generate", {"repo": raw}, token=token)
    assert status == 400
    assert body == {"error": "invalid repo id"}
    assert not (store_dir / "dashboard" / "docs-gen.pid").exists()


def test_validate_repo_id_refuses_a_backslash_for_the_windows_path():
    """POSIX allows a backslash in a directory name; ``repo_slug`` keeps it, and on
    Windows the page path built from this body field then resolves outside the docs
    directory. Neither writer of a repo id emits one, so refusing costs nothing and
    closes a traversal this process cannot see on Linux."""
    from contextlake.kb.dashboard import mutations as kbmut
    for bad in ("a\\b", "..\\..\\etc\\passwd", "a\\"):
        assert kbmut.validate_repo_id(bad) is None, bad
    # the shapes the two writers DO emit still pass
    for good in ("acme/billing", "gitlab.example.com:8443/acme/api", "repo@abc123"):
        assert kbmut.validate_repo_id(good) == good, good


@pytest.mark.parametrize("value", [
    # every provider cli.py's --llm choices declare
    "auto", "ollama", "openai", "builtin", "anthropic", "cli",
])
def test_wiki_generate_accepts_every_provider_the_cli_declares(served_with_wiki_mutations, value):
    """The POSITIVE direction, which is what was missing when this broke.

    ``_model_field`` shipped returning an error for every non-empty value, so this
    route could not start an LLM wiki run at all, and 116 targeted tests passed
    against both the broken and the working version. A validator with only
    rejection rows is indistinguishable from a validator that rejects everything.

    Asserting 200 rather than the argv: the argv builder is covered by
    tests/kb/test_kb_dashboard_mutations.py, and this route's job is to let the
    value through.
    """
    base, token, store_dir = served_with_wiki_mutations
    status, body = _post(base + "/api/wiki/generate", {"repo": "a", "llm": value},
                         token=token)
    assert status == 200, body
    assert body.get("ok") is True, body


@pytest.mark.parametrize("value", [
    "gpt-4o-mini", "llama3.1",
    # a real id carrying the ':' and '.' an over-strict rule would kill, and the
    # example _model_field's own docstring gives as legitimate
    "us.anthropic.claude-sonnet-4-v1:0",
    "meta/llama-3.1-8b", "qwen2.5-coder:7b",
])
def test_wiki_generate_accepts_a_real_model_name(served_with_wiki_mutations, value):
    """Model names carry ':' , '.' and '/'. Refusing those is the failure this row
    guards: it looks like validation and removes the feature."""
    base, token, store_dir = served_with_wiki_mutations
    status, body = _post(base + "/api/wiki/generate",
                         {"repo": "a", "llm": "builtin", "llm_model": value},
                         token=token)
    assert status == 200, body
    assert body.get("ok") is True, body


@pytest.mark.parametrize("field", ["llm", "llm_model"])
@pytest.mark.parametrize("value", ["--force", "-x", "builtin --force", "a\nb", "a\x7fb", 5, ["x"]])
def test_wiki_generate_refuses_a_model_value_that_could_reach_argv(
        served_with_wiki_mutations, field, value):
    """The negative direction. ``builtin --force`` is the interesting row: the rule
    is loose on ':' and '/', so refusing WHITESPACE is what keeps it a single
    rejected word instead of two argv words."""
    base, token, store_dir = served_with_wiki_mutations
    status, body = _post(base + "/api/wiki/generate", {"repo": "a", field: value},
                         token=token)
    assert status == 400
    assert body == {"error": f"invalid {field} value"}
    assert not (store_dir / "dashboard" / "wiki-gen.pid").exists()


@pytest.mark.parametrize("payload", [{"repo": 123}, {"repo": ["a"]}, {"repo": {"a": 1}}, []])
def test_docs_generate_answers_400_not_500_for_a_malformed_body(
        served_with_docs_mutations, payload):
    """A non-string ``repo``, or a JSON array as the whole body, used to reach
    ``.strip()`` on whatever JSON held and come back as a 500 with an unhandled
    AttributeError traceback. A traceback on this surface is an information leak as
    well as a defect, and the same handler already answered 400 for a body that was
    not JSON at all."""
    base, token, store_dir = served_with_docs_mutations
    status, _ = _post(base + "/api/docs/generate", payload, token=token)
    assert status == 400
    assert not (store_dir / "dashboard" / "docs-gen.pid").exists()


@pytest.mark.parametrize("payload", [{"repo": 123}, {"llm": 5}, {"llm_model": ["x"]},
                                     {"llm": "--force"}, []])
def test_wiki_generate_answers_400_not_500_for_a_malformed_body(
        served_with_wiki_mutations, payload):
    """Same 500, same line, in the wiki route -- plus its two extra argv fields.
    ``llm`` and ``llm_model`` are read with ``.strip()`` too, so a non-string is the
    same crash, and a value beginning with ``-`` is the same flag hazard."""
    base, token, _store_dir = served_with_wiki_mutations
    status, _ = _post(base + "/api/wiki/generate", payload, token=token)
    assert status == 400


def test_two_concurrent_docs_generate_posts_start_exactly_one_run(
        served_with_docs_mutations):
    """Two POSTs fired at one moment: one starts a run, the other is refused.

    Both used to answer ``ok: true``. The route-level in-progress check read the
    status and then spawned, which is two filesystem operations, and both requests
    cleared the read before either wrote the pidfile. Both children then opened the
    one log file with "w" and wrote at independent offsets, so the log the card
    shows was shredded: the loser's refusal partially overwrote the winner's output
    and neither line could be matched in full.

    NOT flaky by construction: the losing request reaches its claim within
    microseconds of the winner's, while the winner's child is still starting a
    Python interpreter. The one interleaving that would legitimately allow two runs
    needs the child to exit between the winner's pid write and the loser's status
    read, which that startup cost rules out here.
    """
    import threading as _threading

    base, token, store_dir = served_with_docs_mutations
    barrier = _threading.Barrier(2)
    replies = []
    lock = _threading.Lock()

    def fire():
        barrier.wait()
        reply = _post(base + "/api/docs/generate", {}, token=token)
        with lock:
            replies.append(reply)

    threads = [_threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(replies) == 2
    assert all(code == 200 for code, _ in replies), replies
    started = [b for _, b in replies if b.get("ok")]
    refused = [b for _, b in replies if not b.get("ok")]
    assert len(started) == 1, replies
    assert len(refused) == 1, replies
    assert "already in progress" in refused[0]["error"]

    poll = _poll_until_finished(base)
    # One writer means one clean log: the winner's completion line is intact, and
    # the refusal was never written into this file at all.
    assert "refusing to run concurrently" not in poll["log_tail"].lower(), poll["log_tail"]
    assert poll["documents_written"] == 3
