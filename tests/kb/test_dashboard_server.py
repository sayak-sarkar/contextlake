"""The dashboard HTTP server (kb/dashboard/server.py): JSON API + SPA shell routes.

Starts the server on an ephemeral port in a thread, hits the endpoints, shuts it down.
"""

import json
import socket
import threading
import time
import urllib.request
from datetime import date

import pytest

from contextlake.kb.dashboard.server import build_dashboard_server
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))


@pytest.fixture
def served(tmp_path):
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


def test_repo_detail_and_rel_endpoints(served):
    detail = json.loads(_get(served + "/api/repo/team/app"))
    assert detail["brief"]["node_count"] == 2
    rel = json.loads(_get(served + "/api/repo/team/app/rel"))
    assert set(rel) == {"dependencies", "http_flow", "event_flow"}


def test_shell_and_graph_routes(served):
    shell = _get(served + "/").lower()
    assert b"<html" in shell and b'id="app"' in shell
    graph = _get(served + "/graph/overview").lower()
    assert b"<html" in graph and b"cytoscape" in graph


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
