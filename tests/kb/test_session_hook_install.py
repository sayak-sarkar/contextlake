"""`kb steer` writes into a settings file it does not own, so the merge gets its own tests.

`.claude/settings.json` is different from the MCP files it sits beside, and the
difference is the whole risk: `hooks.SessionStart` is a **list of matcher groups**, not a
dict keyed by name. Appending would leave a duplicate copy of our hook after every
`steer` run, and replacing the list would silently delete hooks somebody else installed.
Neither failure raises anything, so only a test catches them.
"""

import json
from types import SimpleNamespace

from contextlake.kb.cmds.steer import cmd_steer
from contextlake.kb.steer.generate import SESSION_HOOK_MARK, session_hook_entry
from contextlake.kb.store.sqlite_store import SqliteStore

EXISTING = {
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo theirs"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo theirs-too"}]}],
    },
}


def _run(tmp_path, out):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{(tmp_path / "store").as_posix()}"\n',
                   encoding="utf-8")
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    SqliteStore(tmp_path / "store" / "index.sqlite").close()
    return cmd_steer(SimpleNamespace(config=str(cfg), out=str(out), workspace=None,
                                     force=False))


def _settings(out):
    return json.loads((out / ".claude" / "settings.json").read_text())


def _ours(groups):
    return [h for g in groups for h in (g.get("hooks") or [])
            if SESSION_HOOK_MARK in h.get("command", "")]


def test_a_fresh_workspace_gets_the_hook(tmp_path):
    out = tmp_path / "ws"
    assert _run(tmp_path, out) == 0
    groups = _settings(out)["hooks"]["SessionStart"]
    assert len(_ours(groups)) == 1


def test_other_hooks_and_settings_survive(tmp_path):
    out = tmp_path / "ws"
    (out / ".claude").mkdir(parents=True)
    (out / ".claude" / "settings.json").write_text(json.dumps(EXISTING), encoding="utf-8")
    assert _run(tmp_path, out) == 0
    data = _settings(out)
    assert data["permissions"] == EXISTING["permissions"]
    assert data["hooks"]["PreToolUse"] == EXISTING["hooks"]["PreToolUse"]
    commands = [h["command"] for g in data["hooks"]["SessionStart"]
                for h in (g.get("hooks") or [])]
    assert "echo theirs-too" in commands


def test_running_steer_again_replaces_our_hook_rather_than_adding_another(tmp_path):
    """The one a reviewer would miss. Nothing fails when a duplicate is appended -- the
    hook simply runs twice per session, then three times, then four."""
    out = tmp_path / "ws"
    for _ in range(3):
        assert _run(tmp_path, out) == 0
    groups = _settings(out)["hooks"]["SessionStart"]
    assert len(_ours(groups)) == 1
    # ...and no empty husks left behind by the removals
    assert all(g.get("hooks") for g in groups)


def test_unparseable_settings_are_left_alone(tmp_path):
    """Overwriting somebody's settings because we could not parse them would be the
    worst available outcome, so the hook is skipped and the run still succeeds."""
    out = tmp_path / "ws"
    (out / ".claude").mkdir(parents=True)
    broken = '{"hooks": {"SessionStart": [ '
    (out / ".claude" / "settings.json").write_text(broken, encoding="utf-8")
    assert _run(tmp_path, out) == 0
    assert (out / ".claude" / "settings.json").read_text() == broken


def test_a_config_path_with_a_space_is_quoted_for_the_shell(tmp_path):
    """The command string is handed to a shell. Unquoted, a store under "My Repos"
    installs a hook that reads a different config -- and it would keep working, on the
    wrong store."""
    entry = session_hook_entry("/home/u/My Configs/kb.toml")
    assert "'/home/u/My Configs/kb.toml'" in entry["command"]
    assert entry["type"] == "command"
