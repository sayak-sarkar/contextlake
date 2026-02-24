"""Tests for steering-layer generation (facts, renderers, and the steer command)."""

import json
from argparse import Namespace

from contextlake.kb.commands import cmd_steer
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.steer.generate import (
    BEGIN,
    END,
    MARKER,
    mcp_server_entry,
    render_agents_md,
    workspace_facts,
)
from contextlake.kb.steer.skills import SKILLS, skill_files, skill_md
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore


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
        assert md.startswith("# AGENTS.md")  # the writer wraps this body in a managed block
        assert "2 repositories" in md
        assert "`team/api`" in md and "`team/ui`" in md  # repo list is specific
        assert "Cite, don't guess" in md  # guardrails present
        assert "contextlake serve --config /c/kb.toml" in md
    finally:
        store.close()


def test_skill_files_cover_both_tool_formats():
    files = skill_files()
    names = {s["name"] for s in SKILLS}
    for name in names:
        assert f".claude/skills/{name}/SKILL.md" in files
        assert f".windsurf/workflows/{name}.md" in files
    assert len(files) == 2 * len(SKILLS)


def test_skill_md_has_frontmatter_and_marker():
    md = skill_md(SKILLS[0])
    assert md.startswith("---\nname: ") and "description:" in md
    assert MARKER in md  # managed-file marker so steer can refresh idempotently


def test_mcp_server_entry():
    assert mcp_server_entry("/c/kb.toml") == {
        "command": "contextlake", "args": ["serve", "--config", "/c/kb.toml"]}
    assert mcp_server_entry(None) == {"command": "contextlake", "args": ["serve"]}


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
    # the generic skills/workflows library is installed too
    assert (out / ".claude" / "skills" / "use-knowledge-graph" / "SKILL.md").exists()
    assert (out / ".windsurf" / "workflows" / "ship-safely.md").exists()

    mcp = json.loads((out / ".mcp.json").read_text())
    assert "other" in mcp["mcpServers"]  # preserved
    assert mcp["mcpServers"]["gitlab-kb"]["command"] == "contextlake"


def test_cmd_steer_enhances_existing_files_without_clobbering(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    out = tmp_path / "ws"
    out.mkdir()
    # a hand-written file the user already had (no managed markers)
    (out / "AGENTS.md").write_text("# my agents file\n\nkeep this note\n")

    cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    text = (out / "AGENTS.md").read_text()
    assert "my agents file" in text and "keep this note" in text  # user content preserved
    assert BEGIN in text and END in text  # our managed block appended (enhanced)

    # re-running refreshes only our block — no duplication, user content intact
    cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    text2 = (out / "AGENTS.md").read_text()
    assert text2.count(BEGIN) == 1 and text2.count(END) == 1
    assert "keep this note" in text2


def test_cmd_steer_keeps_foreign_kiro_and_skill_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    out = tmp_path / "ws"
    out.mkdir()
    # a user's own Kiro steering doc and a same-named skill must survive
    (out / ".kiro" / "steering").mkdir(parents=True)
    (out / ".kiro" / "steering" / "my-rules.md").write_text("my kiro rules\n")
    (out / ".claude" / "skills" / "ship-safely").mkdir(parents=True)
    (out / ".claude" / "skills" / "ship-safely" / "SKILL.md").write_text("my own skill\n")

    cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    assert (out / ".kiro" / "steering" / "my-rules.md").read_text() == "my kiro rules\n"
    assert (out / ".claude" / "skills" / "ship-safely" / "SKILL.md").read_text() == "my own skill\n"
    assert (out / ".kiro" / "steering" / "workspace.md").exists()  # ours added alongside
