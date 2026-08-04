"""Tests for steering-layer generation (facts, renderers, and the steer command)."""

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from contextlake.kb.cmds.steer import _implicit_binding
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
            Node(id="a", repo="team/api", kind="class", name="CatalogService", lang="python"),
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
        assert "contextlake kb serve --config /c/kb.toml" in md
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


def test_generated_steering_points_at_the_namespaced_serve_command():
    """.mcp.json and AGENTS.md land in users' repos and are read by an editor,
    not a terminal -- a stale command here fails opaquely inside the editor.
    `steer --force` rewrites them; this guards what it writes."""
    entry = mcp_server_entry(None)
    assert entry["args"][:2] == ["kb", "serve"]
    assert entry["args"][0] != "serve"


def test_mcp_server_entry():
    assert mcp_server_entry("/c/kb.toml") == {
        "command": "contextlake", "args": ["kb", "serve", "--config", "/c/kb.toml"]}
    assert mcp_server_entry(None) == {"command": "contextlake", "args": ["kb", "serve"]}


def test_render_agents_md_sanitizes_a_repo_id_and_package_name_that_embed_markers(tmp_path):
    """A repo id or package name (the latter reachable verbatim via manifest.py's
    unvalidated package.json dependency-key parsing) that carries a backtick or the
    literal END marker text used to break out of its markdown code span or smuggle
    the marker mid-body -- corrupting the *next* `steer` refresh's BEGIN/END splice.
    """
    store = SqliteStore(tmp_path / "index.sqlite")
    check_schema(store)
    poisoned_repo = f"team/evil`{END}"
    poisoned_pkg = f"evil`pkg`{END}"
    nodes = [
        Node(id="e", repo=poisoned_repo, kind="class", name="X", lang="python"),
        Node(id="ep", repo="(packages)", kind="package", name=poisoned_pkg),
    ]
    store.upsert_repo(Repo(id=poisoned_repo, path="/w/evil"))
    write_shard(tmp_path, GraphShard(repo=poisoned_repo, head_commit="h",
                                      nodes=nodes, edges=[]))
    store.upsert_nodes(poisoned_repo, nodes)
    try:
        md = render_agents_md(workspace_facts(store, tmp_path), config_path=None)
    finally:
        store.close()
    assert END not in md
    assert "evil`pkg`" not in md and "evil`" + END not in md


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
    assert mcp["mcpServers"]["contextlake-kb"]["command"] == "contextlake"


def test_cmd_steer_writes_vscode_mcp_json_under_servers_key(tmp_path, monkeypatch):
    """VS Code's .vscode/mcp.json uses a `servers` top-level key, not `mcpServers`
    -- a distinct schema from Claude Code/Windsurf/Cursor's .mcp.json."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    out = tmp_path / "ws"
    out.mkdir()
    (out / ".vscode").mkdir()
    (out / ".vscode" / "mcp.json").write_text('{"servers": {"other": {"command": "x"}}}')

    rc = cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    assert rc == 0

    vscode = json.loads((out / ".vscode" / "mcp.json").read_text())
    assert "mcpServers" not in vscode
    assert "other" in vscode["servers"]  # preserved
    assert vscode["servers"]["contextlake-kb"]["command"] == "contextlake"
    assert vscode["servers"]["contextlake-kb"]["args"][:2] == ["kb", "serve"]


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


def test_cmd_steer_keeps_a_locally_edited_skill_file_without_force(tmp_path, monkeypatch):
    """A skill/workflow file has no END marker bounding "our" content (unlike
    AGENTS.md's managed block), so MARKER-presence alone can't tell a still-
    pristine contextlake-managed file from one the user edited since -- must
    not silently discard the user's edit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    out = tmp_path / "ws"
    out.mkdir()

    # first run installs the pristine skill file
    cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    p = out / ".claude" / "skills" / "ship-safely" / "SKILL.md"
    original = p.read_text()
    assert MARKER in original

    # the user edits the generated file in place (still carries MARKER)
    edited = original + "\n<!-- my own note, added after generation -->\n"
    p.write_text(edited)

    # re-running steer must NOT silently overwrite the user's edit
    cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    assert p.read_text() == edited

    # --force is the explicit opt-in to overwrite it
    cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=True))
    assert p.read_text() == original


def test_cmd_steer_refuses_to_splice_a_file_with_a_duplicated_begin_marker(tmp_path, monkeypatch):
    """If a BEGIN or END marker somehow appears more than once (e.g. a pre-sanitizer
    poisoned name once smuggled a duplicate END mid-body), splicing at the first
    occurrence would corrupt the file. Must refuse and leave the file untouched
    rather than guess which occurrence is the real boundary."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    out = tmp_path / "ws"
    out.mkdir()
    malformed = f"intro\n{BEGIN}\nbody one\n{END}\nmiddle\n{BEGIN}\nbody two\n{END}\ntail\n"
    (out / "AGENTS.md").write_text(malformed)

    rc = cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    assert rc == 0  # the run as a whole still succeeds -- other files still refresh
    assert (out / "AGENTS.md").read_text() == malformed  # untouched, not guessed at


def test_merge_mcp_entry_self_heals_a_malformed_wrapper_key(tmp_path, monkeypatch):
    """An existing .mcp.json with mcpServers set to something other than an object
    (null, a list, ...) used to crash cmd_steer uncaught mid-run, leaving the
    workspace half-steered (markdown/skills already written, MCP configs not)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    out = tmp_path / "ws"
    out.mkdir()
    (out / ".mcp.json").write_text('{"mcpServers": null}')

    rc = cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False))
    assert rc == 0
    mcp = json.loads((out / ".mcp.json").read_text())
    assert mcp["mcpServers"]["contextlake-kb"]["command"] == "contextlake"


def test_cmd_steer_resolves_a_relative_config_path_to_absolute(tmp_path, monkeypatch):
    """A relative --config value used to be embedded verbatim into the generated
    .mcp.json / markdown, valid only from the directory `steer` happened to be
    invoked from -- not from `out`, where an MCP client actually launches it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    _seed(store_dir).close()
    cfg_dir = tmp_path / "elsewhere"
    cfg_dir.mkdir()
    cfg = cfg_dir / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')
    out = tmp_path / "ws"
    out.mkdir()

    monkeypatch.chdir(cfg_dir)
    rc = cmd_steer(Namespace(config="kb.toml", out=str(out), workspace=None, force=False))
    assert rc == 0

    mcp = json.loads((out / ".mcp.json").read_text())
    args = mcp["mcpServers"]["contextlake-kb"]["args"]
    resolved = args[args.index("--config") + 1]
    assert Path(resolved).is_absolute()
    assert Path(resolved) == cfg.resolve()


# --- store provenance (a generated file must name the store it came from) ---
#
# `--out` picks where files are written; the config chain picks the store. They
# are resolved independently, so `kb steer --force` run from the wrong place
# rewrote a correct 5,500-symbol AGENTS.md down to "2 symbols, 1 relations" with
# exit 0 and no warning. Every number was right for the store that resolved --
# nothing in the output said which store that was.


def test_workspace_facts_carries_the_store_it_read(tmp_path):
    store = _seed(tmp_path)
    try:
        facts = workspace_facts(store, tmp_path)
    finally:
        store.close()
    assert facts["store"] == str(tmp_path)


def test_render_agents_md_names_the_store_it_was_built_from(tmp_path):
    store = _seed(tmp_path)
    try:
        facts = workspace_facts(store, tmp_path)
    finally:
        store.close()
    md = render_agents_md(facts)
    assert str(tmp_path) in md
    # The counts and the store that produced them must travel together -- the
    # counts alone were what made the wrong-store rewrite look plausible.
    assert f"{facts['nodes']} symbols" in md
    assert md.startswith("# AGENTS.md")  # provenance goes inside, not above, the H1


def test_cmd_steer_records_the_store_in_the_generated_agents_md(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    out = tmp_path / "ws"
    out.mkdir()

    assert cmd_steer(Namespace(config=cfg, out=str(out), workspace=None, force=False)) == 0

    md = (out / "AGENTS.md").read_text()
    # Asserted as a backticked code span, not a bare substring: the config path
    # ".../kb.toml" already contains ".../kb", so a bare `in` check passes even
    # with no provenance line at all.
    assert "from the knowledge store at" in md
    assert f"`{tmp_path / 'kb'}`" in md


def test_steer_provenance_survives_a_refresh_from_a_different_store(tmp_path, monkeypatch):
    """The actual incident: same --out, a second run against a smaller store.

    The rewrite still happens (that is what steer does), but the file now says
    which store produced the numbers, so the swap is visible in the diff."""
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / "ws"
    out.mkdir()
    real = _cfg(tmp_path)
    assert cmd_steer(Namespace(config=real, out=str(out), workspace=None, force=False)) == 0
    before = (out / "AGENTS.md").read_text()

    stub_dir = tmp_path / "stub"
    (stub_dir / "kb").mkdir(parents=True)
    SqliteStore(stub_dir / "kb" / "index.sqlite").close()
    check_schema(SqliteStore(stub_dir / "kb" / "index.sqlite"))
    stub_cfg = stub_dir / "kb.toml"
    stub_cfg.write_text(f'[kb]\nstore_dir = "{(stub_dir / "kb").as_posix()}"\n')

    assert cmd_steer(
        Namespace(config=str(stub_cfg), out=str(out), workspace=None, force=True)) == 0
    after = (out / "AGENTS.md").read_text()

    assert before != after
    assert f"`{stub_dir / 'kb'}`" in after
    assert f"`{tmp_path / 'kb'}`" not in after


# --- launcher binding: the generated MCP entry must resolve the same store ---


def test_implicit_binding_leaves_the_global_config_unpinned(tmp_path):
    """The global config is the one privileged source worth NOT pinning.

    `.mcp.json` is committed and shared. Pinning writes an absolute
    `/home/<user>/...` into it, so a teammate who clones gets a launcher naming a
    path that does not exist on their machine, and the committer's home layout
    lands in version control. Leaving it unpinned costs nothing: an unpinned
    launcher walks up from the workspace and reaches the global config anyway, on
    whichever machine is running it, which is the store that user should get.
    """
    cfg = SimpleNamespace(loaded_from=[str(tmp_path / "global.toml")],
                          store_path=tmp_path / "kb")
    path, warning = _implicit_binding(cfg, tmp_path / "ws")
    assert path is None and warning is None  # not the global config -> not pinned

    import contextlake.kb.config as kb_config_mod

    real_global = str(tmp_path / "global.toml")
    orig = kb_config_mod.GLOBAL_CONFIG
    try:
        kb_config_mod.GLOBAL_CONFIG = real_global
        path, warning = _implicit_binding(cfg, tmp_path / "ws")
    finally:
        kb_config_mod.GLOBAL_CONFIG = orig
    # Recognised as the global config, and deliberately left unpinned: portable
    # for everyone who clones, and it resolves to the same file regardless.
    assert path is None and warning is None


def test_implicit_binding_refuses_to_promote_an_ancestor_config(tmp_path):
    """Auto-pinning a discovered .contextlake.kb.toml would write --config onto
    a command line, which is exactly what makes a config *trusted*. That would
    launder a file the user never named into a privileged one."""
    project = tmp_path / "project"
    project.mkdir()
    discovered = project / ".contextlake.kb.toml"
    # Sets store_dir: this file really is what chose the store, which is the
    # case where pinning it would change behaviour and so needs the refusal.
    discovered.write_text(f'[kb]\nstore_dir = "{(project / "kb").as_posix()}"\n')
    cfg = SimpleNamespace(loaded_from=[str(discovered)], store_path=project / "kb")

    # Workspace outside the config's directory: unreachable by the ancestor walk.
    path, warning = _implicit_binding(cfg, tmp_path / "elsewhere")
    assert path is None
    assert warning and "different store" in warning

    # Workspace underneath it: the walk finds it, nothing to warn about.
    assert _implicit_binding(cfg, project / "sub") == (None, None)


def test_implicit_binding_stays_quiet_when_the_ancestor_sets_no_store(tmp_path):
    """An ancestor config that never sets store_dir did not choose the store --
    the global one did, and that is found from any directory. Warning that the
    store may differ would send the reader chasing a problem they do not have."""
    project = tmp_path / "project"
    project.mkdir()
    discovered = project / ".contextlake.kb.toml"
    discovered.write_text('[kb]\nlanguages = ["python"]\n')  # no store_dir
    cfg = SimpleNamespace(loaded_from=[str(discovered)], store_path=tmp_path / "kb")

    assert _implicit_binding(cfg, tmp_path / "elsewhere") == (None, None)


def test_implicit_binding_is_quiet_when_no_config_exists(tmp_path):
    cfg = SimpleNamespace(loaded_from=[], store_path=tmp_path / "kb")
    assert _implicit_binding(cfg, tmp_path / "ws") == (None, None)
