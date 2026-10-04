"""`kb steer` must not overwrite an MCP config it cannot parse.

`_merge_mcp_entry` caught the JSON error and carried on with an empty dict, so a
`.vscode/mcp.json` holding a comment or a trailing comma (both common in a hand-edited
file) was replaced by a file with only the contextlake entry. The run then printed
"other servers kept". The sibling `_merge_session_hook` refuses in the same situation, with
a comment calling an overwrite the worst outcome. These pin that the MCP merge does the same.

Every run happens inside a git repository created under `tmp_path`, with the working
directory moved there as well: `steer` writes into the repository it is pointed at.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from contextlake.kb.cmds.steer import cmd_steer
from contextlake.kb.store.sqlite_store import SqliteStore

# Each of these is what a person writes by hand and what `json.loads` rejects.
BROKEN = {
    "comment": '{\n  // my servers\n  "SERVERS": {\n    "other": {"command": "x"}\n  }\n}\n',
    "trailing-comma": '{\n  "SERVERS": {\n    "other": {"command": "x"},\n  }\n}\n',
    "truncated": '{\n  "SERVERS": {\n    "other": {"command": "x"}\n',
}
WRAPPER = {".vscode/mcp.json": "servers", ".mcp.json": "mcpServers"}


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def _steer(tmp_path, out):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{(tmp_path / "store").as_posix()}"\n',
                   encoding="utf-8")
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    SqliteStore(tmp_path / "store" / "index.sqlite").close()
    return cmd_steer(SimpleNamespace(config=str(cfg), out=str(out), workspace=None,
                                     force=False))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = _git_repo(tmp_path / "ws")
    monkeypatch.chdir(out)
    return out


def _put(out, rel, text):
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("flavour", sorted(BROKEN))
@pytest.mark.parametrize("rel", sorted(WRAPPER))
def test_an_unparseable_mcp_file_is_left_unchanged(tmp_path, repo, gls_logs, rel,
                                                           flavour):
    body = BROKEN[flavour].replace("SERVERS", WRAPPER[rel])
    path = _put(repo, rel, body)

    rc = _steer(tmp_path, repo)

    logs = gls_logs.text
    assert logs.strip(), "the capture saw nothing, so the assertions below prove nothing"
    assert path.read_text(encoding="utf-8") == body, "the file was rewritten"
    assert '"other"' in path.read_text(encoding="utf-8")
    assert "contextlake-kb" not in path.read_text(encoding="utf-8")
    # Exit 0, like steer's other refusals: `bootstrap` runs steer last, and a commented
    # mcp.json must not turn every scheduled bootstrap red. The closing line says it.
    assert rc == 0
    # The refusal names the file and says why, and says what to do about it.
    assert rel in logs and "not valid JSON" in logs, logs
    assert "re-run" in logs.lower(), logs
    # The old success line, about this file, must not appear.
    assert f"{rel} (contextlake-kb MCP server, other servers kept)" not in logs, logs
    # The closing line says what was left out, and is not the success line.
    assert "existing files enhanced, not replaced" not in logs, logs
    assert f"except {rel}" in logs, logs


def test_the_other_files_are_still_written_when_one_is_refused(tmp_path, repo):
    """A refusal costs that file only. Skipping the rest would leave a half-steered
    workspace, which is the state `test_merge_mcp_entry_self_heals_a_malformed_wrapper_key`
    exists to prevent."""
    _put(repo, ".vscode/mcp.json", BROKEN["trailing-comma"].replace("SERVERS", "servers"))
    _put(repo, ".mcp.json", '{"mcpServers": {"other": {"command": "x"}}}')

    assert _steer(tmp_path, repo) == 0

    mcp = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))
    assert set(mcp["mcpServers"]) == {"other", "contextlake-kb"}
    assert (repo / "AGENTS.md").exists()
    assert (repo / ".claude" / "settings.json").exists(), "the session hook was skipped"


def test_fixing_the_file_and_re_running_merges_normally(tmp_path, repo, gls_logs):
    rel = ".vscode/mcp.json"
    _put(repo, rel, BROKEN["trailing-comma"].replace("SERVERS", "servers"))
    assert _steer(tmp_path, repo) == 0

    _put(repo, rel, '{"servers": {"other": {"command": "x"}}}\n')
    assert _steer(tmp_path, repo) == 0

    data = json.loads((repo / rel).read_text(encoding="utf-8"))
    assert set(data["servers"]) == {"other", "contextlake-kb"}
    assert f"{rel} (contextlake-kb MCP server, other servers kept)" in gls_logs.text


def test_a_valid_file_still_says_other_servers_were_kept(tmp_path, repo, gls_logs):
    """The message is true on this path and must stay."""
    _put(repo, ".vscode/mcp.json", '{"servers": {"other": {"command": "x"}}}\n')

    assert _steer(tmp_path, repo) == 0

    assert (".vscode/mcp.json (contextlake-kb MCP server, other servers kept)"
            in gls_logs.text)


# --- parseable, but not a shape steer merges into (stability v2 tier A, F7) -------------

WRONG_SHAPE = {
    "top-level-array": '[{"other": {"command": "x"}}]\n',
    "servers-as-a-list": '{\n  "SERVERS": [{"name": "other", "command": "x"}]\n}\n',
}


@pytest.mark.parametrize("rel", sorted(WRAPPER))
@pytest.mark.parametrize("shape", sorted(WRONG_SHAPE))
def test_a_wrong_shape_mcp_config_is_left_unchanged(repo, tmp_path, rel, shape):
    text = WRONG_SHAPE[shape].replace("SERVERS", WRAPPER[rel])
    _put(repo, rel, text)
    _steer(tmp_path, repo)
    assert (repo / rel).read_text(encoding="utf-8") == text


def test_a_settings_file_that_is_an_array_is_left_unchanged(repo, tmp_path):
    text = '[{"hooks": {"SessionStart": []}}]\n'
    _put(repo, ".claude/settings.json", text)
    _steer(tmp_path, repo)
    assert (repo / ".claude/settings.json").read_text(encoding="utf-8") == text


def test_agents_md_keeps_its_line_endings_and_bytes(repo, tmp_path):
    """The block is appended; everything the user wrote comes back byte for byte. Text
    mode turned CRLF into LF and `errors="ignore"` dropped bytes that were not UTF-8."""
    user = b"# Notes\r\nKeep \xff this byte.\r\nAnd this line.\r\n"
    (repo / "AGENTS.md").write_bytes(user)
    _steer(tmp_path, repo)
    after = (repo / "AGENTS.md").read_bytes()
    assert after.startswith(user)
    assert b"\n" not in after.replace(b"\r\n", b""), "the appended block mixed line endings"
