"""`contextlake init` writes `[[sources]]` blocks too, and had the wizard's defect.

The reported shape: `init --local` accepted an MCP server URL, printed a green
tick, and the next load dropped the key at the trust gate -- after which
`connectors/orchestrate.py` falls back to `DEFAULT_MCP_URL` and dials the
vendor's hosted endpoint. An operator who typed an internal URL had every
reason to believe their traffic went there, and nothing said otherwise.

These rows assert on what the REAL loader reads back, not on what init printed.
A tick is what the bug produced, so a test that reads the output alone would
have passed while the traffic went somewhere else.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from contextlake import init_cmd, style
from contextlake.kb import config as kbcfg
from contextlake.kb.config import load_kb_config

_INTERNAL_MCP = "https://mcp.internal.example.net/v1/mcp"


@pytest.fixture(autouse=True)
def _isolated_and_no_shell_writes(tmp_path, monkeypatch):
    """Nothing here may touch the real machine.

    `init`'s interactive path calls `_setup_shell_completion`, which appends to
    the user's ~/.bashrc or ~/.zshrc, and its non-local path resolves to the real
    ~/.contextlake/kb.toml. Both are stubbed or redirected before any run.
    """
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "no-global.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / "unused-global-kb.toml"))
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)
    monkeypatch.setattr(init_cmd, "_setup_shell_completion",
                        lambda **_kw: None)


def _args(**kw):
    defaults = {"skip_interactive": False, "force": True, "no_mirror": True,
                "platform": "gitlab", "group": "", "work_dir": None, "kb": True,
                "embeddings": False, "store_dir": None, "completion": False,
                "config": None, "local": False}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _answer_with(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(it))
    return it


def test_init_local_refuses_an_mcp_url_the_loader_would_drop(
        tmp_path, monkeypatch, gls_logs):
    """`init --local` + an internal MCP URL: refused, and nothing written.

    The assertion that matters is the last one -- the file is loaded back through
    `load_kb_config`, the same call `kb connect` makes, and carries no source at
    all. A tick over a dropped key is the defect; so is a silent write.
    """
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    _answer_with(monkeypatch, [
        "",                 # platform -> gitlab
        "",                 # workspace directory -> cwd
        "y",                # set up the knowledge layer
        "n",                # semantic search off (keeps the run offline)
        str(project / "store"),
        "y",                # connect a data source now
        "atlassian",        # source type
        "jira",             # source name
        _INTERNAL_MCP,      # MCP server URL
        "n",                # connect another? no
    ])

    rc = init_cmd.cmd_init(_args(local=True))
    out = style.strip_ansi(gls_logs.text)
    assert rc == 0

    local_cfg = project / ".contextlake.kb.toml"
    assert local_cfg.exists(), "init wrote its kb config somewhere else"
    # Nothing under the user's home was touched by this run.
    assert str(tmp_path) in str(local_cfg.resolve())

    # FIRST, because this is the claim that matters: read the file back the way
    # `kb connect` reads it. A source here with `mcp` stripped is the reported
    # defect -- the connector then dials the vendor's hosted endpoint. Asserting
    # on what init PRINTED would pass while traffic went elsewhere.
    loaded = load_kb_config(None)
    assert [s.name for s in loaded.sources] == [], (
        "init wrote a source the loader strips the endpoint from: "
        f"{[(s.name, s.mcp) for s in loaded.sources]}")

    assert "Refusing to write 'mcp'" in out
    assert "Added jira" not in out, "a refused source was reported as added"
    assert "Nothing was written for this source." in out
    # The refusal must leave the operator a command that works, which is the
    # difference between a refusal and a dead end.
    assert "--config" in out
    assert str(kbcfg.GLOBAL_CONFIG) in out


def test_init_with_an_explicit_config_keeps_the_mcp_url_through_the_loader(
        tmp_path, monkeypatch, gls_logs):
    """The other direction: a file the user NAMED keeps the key.

    Over-refusing would be the same defect from the other side -- an operator
    told their internal endpoint was saved needs it to still be there, and an
    operator told it was refused needs a route that works. This is that route.
    """
    project = tmp_path / "named"
    project.mkdir()
    monkeypatch.chdir(project)
    mirror_ini = project / "contextlake.ini"
    _answer_with(monkeypatch, [
        "", "", "y", "n", str(project / "store"),
        "y", "atlassian", "jira", _INTERNAL_MCP, "n",
    ])

    rc = init_cmd.cmd_init(_args(config=str(mirror_ini)))
    out = style.strip_ansi(gls_logs.text)
    assert rc == 0
    assert "Refusing to write" not in out, out
    assert "Added jira" in out

    kb_toml = project / "kb.toml"
    assert kb_toml.exists()
    jira = next(s for s in load_kb_config(str(kb_toml)).sources if s.name == "jira")
    assert str(jira.mcp) == _INTERNAL_MCP, (
        "the URL the operator typed did not survive the load")


def test_the_loader_really_strips_mcp_from_a_discovered_file(tmp_path, monkeypatch):
    """The control that makes the refusal above mean something.

    Written by hand and loaded the way `kb connect` loads it. If the trust gate
    stopped stripping `mcp`, the refusal would be a command blocking a flow that
    works, and this row says so.
    """
    project = tmp_path / "control"
    project.mkdir()
    (project / ".contextlake.kb.toml").write_text(
        '[[sources]]\ntype = "atlassian"\nname = "jira"\n'
        f'mcp = "{_INTERNAL_MCP}"\n')
    monkeypatch.chdir(project)

    jira = next(s for s in load_kb_config(None).sources if s.name == "jira")
    assert jira.mcp is None, "the trust gate no longer strips this; re-check the refusal"


def test_only_two_modules_call_config_edit_add_source():
    """Two surfaces write `[[sources]]` through `add_source`, and both run the gate.

    Two were found by reading, one at a time, so this counts them instead. What
    it sees is exactly `config_edit.add_source` callers -- a module that edited
    `kb.toml` with tomlkit directly would pass this and still need the gate.
    """
    root = Path(__file__).resolve().parents[2] / "src"
    callers = sorted(
        f.relative_to(root).as_posix()
        for f in root.rglob("*.py")
        if "add_source(" in f.read_text(encoding="utf-8")
        and "def add_source(" not in f.read_text(encoding="utf-8")
    )
    assert callers == ["contextlake/init_cmd.py", "contextlake/kb/source_cmd.py"], (
        f"the set of [[sources]] writers changed: {callers}. A new one needs the "
        "same refusal_for_unloadable_keys gate the other two run.")
