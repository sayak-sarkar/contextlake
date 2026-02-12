"""Tests for steering-layer generation (facts, renderers, and the steer command)."""

import json
from argparse import Namespace

from gitlab_sync.kb.commands import cmd_steer
from gitlab_sync.kb.model import Node, Repo
from gitlab_sync.kb.state import check_schema
from gitlab_sync.kb.steer.generate import (
    MARKER,
    mcp_server_entry,
    render_agents_md,
    workspace_facts,
)
from gitlab_sync.kb.store.shards import GraphShard, write_shard
from gitlab_sync.kb.store.sqlite_store import SqliteStore


def _seed(store_dir):
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    for rid, path, nodes in [
        ("team/api", "/w/team/api", [
            Node(id="a", repo="team/api", kind="class", name="OrderService", lang="python"),
            Node(id="p", repo="(packages)", kind="package", name="requests"),
        ]),
        ("team/ui", "/w/team/ui", [
            Node(id="b", repo="team/ui", kind="function", name="render", lang="typescript"),
            Node(id="p2", repo="(packages)", kind="package", name="requests"),
        ]),
    ]:
        store.upsert_repo(Repo(id=rid, path=path))
        write_shard(store_dir, GraphShard(repo=rid, head_commit="h", nodes=nodes, edges=[]))
        store.upsert_nodes(rid, nodes)
    return store


# --- facts + renderers -----------------------------------------------------

def test_workspace_facts(tmp_path):
    store = _seed(tmp_path)
    try:
        f = workspace_facts(store, tmp_path)
        assert f["count"] == 2
        assert set(f["languages"]) == {"python", "typescript"}
        assert "requests" in f["top_packages"]  # shared across both repos
        assert {r["id"] for r in f["per_repo"]} == {"team/api", "team/ui"}
    finally:
        store.close()


def test_render_agents_md_is_specific_and_guarded(tmp_path):
    store = _seed(tmp_path)
    try:
        md = render_agents_md(workspace_facts(store, tmp_path), config_path="/c/kb.toml")
        assert md.startswith(MARKER)
        assert "2 repositories" in md
        assert "`team/api`" in md and "`team/ui`" in md  # repo list is specific
        assert "Cite, don't guess" in md  # guardrails present
        assert "gitlab-sync serve --config /c/kb.toml" in md
    finally:
        store.close()


def test_mcp_server_entry():
    assert mcp_server_entry("/c/kb.toml") == {
        "command": "gitlab-sync", "args": ["serve", "--config", "/c/kb.toml"]}
    assert mcp_server_entry(None) == {"command": "gitlab-sync", "args": ["serve"]}


# --- command ---------------------------------------------------------------

def _cfg(tmp_path):
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    _seed(store_dir).close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')
    return str(cfg)


def test_cmd_steer_writes_files_and_merges_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    out = tmp_path / "ws"
    out.mkdir()
    # a pre-existing .mcp.json with another server must be preserved
    (out / ".mcp.json").write_text('{"mcpServers": {"other": {"command": "x"}}}')

    rc = cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    assert rc == 0

    assert MARKER in (out / "AGENTS.md").read_text()
    claude = (out / "CLAUDE.md").read_text()
    assert MARKER in claude and "@AGENTS.md" in claude  # CLAUDE.md imports AGENTS.md
    assert (out / ".windsurfrules").exists()
    assert (out / ".kiro" / "steering" / "workspace.md").exists()

    mcp = json.loads((out / ".mcp.json").read_text())
    assert "other" in mcp["mcpServers"]  # preserved
    assert mcp["mcpServers"]["gitlab-kb"]["command"] == "gitlab-sync"


def test_cmd_steer_skips_foreign_files_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    out = tmp_path / "ws"
    out.mkdir()
    (out / "AGENTS.md").write_text("# my hand-written agents file\n")  # no MARKER

    cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    assert "hand-written" in (out / "AGENTS.md").read_text()  # not clobbered

    cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=True))
    assert MARKER in (out / "AGENTS.md").read_text()  # --force replaces it
