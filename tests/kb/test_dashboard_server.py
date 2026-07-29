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


def test_repo_modules_endpoint(served):
    body = json.loads(_get(served + "/api/repo/team/app/modules"))
    assert body["repo"] == "team/app"
    assert body["modules"] == []  # fixture nodes have no `file` set -- honestly empty


def test_send_swallows_client_disconnect_errors(tmp_path):
    # a client (browser tab, curl) disconnecting mid-write must not surface a traceback --
    # ThreadingHTTPServer already isolates it to its own request thread, this only checks
    # _send() itself doesn't propagate the write error.
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
            handler._send(200, "text/plain", b"hello")  # must not raise
        finally:
            srv.server_close()
    finally:
        s.close()


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
    assert body["mcp_json"]["mcpServers"]["contextlake"]["command"] == "contextlake"
    assert body["vscode_mcp_json"]["servers"]["contextlake"]["command"] == "contextlake"


def test_settings_endpoint(served):
    body = json.loads(_get(served + "/api/settings"))
    assert body["store_size_bytes"] > 0
    assert body["schema_version"]["running"] == body["schema_version"]["stored"]
    assert isinstance(body["languages"], list)
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


def test_post_rejects_mismatched_host_header(served_with_mutations):
    base, port, token, _origin = served_with_mutations
    status, _ = _post(base + "/api/repo/acme/origin/sync", {}, token=token, host="evil.example.com")
    assert status == 403


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
