"""Tests for the `contextlake source` verb (add/list/remove/test/enable/disable).

Output assertions use the repo's ``gls_logs`` fixture (tests/conftest.py), not
``capsys``: ``cmd_source`` reports through the package ``log()`` helper, whose
console handler is lazily created only when the logger has no handlers yet --
and pytest's own log-capture handler is already attached to the named logger
by the time a test body runs (only ``contextlake.cli.main`` reliably rebinds
it, by calling ``setup_logging()`` on every invocation). ``gls_logs`` reads the
logger's records directly and is unaffected by that stream/handler wiring, so
it is the reliable way to assert on ``log()`` output from a command called
directly rather than through ``main()`` (see also tests/test_init.py).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from contextlake import style
from contextlake.kb import config as kbcfg
from contextlake.kb import source_cmd


@pytest.fixture(autouse=True)
def _isolate_kb_config(tmp_path, monkeypatch):
    """`load_kb_config`'s precedence chain must never touch the real machine's
    global config files or a stray cwd file -- point every fallback at a path
    that does not exist so tests stay hermetic."""
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "no-global.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "no-local.toml"))


def _args(action, config, **kw):
    defaults = {"action": action, "config": config, "name": None, "type": None,
                "mcp": None, "set": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _toml(p):
    return tomllib.loads(Path(p).read_text())


# --- CLI wiring ----------------------------------------------------------------

def test_cli_parses_source_add_type_name():
    from contextlake.cli import _resolve_command, build_parser

    parser = build_parser()
    args = parser.parse_args(["kb", "source", "add", "jira", "--type", "atlassian"])
    _resolve_command(args, parser)
    assert args.command == "source"
    assert args.action == "add"
    assert args.name == "jira"
    assert args.type == "atlassian"


def test_cli_dispatches_source_list_through_kb_commands(tmp_path, gls_logs):
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    cfg = tmp_path / "kb.toml"
    cfg.write_text("")  # an explicit --config path must exist (kb/config.py:ConfigError)
    args = build_parser().parse_args(["kb", "source", "list", "--config", str(cfg)])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("source", args) == 0
    assert "no sources" in gls_logs.text.lower()


# --- add ---------------------------------------------------------------------

def test_add_local_flag_writes_to_cwd_not_global(tmp_path, gls_logs, monkeypatch):
    """--local with no --config and no existing ancestor local config writes a
    fresh .contextlake.kb.toml in cwd, not the global config -- lets a
    workspace get its first local config from `source add` alone.

    The source here sets `path`, which `kb/trust.py` leaves ungated on purpose:
    directory-scoped config is the feature. The keys a discovered file may not
    set have their own test below (`test_add_refuses_a_key_the_loader_would_strip`).
    """
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    project = tmp_path / "myproject"
    project.mkdir()
    monkeypatch.chdir(project)

    rc = source_cmd.cmd_source(
        _args("add", None, type="files", name="handbook", local=True,
              set=[f"path={tmp_path}"]))
    assert rc == 0
    local_file = project / ".contextlake.kb.toml"
    assert local_file.exists()
    assert _toml(local_file)["sources"] == [
        {"type": "files", "name": "handbook", "path": str(tmp_path)}]


def test_add_writes_source_from_flags(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="atlassian", name="jira", mcp="https://mcp.example"))
    assert rc == 0
    srcs = _toml(cfg)["sources"]
    assert srcs == [{"type": "atlassian", "name": "jira", "mcp": "https://mcp.example"}]
    assert "jira" in gls_logs.text
    assert "contextlake kb connect" in gls_logs.text


def test_add_applies_set_flags(tmp_path):
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="api", name="tickets",
              set=["url=https://api.example.com/v1/x", "text_field=body"]))
    assert rc == 0
    src = _toml(cfg)["sources"][0]
    assert src["url"] == "https://api.example.com/v1/x"
    assert src["text_field"] == "body"


def test_add_malformed_set_flag_is_a_clean_error_not_a_traceback(tmp_path, gls_logs):
    """--set expects KEY=VALUE; a value with no '=' used to raise ValueError
    uncaught all the way out of cmd_source_add, printing a raw Python
    traceback instead of the CLI's normal clean error style."""
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="api", name="tickets", set=["no_equals_sign"]))
    assert rc == 2
    assert not cfg.exists()
    assert "--set expects KEY=VALUE" in gls_logs.text
    assert "Traceback" not in gls_logs.text


def test_add_missing_required_fields_non_interactive_errors(tmp_path, gls_logs, monkeypatch):
    monkeypatch.setattr(source_cmd.sys.stdin, "isatty", lambda: False)
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(_args("add", str(cfg)))
    assert rc != 0
    assert not cfg.exists()
    assert "requires" in gls_logs.text.lower()


def test_add_refuses_a_literal_secret_and_names_the_env_var_key(tmp_path, gls_logs):
    """A bare `token` is refused, not stored: nothing reads it (only `token_env`
    is consumed), so writing it leaks a secret to disk and buys nothing."""
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="api", name="tickets",
              set=["token=super-secret-value"]))
    assert rc == 2
    assert not cfg.exists()
    assert "super-secret-value" not in gls_logs.text
    assert "token_env" in gls_logs.text


def test_add_never_echoes_a_set_value(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="api", name="tickets",
              set=["token_env=MY_TICKETS_TOKEN", "url=https://api.example.com/v1/x"]))
    assert rc == 0
    assert "https://api.example.com/v1/x" not in gls_logs.text


def test_add_from_stdin_reads_the_value_and_never_echoes_it(tmp_path, gls_logs, monkeypatch):
    """--from-stdin keeps a sensitive value out of shell history entirely -- it
    must never appear as a CLI argument, only read off the pipe."""
    monkeypatch.setattr(source_cmd.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(source_cmd.sys.stdin, "readline",
                        lambda: "https://mcp.example/private\n")
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="atlassian", name="jira", from_stdin="mcp"))
    assert rc == 0
    assert "https://mcp.example/private" not in gls_logs.text
    assert _toml(cfg)["sources"][0]["mcp"] == "https://mcp.example/private"


def test_add_from_stdin_refuses_a_literal_secret_key_without_reading_stdin(
    tmp_path, gls_logs, monkeypatch
):
    """The help's old worked example was `--from-stdin token`, which wrote the
    piped secret straight into the config file. It is refused now, and refused
    before the pipe is read at all."""
    monkeypatch.setattr(source_cmd.sys.stdin, "isatty", lambda: False)

    def _never(*_a, **_k):
        raise AssertionError("stdin was read for a key that must be refused")

    monkeypatch.setattr(source_cmd.sys.stdin, "readline", _never)
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="api", name="tickets", from_stdin="token"))
    assert rc == 2
    assert not cfg.exists()
    assert "token_env" in gls_logs.text


def test_add_from_stdin_on_a_tty_errors_instead_of_hanging(tmp_path, gls_logs, monkeypatch):
    monkeypatch.setattr(source_cmd.sys.stdin, "isatty", lambda: True)
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="atlassian", name="jira", from_stdin="mcp"))
    assert rc != 0
    assert not cfg.exists()
    assert "--from-stdin" in gls_logs.text


# --- keys a directory-walked config may not set ------------------------------
#
# Every assertion here is on what survives a LOAD, not on what was printed. The
# defect was that the print and the load disagreed, so a message-only assertion
# would pass on the broken code.

def _privileged_source_keys():
    from contextlake.kb.trust import PRIVILEGED_SOURCE_KEYS

    return sorted(PRIVILEGED_SOURCE_KEYS)


@pytest.mark.parametrize("key", _privileged_source_keys())
def test_add_refuses_a_key_the_loader_would_strip(key, tmp_path, gls_logs, monkeypatch):
    """The wizard wrote `mcp` to a project-local config, printed a checkmark, and
    the next survey in the same run showed the key gone.

    Parametrized over the set in `kb/trust.py` rather than a copy of it, so a key
    added there is covered here without a second edit.
    """
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    project = tmp_path / "myproject"
    project.mkdir()
    monkeypatch.chdir(project)

    rc = source_cmd.cmd_source(
        _args("add", None, type="atlassian", name="jira", local=True,
              set=[f"{key}=synthetic-value"]))
    assert rc == 2, f"{key!r} was written to a config the loader strips it from"
    assert not (project / ".contextlake.kb.toml").exists(), "nothing may be written"
    text = gls_logs.text
    assert key in text and "Nothing was written" in text
    # The refusal has to leave the user with something to run, so both working
    # routes are named.
    assert "--config" in text
    assert kbcfg.GLOBAL_CONFIG in text, "the global-config route is not named"
    assert ".contextlake.kb.toml" in text, "the name-the-file route is not named"


def test_a_privileged_key_written_to_the_global_config_survives_a_real_load(
        tmp_path, monkeypatch):
    """The honest path, end to end: write, then load back through `load_kb_config`.

    The global config is trusted on every run, so this is the route the refusal
    above points at. Asserting on the loaded config rather than on the printed
    line is the point: the two disagreed, and only the loader's answer matters.
    """
    from contextlake.kb.config import load_kb_config

    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "global-kb.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "absent-local.toml"))

    rc = source_cmd.cmd_source(
        _args("add", None, type="atlassian", name="jira", local=False,
              mcp="https://mcp.example.net/v1/mcp"))
    assert rc == 0
    assert Path(kbcfg.GLOBAL_CONFIG).exists()

    loaded = load_kb_config(None)
    jira = next(s for s in loaded.sources if s.name == "jira")
    assert jira.mcp == "https://mcp.example.net/v1/mcp", (
        "the key did not survive the load it was written for")


def test_a_privileged_key_survives_when_the_file_is_named_with_config(tmp_path):
    """Route 2 of the refusal: `--config PATH` is the user naming the file, which
    is the explicit act the trust gate asks for. The key is kept on the loads
    that name the file, which is why the refusal says so rather than calling this
    route unconditional."""
    from contextlake.kb.config import load_kb_config

    cfg = tmp_path / "project.kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="atlassian", name="jira",
              mcp="https://mcp.example.net/v1/mcp"))
    assert rc == 0
    jira = next(s for s in load_kb_config(str(cfg)).sources if s.name == "jira")
    assert jira.mcp == "https://mcp.example.net/v1/mcp"


def test_the_loader_really_does_strip_the_key_from_a_discovered_config(
        tmp_path, monkeypatch):
    """The control that makes the refusal mean something.

    If `load_kb_config` kept `mcp` from a directory-walked file, the refusal
    above would be a command breaking a working flow. This writes the file by
    hand, loads it the way `kb connect` does, and shows the key gone.
    """
    from contextlake.kb.config import load_kb_config

    project = tmp_path / "myproject"
    project.mkdir()
    local = project / ".contextlake.kb.toml"
    local.write_text('[[sources]]\ntype = "atlassian"\nname = "jira"\n'
                     'mcp = "https://mcp.example.net/v1/mcp"\n')
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    monkeypatch.chdir(project)

    jira = next(s for s in load_kb_config(None).sources if s.name == "jira")
    assert jira.mcp is None, "the trust gate no longer strips this; re-check the refusal"


def test_add_local_still_writes_the_keys_the_gate_leaves_alone(
        tmp_path, monkeypatch):
    """Over-gating would be worse than the bug: directory-scoped config is the
    feature, and `kb/trust.py` leaves `url`, `path`, `env` and connector options
    alone on purpose. A narrowing `scopes` is allowed too -- only a widening one
    is refused."""
    from contextlake.kb.config import load_kb_config
    from contextlake.kb.connectors.atlassian import DEFAULT_SCOPES

    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    project = tmp_path / "myproject"
    project.mkdir()
    monkeypatch.chdir(project)

    narrower = DEFAULT_SCOPES.split()[0]
    rc = source_cmd.cmd_source(
        _args("add", None, type="api", name="tickets", local=True,
              set=["url=https://api.example.net/v1/tickets",
                   f"scopes={narrower}"]))
    assert rc == 0
    tickets = next(s for s in load_kb_config(None).sources if s.name == "tickets")
    assert str(tickets.url) == "https://api.example.net/v1/tickets"
    assert (tickets.model_extra or {}).get("scopes") == narrower


def test_the_refusal_keys_on_the_resolved_write_target_not_on_the_local_flag(
        tmp_path, monkeypatch, gls_logs):
    """No `--local`, no `--config`, and the write still lands in a discovered file.

    `resolve_write_target` uses the nearest ancestor `.contextlake.kb.toml`
    whenever one already exists, so once a workspace has one, every plain
    `kb source add` writes to it -- with no flag anywhere on the command line.
    That file is found by walking up from the working directory, so the trust
    gate strips `mcp` from it on the next load.

    A guard written as `if args.local:` passes every other test in this file and
    is inert here, which is the reason this row exists. Both directions: the same
    add is accepted, and the key survives the real loader, when `--config` names
    the file.
    """
    from contextlake.kb import config_edit
    from contextlake.kb.config import load_kb_config

    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    project = tmp_path / "myproject"
    (project / "sub" / "deeper").mkdir(parents=True)
    ancestor = project / ".contextlake.kb.toml"
    ancestor.write_text("[kb]\n")
    before = ancestor.read_text()
    monkeypatch.chdir(project / "sub" / "deeper")

    # The precondition the whole row rests on: with no flag at all, this add
    # resolves to the discovered ancestor file. If the fixture stops arranging
    # that, this says so rather than failing on an exit code.
    resolved = config_edit.resolve_write_target(None, local=False)
    assert Path(resolved).resolve() == ancestor.resolve(), (
        f"the write target is {resolved}, so this no longer covers the discovered file")

    rc = source_cmd.cmd_source(
        _args("add", None, type="atlassian", name="jira", local=False,
              mcp="https://mcp.example.net/v1/mcp"))
    out = style.strip_ansi(gls_logs.text)
    # Not just `rc == 2`: three other paths in cmd_source_add return 2. The text
    # is what says this was the loader-strip refusal.
    assert rc == 2
    assert "Refusing to write 'mcp'" in out
    assert str(ancestor.resolve()) in out, f"the refusal does not name the file: {out}"
    assert "the next load would drop it" in out
    assert "Nothing was written" in out
    assert ancestor.read_text() == before, "the source was written after the refusal"

    # The other direction: named on the command line, the same write is allowed
    # and the key is still there when the real loader reads it back.
    gls_logs.clear()
    rc = source_cmd.cmd_source(
        _args("add", str(ancestor), type="atlassian", name="jira",
              mcp="https://mcp.example.net/v1/mcp"))
    assert rc == 0, style.strip_ansi(gls_logs.text)
    jira = next(s for s in load_kb_config(str(ancestor)).sources if s.name == "jira")
    assert str(jira.mcp) == "https://mcp.example.net/v1/mcp"


def test_the_wizard_add_step_refuses_rather_than_reporting_a_lost_key(
        tmp_path, monkeypatch, gls_logs):
    """The reported symptom, driven through the wizard shell itself.

    The wizard delegates to `cmd_source_add`, so the guard sits at one seam; this
    row proves the delegation actually carries it and that a refused add is not
    counted as one added.
    """
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    project = tmp_path / "myproject"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(source_cmd, "_interactive", lambda: True)
    # The survey must not dial anything: with the guard removed this fixture
    # writes a real atlassian source, and the next survey loop would spawn
    # `npx mcp-remote` against a live endpoint. Stubbed so the break-test run is
    # as offline as the passing one.
    monkeypatch.setattr(source_cmd, "verify_source",
                        lambda src, timeout=None: (False, "not dialled in tests"))

    # add-another? -> yes; type; name; mcp url; add-another? -> no
    answers = iter(["y", "atlassian", "jira", "https://mcp.example.net/v1/mcp", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    rc = source_cmd.cmd_source_wizard(_args("wizard", None, local=True))
    assert rc == 0
    assert not (project / ".contextlake.kb.toml").exists()
    text = gls_logs.text
    assert "Nothing was written" in text
    assert "Added source" not in text, "a refused add was reported as a success"
    assert "0 source(s) added" in text


def test_written_config_is_owner_readable_only(tmp_path):
    """The file a source lands in must not be world-readable: it names every
    source the fleet is wired to, and connector options can carry private URLs."""
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="api", name="tickets",
              set=["url=https://api.example.com/v1/x"]))
    assert rc == 0
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"


def test_the_gate_reads_the_global_config_its_caller_names(tmp_path, monkeypatch):
    """Three directions of `global_config=`, the argument `contextlake init` passes.

    `init` carries its own copy of the global kb.toml path (`init_cmd._KB_CONFIG`,
    expanded at import time so `init` runs without the [kb] extra). This gate read
    `kb.config.GLOBAL_CONFIG` and nothing else, so a plain `init` -- whose target
    IS the global config -- was judged a discovered project file, its `mcp` URL was
    refused, and the interactive source prompt wrote nothing at all.

    Same source and same keys each time; only who the caller calls the global
    tier changes. The third direction is the one that says the argument is not a
    blanket pass: naming a global config does not privilege a DIFFERENT target.
    """
    caller_global = tmp_path / "callers-global-kb.toml"
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "other-global.toml"))
    src = {"type": "atlassian", "name": "jira", "mcp": "https://mcp.example.net/v1/mcp"}

    assert source_cmd.refusal_for_unloadable_keys(
        str(caller_global), None, src, global_config=str(caller_global)) is None, (
        "the caller's own global config was still treated as a discovered file")

    default = source_cmd.refusal_for_unloadable_keys(str(caller_global), None, src)
    assert default is not None, (
        "with no argument the gate must still read kb.config.GLOBAL_CONFIG")
    assert "Refusing to write 'mcp'" in default

    elsewhere = tmp_path / "project" / ".contextlake.kb.toml"
    other = source_cmd.refusal_for_unloadable_keys(
        str(elsewhere), None, src, global_config=str(caller_global))
    assert other is not None, (
        "naming a global config privileged a target that is not it")
    assert str(elsewhere.resolve()) in other


# --- list ----------------------------------------------------------------------

def test_list_prints_name_type_pipeline_enabled(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
    source_cmd.cmd_source(_args("add", str(cfg), type="atlassian", name="jira",
                                mcp="https://x"))
    source_cmd.cmd_source(_args("add", str(cfg), type="files", name="handbook",
                                set=["path=~/notes"]))
    gls_logs.clear()
    rc = source_cmd.cmd_source(_args("list", str(cfg)))
    assert rc == 0
    out = gls_logs.text
    assert "jira" in out and "atlassian" in out and "connect" in out
    assert "handbook" in out and "files" in out and "ingest" in out


def test_list_empty_reports_none_configured(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("")  # an explicit --config path must exist (kb/config.py:ConfigError)
    rc = source_cmd.cmd_source(_args("list", str(cfg)))
    assert rc == 0
    assert "no sources" in gls_logs.text.lower()


def test_list_shows_effective_merged_config_not_just_the_write_target(
        tmp_path, monkeypatch, gls_logs):
    """A source defined in another file in the load_kb_config precedence chain
    (e.g. a cwd-local .contextlake.kb.toml) must still show up in `list`, even
    though `list`'s write-target file (--config) has no sources of its own --
    `list` reports the same merged view `connect`/`ingest`/`test` see."""
    local_cfg = tmp_path / "local.toml"
    local_cfg.write_text('[[sources]]\ntype = "gitlab"\nname = "gl"\n')
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(local_cfg))

    write_target = tmp_path / "kb.toml"  # deliberately has no sources of its own
    write_target.write_text("")  # must still exist (kb/config.py:ConfigError)
    rc = source_cmd.cmd_source(_args("list", str(write_target)))
    assert rc == 0
    out = gls_logs.text
    assert "gl" in out and "gitlab" in out


# --- remove ----------------------------------------------------------------------

def test_remove_deletes_source(tmp_path):
    cfg = tmp_path / "kb.toml"
    source_cmd.cmd_source(_args("add", str(cfg), type="gitlab", name="gl"))
    rc = source_cmd.cmd_source(_args("remove", str(cfg), name="gl"))
    assert rc == 0
    assert _toml(cfg).get("sources", []) == []


def test_remove_missing_name_is_a_no_op(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(_args("remove", str(cfg), name="ghost"))
    assert rc == 0
    assert "ghost" in gls_logs.text


def test_remove_not_found_names_the_write_target_file(tmp_path, monkeypatch, gls_logs):
    """The source is visible via the merged config (list/test) but lives in a
    different file than the one `remove` mutates -- the not-found message must
    name that write-target file so the divergence is visible, not silent."""
    local_cfg = tmp_path / "local.toml"
    local_cfg.write_text('[[sources]]\ntype = "gitlab"\nname = "gl"\n')
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(local_cfg))

    write_target = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(_args("remove", str(write_target), name="gl"))
    assert rc == 0  # remove stays a no-op on not-found
    out = gls_logs.text
    assert "gl" in out
    assert str(write_target) in out
    assert "source list" in out


# --- enable / disable --------------------------------------------------------

def test_disable_sets_enabled_false(tmp_path):
    cfg = tmp_path / "kb.toml"
    source_cmd.cmd_source(_args("add", str(cfg), type="gitlab", name="gl"))
    rc = source_cmd.cmd_source(_args("disable", str(cfg), name="gl"))
    assert rc == 0
    assert _toml(cfg)["sources"][0]["enabled"] is False


def test_enable_sets_enabled_true(tmp_path):
    cfg = tmp_path / "kb.toml"
    source_cmd.cmd_source(_args("add", str(cfg), type="gitlab", name="gl"))
    source_cmd.cmd_source(_args("disable", str(cfg), name="gl"))
    rc = source_cmd.cmd_source(_args("enable", str(cfg), name="gl"))
    assert rc == 0
    assert _toml(cfg)["sources"][0]["enabled"] is True


def test_disable_missing_name_reports_failure(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(_args("disable", str(cfg), name="ghost"))
    assert rc != 0
    assert "ghost" in gls_logs.text


# --- test (reachability) ------------------------------------------------------

def test_test_reports_reachable(tmp_path, gls_logs, monkeypatch):
    cfg = tmp_path / "kb.toml"
    source_cmd.cmd_source(_args("add", str(cfg), type="atlassian", name="jira",
                                mcp="https://x"))
    # `timeout=None` is part of the signature `cmd_source_test` calls through
    # `survey_source`, not decoration: a stub without it raises TypeError and the
    # test reports a crash where the command reports a result.
    monkeypatch.setattr(source_cmd, "verify_source",
                        lambda src, timeout=None: (True, "2 site(s) reachable"))
    gls_logs.clear()
    rc = source_cmd.cmd_source(_args("test", str(cfg), name="jira"))
    assert rc == 0
    out = gls_logs.text
    assert "jira" in out and "reachable" in out


def test_test_reports_unreachable_and_never_raises(tmp_path, gls_logs, monkeypatch):
    cfg = tmp_path / "kb.toml"
    source_cmd.cmd_source(_args("add", str(cfg), type="atlassian", name="jira",
                                mcp="https://x"))
    monkeypatch.setattr(source_cmd, "verify_source",
                        lambda src, timeout=None: (False, "connection refused"))
    gls_logs.clear()
    rc = source_cmd.cmd_source(_args("test", str(cfg), name="jira"))
    assert rc == 1
    assert "connection refused" in gls_logs.text


def test_test_no_probe_for_type_is_neutral_not_a_failure(tmp_path, gls_logs):
    """A type with no reachability check (e.g. gitlab) is a perfectly valid,
    configured source -- `source test` must report it neutrally and exit 0,
    not paint it red as a failed test."""
    cfg = tmp_path / "kb.toml"
    source_cmd.cmd_source(_args("add", str(cfg), type="gitlab", name="gl"))
    gls_logs.clear()
    rc = source_cmd.cmd_source(_args("test", str(cfg), name="gl"))
    assert rc == 0
    out = gls_logs.text
    assert "gl" in out
    assert "no reachability check" in out
    assert "source is configured" in out


def test_test_does_not_draw_a_pass_mark_for_a_disabled_source(
        tmp_path, gls_logs, monkeypatch):
    """The third surface for one fact, and the only one that got it wrong.

    `kb source list` reports ENABLED=no, and `kb connect` and `kb ingest` both
    skip a disabled source. `kb source test` dialled it anyway and drew the green
    tick, so the one command an operator runs to ask "does this work" answered
    yes about a source nothing will read.

    Both directions from one stub: the enabled sibling in the same file must
    still be dialled, so a change that reported every source as disabled fails
    the second half.
    """
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        '[[sources]]\ntype = "files"\nname = "parked"\n'
        'path = "/nonexistent/synthetic"\nenabled = false\n\n'
        '[[sources]]\ntype = "files"\nname = "live"\n'
        'path = "/nonexistent/synthetic"\n')

    dialled: list[str] = []

    def spy(src, timeout=None):
        dialled.append(src.name)
        return False, "path does not exist: /nonexistent/synthetic"

    monkeypatch.setattr(source_cmd, "verify_source", spy)

    rc = source_cmd.cmd_source(_args("test", str(cfg), name="parked"))
    out = style.strip_ansi(gls_logs.text)
    assert dialled == [], "a disabled source was dialled"
    # Exit 0 matches the untested state and doctor's advisory mark: the operator
    # switched it off, so "this failed" would be a claim about a question nobody
    # asked. The defect was the tick, so that is what this pins.
    assert rc == 0
    assert "✓" not in out, f"a disabled source still draws the pass mark: {out}"
    assert "DISABLED" in out
    assert "not dialled" in out
    assert "Nothing here says it works." in out
    # The route back, named rather than described -- and in the spelling the
    # parser accepts. `--name` is not a flag on `kb source`; the name is
    # positional. See test_every_source_command_this_module_prints_actually_parses.
    assert "kb source enable parked" in out

    gls_logs.clear()
    rc = source_cmd.cmd_source(_args("test", str(cfg), name="live"))
    assert dialled == ["live"], "the enabled source was skipped too"
    assert rc == 1
    assert "path does not exist" in style.strip_ansi(gls_logs.text)


def test_every_source_command_this_module_prints_actually_parses(
        tmp_path, gls_logs, monkeypatch):
    """A printed command an operator cannot run is the defect this file is about.

    `kb source` takes the source name POSITIONALLY (`kb source enable jira`);
    there is no `--name` flag on it. Three messages here told the reader to pass
    one, and `contextlake kb source enable --name parked` exits with
    "'--name' isn't a flag on 'source'". Nothing caught it: the flag guard in
    tests/test_error_messages_name_real_flags.py scans raised exceptions, these
    go through `log()`, and `--name` is a real flag on `kb graph` anyway.

    So the messages are driven and every command they name is fed to the real
    parser.
    """
    import re
    import shlex

    from contextlake.cli import build_parser

    parser = build_parser()

    def parses(command: str) -> bool:
        argv = shlex.split(command)
        assert argv[0] == "contextlake"
        try:
            parser.parse_args(argv[1:])
        except SystemExit:
            return False
        return True

    # The control: the spelling that was being printed must NOT parse, otherwise
    # every row below passes for a reason that has nothing to do with the fix.
    assert not parses("contextlake kb source enable --name parked")

    cfg = tmp_path / "kb.toml"
    cfg.write_text('[[sources]]\ntype = "files"\nname = "parked"\n'
                   'path = "/nonexistent/synthetic"\nenabled = false\n')
    monkeypatch.setattr(source_cmd, "_interactive", lambda: False)

    source_cmd.cmd_source(_args("add", str(cfg)))                  # no type, no name
    source_cmd.cmd_source(_args("enable", str(cfg)))               # no name
    source_cmd.cmd_source(_args("test", str(cfg), name="parked"))  # disabled

    out = style.strip_ansi(gls_logs.text)
    # The messages quote a runnable command in backticks; the placeholders
    # (`add ...`, `add NAME --type TYPE`) are forms, not commands, and are not.
    found = [c for c in re.findall(r"`([^`]*contextlake kb source [^`]*)`", out)
             if "..." not in c and "NAME" not in c and "<" not in c]
    assert len(found) >= 3, f"the messages changed shape; only found {found}"
    broken = [c for c in found if not parses(c)]
    assert not broken, f"these printed commands do not parse: {broken}"

    # A flag can be named outside a backticked command ("this action requires
    # --name"), so the flag tokens are checked on their own against what THIS
    # subcommand accepts. The package-wide guard in
    # tests/test_error_messages_name_real_flags.py cannot see this: `--name` is
    # a real flag on `kb graph`, so it is registered somewhere and passes there.
    def accepted_by_kb_source(flag: str) -> bool:
        for tail in ([flag], [flag, "X"]):
            try:
                parser.parse_args(["kb", "source", "list", *tail])
            except SystemExit:
                continue
            return True
        return False

    assert not accepted_by_kb_source("--name"), (
        "`--name` is a flag on kb source now; this control no longer discriminates")
    assert accepted_by_kb_source("--type"), "the probe rejects a flag that is real"
    named = set(re.findall(r"--[a-z][a-z0-9-]+", out))
    assert named, "no flag was named in any message; this half asserts nothing"
    unreal = sorted(f for f in named if not accepted_by_kb_source(f))
    assert not unreal, (
        f"these messages name a flag `kb source` does not take: {unreal}")


def test_test_unknown_source_name_fails_cleanly(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("")  # an explicit --config path must exist (kb/config.py:ConfigError)
    rc = source_cmd.cmd_source(_args("test", str(cfg), name="ghost"))
    assert rc == 1
    assert "ghost" in gls_logs.text


# --- verify_source dispatch (no network; exercises the real per-type logic) --

def test_verify_source_gitlab_has_no_check(tmp_path):
    from contextlake.kb.config import SourceCfg

    ok, detail = source_cmd.verify_source(SourceCfg(type="gitlab", name="gl"))
    assert ok is False
    assert "gitlab" in detail


def test_verify_source_figma_without_mcp_configured(tmp_path):
    from contextlake.kb.config import SourceCfg

    ok, detail = source_cmd.verify_source(SourceCfg(type="figma", name="design"))
    assert ok is False
    assert "mcp" in detail.lower()


def test_verify_source_slack_without_mcp_configured(tmp_path):
    from contextlake.kb.config import SourceCfg

    ok, detail = source_cmd.verify_source(SourceCfg(type="slack", name="team"))
    assert ok is False
    assert "mcp" in detail.lower()


def test_verify_source_slack_without_channel_configured(monkeypatch):
    import contextlake.kb.connectors.orchestrate as orch
    from contextlake.kb.config import SourceCfg

    class _Stub:
        mcp_url = "https://mcp.example/slack"
        mcp_command = None

    monkeypatch.setattr(orch, "build_slack", lambda src: _Stub())
    ok, detail = source_cmd.verify_source(SourceCfg(type="slack", name="team"))
    assert ok is False
    assert "channel" in detail.lower()


def test_verify_source_unknown_type(tmp_path):
    """A type with genuinely nothing to dial reports False and names itself.

    This used to use `files` as the example. `files` is now really probed (a path typo
    is that type's version of an expired token), so the example moved to `gitlab`, whose
    reachability belongs to the mirror tier. If gitlab ever gains a probe this test
    should move again rather than be deleted -- the branch it covers is the honest
    "nothing was tested" path, which must keep existing.
    """
    from contextlake.kb.config import SourceCfg

    ok, detail = source_cmd.verify_source(SourceCfg(type="gitlab", name="fleet"))
    assert ok is False
    assert "gitlab" in detail


def test_verify_source_probes_a_files_source_for_real(tmp_path):
    """`files` is one of the four types that had no probe. A path that does not exist
    is the `files` equivalent of an expired token, and it used to report as configured."""
    from contextlake.kb.config import SourceCfg

    ok, detail = source_cmd.verify_source(
        SourceCfg(type="files", name="handbook", path=str(tmp_path / "nope")))
    assert ok is False
    assert "does not exist" in detail

    (tmp_path / "a.md").write_text("# hello\n\nbody\n", encoding="utf-8")
    ok, detail = source_cmd.verify_source(
        SourceCfg(type="files", name="handbook", path=str(tmp_path)))
    assert ok is True, detail
    # The probe stops at the FIRST document rather than counting them all,
    # so it reports availability rather than a total.
    assert "at least one document" in detail


def test_verify_source_probes_an_unreachable_web_source(tmp_path):
    """The worst case of the four: `web`/`api`/`graphql` swallow every network error,
    so they were also unprobed -- the diagnostic confirmed a broken source was fine."""
    from contextlake.kb.config import SourceCfg

    ok, detail = source_cmd.verify_source(
        SourceCfg(type="web", name="docs", url="http://127.0.0.1:9/nope"))
    assert ok is False, "an unreachable web source reported as reachable"
    assert "unreadable" in detail


# --- the TRUE branch: a probe that matched nothing is not a pass --------------

class _FakeHeaders:
    def get_content_charset(self):
        return "utf-8"

    def get(self, _name, default=None):
        # `sources/api.py` reads the RFC 8288 `Link` header to follow pages.
        # Absent here, so the api probe reads one page and stops.
        return default


class _FakeResponse:
    """The three things `sources/web.py` asks of a urlopen result."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self.headers = _FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


_PAGE_WITH_TEXT = "<html><head><title>Handbook</title></head><body><p>hello</p></body></html>"
_PAGE_WITH_NO_TEXT = "<html><head><title>Handbook</title></head><body></body></html>"


def test_a_web_probe_that_read_nothing_does_not_draw_the_pass_mark(monkeypatch):
    """S2.1 split the FALSE branch of verify_source and left the TRUE one alone.

    A `web`/`api`/`graphql` probe can return with no document AND no unreachable
    target -- an answering server whose page carried no readable text, or a
    source with nothing configured to dial. That used to return True, so a
    source that read nothing drew the same green tick as one that answered.
    Both directions are asserted here from one stub, so a fix that turned every
    healthy source amber fails the first row.
    """
    from contextlake.kb.config import SourceCfg

    pages = {"http://docs.example.net/ok": _PAGE_WITH_TEXT,
             "http://docs.example.net/empty": _PAGE_WITH_NO_TEXT}
    dialled: list[str] = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        dialled.append(url)
        return _FakeResponse(pages[url])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ok, detail = source_cmd.verify_source(
        SourceCfg(type="web", name="docs", url="http://docs.example.net/ok"))
    assert ok is True, detail
    assert "at least one document" in detail

    ok, detail = source_cmd.verify_source(
        SourceCfg(type="web", name="docs", url="http://docs.example.net/empty"))
    assert ok is False, "a probe that read no document reported as a pass"
    # This target WAS dialled and it DID answer, so the message must say that and
    # must not send the reader to check a url that is correct. The sibling test
    # below covers the other fact ("nothing was dialled"); the two must not share
    # one sentence, which is the state this pair pins.
    assert "1 target(s) answered" in detail
    assert "no readable text came back" in detail
    assert "nothing was dialled" not in detail, (
        f"the two facts share one message again: {detail}")
    # `items` and `text_field` are `api`/`graphql` config keys. A `web` source has
    # neither, so naming them here is advice an operator cannot act on.
    assert "items" not in detail and "text_field" not in detail, (
        f"a web source was told to check a key it does not have: {detail}")
    # The stub proves the second row went down the same code path as the first,
    # rather than failing before the fetch.
    assert dialled == ["http://docs.example.net/ok", "http://docs.example.net/empty"]


def test_a_web_source_with_nothing_to_dial_does_not_draw_the_pass_mark(monkeypatch):
    """The named case: `got == 0 and not misses` with no target at all.

    A `web` source with no `url`/`urls` builds fine and iterates zero times, so
    nothing is fetched and nothing is recorded as a miss.
    """
    from contextlake.kb.config import SourceCfg

    def never(*_a, **_k):
        raise AssertionError("a source with no url must not dial anything")

    monkeypatch.setattr("urllib.request.urlopen", never)
    ok, detail = source_cmd.verify_source(SourceCfg(type="web", name="docs"))
    assert ok is False, "a source that dialled nothing reported as a pass"
    # Nothing answered, because nothing was configured to ask. The message must
    # say so and name the key that is missing, not report a target as answering.
    assert "nothing was dialled" in detail
    assert "`url/urls`" in detail
    assert "answered" not in detail, (
        f"a source with no target was reported as having dialled one: {detail}")


def test_an_api_probe_that_read_nothing_names_the_record_keys_a_web_one_lacks(
        monkeypatch):
    """The other half of the split: advice that fits the type in hand.

    `items` and `text_field` are `api`/`graphql` keys. One message for both types
    named them at a `web` source, which has neither. This row and the two web
    rows above fail in opposite directions, so collapsing the branch back into
    one sentence cannot pass all three.
    """
    from contextlake.kb.config import SourceCfg

    dialled: list[str] = []

    def fake_urlopen(req, timeout=None):
        dialled.append(req.full_url if hasattr(req, "full_url") else str(req))
        return _FakeResponse("[]")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ok, detail = source_cmd.verify_source(
        SourceCfg(type="api", name="tickets", url="http://api.example.net/v1/tickets"))
    assert ok is False, "an api probe that read no record reported as a pass"
    assert dialled == ["http://api.example.net/v1/tickets"], (
        "the probe did not reach the fetch, so this asserts nothing about the message")
    assert "1 target(s) answered" in detail
    assert "no record came back" in detail
    # The keys this type actually has, which is the whole reason the branch split.
    assert "items" in detail and "text_field" in detail
    # And not the web half's explanation, which is about page text.
    assert "JavaScript" not in detail, f"the web advice leaked into an api source: {detail}"


def test_source_test_exit_code_follows_the_probe_not_the_old_true(
        tmp_path, monkeypatch, gls_logs):
    """The command an operator runs, not the function under it.

    `cmd_source_test` returns `0 if ok else 1`, so the TRUE branch decided this
    exit code too: a source that read nothing exited 0, which is what a healthy
    probe looks like to a shell and to CI. Both rows use one stub so a change
    that failed every source fails the first.
    """
    pages = {"http://docs.example.net/ok": _PAGE_WITH_TEXT,
             "http://docs.example.net/empty": _PAGE_WITH_NO_TEXT}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeResponse(pages[req.full_url]))

    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        '[[sources]]\ntype = "web"\nname = "answering-docs"\n'
        'url = "http://docs.example.net/ok"\n'
        '[[sources]]\ntype = "web"\nname = "silent-docs"\n'
        'url = "http://docs.example.net/empty"\n'
    )
    assert source_cmd.cmd_source(_args("test", str(cfg), name="answering-docs")) == 0
    assert source_cmd.cmd_source(_args("test", str(cfg), name="silent-docs")) == 1

    text = _log_text(gls_logs)
    assert _mark(text, "answering-docs (web)") == "✓"
    assert _mark(text, "silent-docs (web)") != "✓"


def test_survey_marks_a_web_probe_that_read_nothing_apart_from_one_that_answered(
        monkeypatch):
    """The renderer half: the four existing states carry this, no fifth is added."""
    from contextlake.kb.config import SourceCfg

    pages = {"http://docs.example.net/ok": _PAGE_WITH_TEXT,
             "http://docs.example.net/empty": _PAGE_WITH_NO_TEXT}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeResponse(pages[req.full_url]))

    state, _ = source_cmd.survey_source(
        SourceCfg(type="web", name="docs", url="http://docs.example.net/ok"))
    assert state == source_cmd.SURVEY_OK

    state, detail = source_cmd.survey_source(
        SourceCfg(type="web", name="docs", url="http://docs.example.net/empty"))
    assert state == source_cmd.SURVEY_FAILED, (
        f"a probe that read nothing still surveys as a pass: {detail}")
    assert state in source_cmd._SURVEY_MARK, "the state must reuse the four marks"
    assert source_cmd._SURVEY_MARK[state] is not source_cmd._SURVEY_MARK[
        source_cmd.SURVEY_OK], "it draws the same mark as a source that answered"


def test_verify_source_atlassian_timeout_bounds_the_connector(monkeypatch):
    """A timeout passed to verify_source (e.g. doctor's 8s bound) must override
    the connector's own default (120s), not just be ignored."""
    import contextlake.kb.connectors.orchestrate as orch
    from contextlake.kb.config import SourceCfg

    class _Stub:
        def __init__(self):
            self.timeout = 120

        def discover_sites(self):
            return {"https://x": "cloud-1"}

    stub = _Stub()
    monkeypatch.setattr(orch, "build_atlassian", lambda src: stub)
    ok, detail = source_cmd.verify_source(SourceCfg(type="atlassian", name="jira"), timeout=8)
    assert ok is True
    assert stub.timeout == 8


_MOCK_MCP_SERVER = """
from mcp.server.mcpserver import MCPServer
m = MCPServer("mock")


@m.tool()
def echo(text: str) -> str:
    return text


m.run()
"""


def _spawned_rejection(tmp_path):
    """A real rejection from a real MCP server, spawned as a local subprocess.

    No network: `sys.executable` runs the script below over stdio, and asking it
    for a tool it does not have makes it answer `is_error`. The wrapper shape
    this returns is what `stdio_client` + anyio produce, which is the whole point
    -- a hand-built `McpToolError` is a shape no transport raises, and a test
    written on one proves nothing about the branch it claims to cover.
    """
    import sys

    from contextlake.kb.mcp_client import call_tool

    script = tmp_path / "mock_server.py"
    script.write_text(_MOCK_MCP_SERVER)
    try:
        call_tool(sys.executable, [str(script)], "getAccessibleAtlassianResources", {})
    except BaseException as e:  # noqa: BLE001 - the wrapper type is the point
        return e
    raise AssertionError("the mock server accepted a tool it does not have")


def test_verify_atlassian_keeps_the_servers_own_words_through_the_task_group(
        tmp_path, monkeypatch):
    """The failing tool's name and the server's text must survive the wrapper.

    `discover_sites` raises inside anyio task groups, so the rejection arrives
    as a doubly nested ExceptionGroup whose str() is "unhandled errors in a
    TaskGroup (1 sub-exception)". `except McpToolError` never matched it, so the
    branch was dead and the operator was shown the wrapper text instead of the
    reason.
    """
    import contextlake.kb.connectors.orchestrate as orch
    from contextlake.kb.config import SourceCfg
    from contextlake.kb.mcp_client import McpToolError

    wrapped = _spawned_rejection(tmp_path)
    # The precondition this test rests on, asserted rather than assumed.
    assert not isinstance(wrapped, McpToolError), (
        "the transport stopped wrapping; this test no longer covers the wrapper")
    assert "TaskGroup" in str(wrapped)

    class _Stub:
        def __init__(self):
            self.timeout = 120

        def discover_sites(self):
            raise wrapped

    monkeypatch.setattr(orch, "build_atlassian", lambda src: _Stub())
    ok, detail = source_cmd.verify_source(SourceCfg(type="atlassian", name="jira"))
    assert ok is False
    assert "TaskGroup" not in detail, f"the wrapper text is still what a user sees: {detail}"
    # Adjacency, not presence. This SDK puts the tool name in its OWN error text
    # ("Unknown tool: getAccessibleAtlassianResources"), so `tool name in detail`
    # is satisfied by the server's words and stays true with `{rejected.tool!r}`
    # deleted from the message. Measured against a spawned server on this tree.
    # The quoted form next to the code's own phrase can only come from the code.
    assert "rejected the call to 'getAccessibleAtlassianResources'" in detail, (
        f"the failing tool is not named by this command: {detail}")
    assert "Unknown tool" in detail, "the server's own words were lost"
    # The classification, not only the text: "the server answered and said no" is
    # a different fact from "the call did not get through", and this is the
    # sentence that says which. verify_source's catch-all would also surface the
    # tool name, so without this row the atlassian branch could be dead again and
    # the three assertions above would still pass.
    assert "the server rejected the call" in detail


def test_verify_atlassian_still_reports_a_healthy_site_list(monkeypatch):
    """The positive half: a connector that answers still reads as reachable.

    Without this row a `_verify_atlassian` that reported every call as rejected
    would satisfy the test above.
    """
    import contextlake.kb.connectors.orchestrate as orch
    from contextlake.kb.config import SourceCfg

    class _Stub:
        timeout = 120

        def discover_sites(self):
            return {"https://team.example.net": "cloud-1"}

    monkeypatch.setattr(orch, "build_atlassian", lambda src: _Stub())
    ok, detail = source_cmd.verify_source(SourceCfg(type="atlassian", name="jira"))
    assert ok is True
    assert "1 site(s) reachable" in detail


def test_verify_atlassian_keeps_the_not_a_site_list_message(monkeypatch):
    """`parse_sites` raises after `call_tool` returns, outside the task group, so
    it arrives unwrapped -- the third of the three deliberate messages."""
    import contextlake.kb.connectors.orchestrate as orch
    from contextlake.kb.config import SourceCfg

    class _Stub:
        timeout = 120

        def discover_sites(self):
            raise ValueError("expected a list of Atlassian sites, got str")

    monkeypatch.setattr(orch, "build_atlassian", lambda src: _Stub())
    ok, detail = source_cmd.verify_source(SourceCfg(type="atlassian", name="jira"))
    assert ok is False
    assert "not a site list" in detail
    assert "expected a list of Atlassian sites" in detail


def test_verify_source_catch_all_reports_the_reason_not_the_task_group(
        tmp_path, monkeypatch):
    """The last line before the user, on every connector rather than one.

    `verify_source` reports whatever escapes a probe, and every connector here
    reaches its server through the MCP client's anyio task groups. `str()` on the
    group they raise is "unhandled errors in a TaskGroup (1 sub-exception)", so
    the catch-all replaced the reason with no reason. The exception raised below
    is a real one, captured from a spawned server, not a hand-built stand-in.
    """
    from contextlake.kb.config import SourceCfg

    wrapped = _spawned_rejection(tmp_path)

    def boom(src, timeout=None):
        raise wrapped

    monkeypatch.setattr(source_cmd, "_verify_mcp", boom)
    ok, detail = source_cmd.verify_source(
        SourceCfg(type="mcp", name="tools", command="synthetic"))
    assert ok is False
    assert "TaskGroup" not in detail, f"the wrapper text reached the user: {detail}"
    assert "getAccessibleAtlassianResources" in detail
    assert "Unknown tool" in detail


def test_verify_source_without_timeout_leaves_connector_default(monkeypatch):
    import contextlake.kb.connectors.orchestrate as orch
    from contextlake.kb.config import SourceCfg

    class _Stub:
        def __init__(self):
            self.timeout = 120

        def discover_sites(self):
            return {"https://x": "cloud-1"}

    stub = _Stub()
    monkeypatch.setattr(orch, "build_atlassian", lambda src: stub)
    source_cmd.verify_source(SourceCfg(type="atlassian", name="jira"))
    assert stub.timeout == 120


def _type_help() -> str:
    """The `--type` help string, as `kb source --help` renders it."""
    from contextlake.cli import build_parser

    def _subparsers(parser):
        for action in parser._actions:
            # a dict-valued `choices` is the subparser map; a plain list of
            # choices is an ordinary constrained argument
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                yield from choices.values()

    for kb in _subparsers(build_parser()):
        for verb in _subparsers(kb):
            if verb.prog.endswith("kb source"):
                return next(a.help for a in verb._actions
                            if "--type" in (a.option_strings or []))
    raise AssertionError("could not find `kb source --type` in the parser")


def test_the_type_help_names_every_source_type_this_build_ships():
    """The help named 7 types while the build ships 9: `slack` and `graphql` are
    real, shipped types (kb/connectors/slack.py, kb/sources/graphql.py) that a
    user reading --help had no way to discover. --type is deliberately an open
    set, so this is not about rejecting anything -- it is about the help naming
    what is actually there."""
    shipped = source_cmd.known_source_types()
    assert {"slack", "graphql"} <= set(shipped)   # the two the help left out
    help_text = _type_help()
    missing = [t for t in shipped if t not in help_text]
    assert not missing, f"--type help does not name shipped type(s): {missing}"


def test_the_interactive_prompt_offers_every_shipped_type():
    """The prompt and the help drifted apart because each hard-coded its own
    list. Both read the one registry now."""
    prompt = source_cmd._type_prompt()
    missing = [t for t in source_cmd.known_source_types() if t not in prompt]
    assert not missing, f"the add prompt does not offer shipped type(s): {missing}"


# --- _PROBED_TYPES is derived from verify_source, not maintained beside it ----

def test_probed_types_matches_verify_source_dispatch():
    """`_PROBED_TYPES` is a hand-written mirror of `verify_source`'s dispatch chain.

    Add a probe for a new type and forget the set, and a working probe renders
    as "nothing was tested" forever -- the same absent-check-reads-as-an-answer
    defect, pointing the other way. Read by AST rather than by calling, so this
    stays offline and makes no network call of any kind.
    """
    import ast
    import inspect

    try:
        src_text = inspect.getsource(source_cmd.verify_source)
    except OSError:  # a frozen/zipapp build has no .py on disk to parse
        pytest.skip("verify_source's source is not on disk in this build")

    collected: set[str] = set()
    for node in ast.walk(ast.parse(src_text)):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Attribute) and node.left.attr == "type"):
            continue
        for op, cmp_node in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, ast.Eq) and isinstance(cmp_node, ast.Constant):
                collected.add(cmp_node.value)
            elif isinstance(op, ast.In) and isinstance(cmp_node, (ast.Set, ast.Tuple,
                                                                  ast.List)):
                collected.update(e.value for e in cmp_node.elts
                                 if isinstance(e, ast.Constant))

    # Non-vacuity first. An AST walk that matched nothing returns an empty set,
    # and an empty set compared against an emptied `_PROBED_TYPES` would pass.
    assert collected, "the AST walk found no `src.type == ...` branches at all"
    assert "files" in collected
    assert source_cmd.is_probed_type("files") is True

    assert collected == source_cmd._PROBED_TYPES, (
        "verify_source's dispatch chain and _PROBED_TYPES disagree: a probe was "
        "added to one and not the other, so a type is reported as untested when it "
        "is probed, or as probed when nothing dials it")


def test_an_unlisted_type_is_reported_as_unprobed():
    """The safe default, from both sides."""
    assert source_cmd.is_probed_type("zendesk") is False
    assert source_cmd.is_probed_type("a-type-no-build-ships") is False
    assert source_cmd.is_probed_type("atlassian") is True


# --- survey_source: the one derivation doctor and the wizard both read --------

def test_survey_source_tells_the_four_states_apart(tmp_path, monkeypatch):
    from contextlake.kb.config import SourceCfg

    good = tmp_path / "good"
    good.mkdir()
    (good / "a.md").write_text("# a\n")

    state, _ = source_cmd.survey_source(SourceCfg(type="files", name="good",
                                                  path=str(good)))
    assert state == source_cmd.SURVEY_OK

    state, detail = source_cmd.survey_source(
        SourceCfg(type="files", name="broken", path=str(tmp_path / "absent")))
    assert state == source_cmd.SURVEY_FAILED
    assert "does not exist" in detail

    state, _ = source_cmd.survey_source(SourceCfg(type="gitlab", name="gl"))
    assert state == source_cmd.SURVEY_UNTESTED

    # A disabled source is not dialled at all, so a broken path on one is not
    # reported: `kb connect` and `kb ingest` already skip them.
    called: list[str] = []
    monkeypatch.setattr(source_cmd, "verify_source",
                        lambda src, timeout=None: called.append(src.name) or (False, "x"))
    state, detail = source_cmd.survey_source(
        SourceCfg(type="files", name="parked", enabled=False,
                  path=str(tmp_path / "absent")))
    assert state == source_cmd.SURVEY_DISABLED
    assert detail == "disabled"
    assert called == [], "a disabled source must not be dialled"


# --- the wizard shell ---------------------------------------------------------

def _log_text(gls_logs) -> str:
    """The logger's own messages, without caplog's "INFO contextlake:..." prefix.

    `gls_logs.text` carries that prefix, so reading the first character of a line
    from it reads caplog's formatting rather than the mark under test.
    """
    from contextlake import style

    return "\n".join(style.strip_ansi(r.getMessage()) for r in gls_logs.records)


def _mark(text: str, label: str) -> str:
    for line in text.splitlines():
        if label in line:
            return line.strip()[0]
    raise AssertionError(f"no line mentions {label!r}; the fixture never produced it")


def _wizard_config(tmp_path) -> Path:
    """Three synthetic sources: probed-and-failed, no-probe, and reachable."""
    good = tmp_path / "good"
    good.mkdir()
    (good / "a.md").write_text("# a\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        '[[sources]]\ntype = "files"\nname = "broken-notes"\n'
        f'path = "{tmp_path / "absent"}"\n'
        '[[sources]]\ntype = "gitlab"\nname = "fleet-mirror"\n'
        f'[[sources]]\ntype = "files"\nname = "good-notes"\npath = "{good}"\n'
    )
    return cfg


def test_wizard_refuses_a_non_tty_and_names_the_flag_form(tmp_path, monkeypatch,
                                                          gls_logs):
    """A prompt written to a pipe hangs, and a hang in CI reads as a network problem."""
    cfg = _wizard_config(tmp_path)
    monkeypatch.setattr(source_cmd, "_interactive", lambda: False)

    def never(*a, **k):
        raise AssertionError("the wizard asked a question with no terminal to ask it on")

    monkeypatch.setattr("builtins.input", never)
    rc = source_cmd.cmd_source_wizard(_args("wizard", str(cfg)))
    assert rc == 2
    assert "kb source add" in gls_logs.text and "--type" in gls_logs.text


def test_wizard_surveys_on_a_tty_and_exits_on_an_empty_answer(tmp_path, monkeypatch,
                                                              gls_logs):
    """The positive half of the refusal above: with a terminal, it runs.

    Without this row a wizard that refused every invocation would pass the
    non-TTY test. The empty answer is the documented way out (`_ask_yn`'s blank
    input keeps the default, which is no).
    """
    cfg = _wizard_config(tmp_path)
    monkeypatch.setattr(source_cmd, "_interactive", lambda: True)
    asked: list[str] = []

    def scripted(prompt):
        asked.append(prompt)
        return ""      # the empty answer

    monkeypatch.setattr("builtins.input", scripted)
    rc = source_cmd.cmd_source_wizard(_args("wizard", str(cfg)))
    assert rc == 0
    assert asked, "the wizard never asked whether to add another source"
    assert "Add another source?" in asked[0]
    assert "good-notes" in gls_logs.text


def test_wizard_survey_marks_a_failed_probe_apart_from_no_probe_and_keeps_going(
        tmp_path, monkeypatch, gls_logs):
    cfg = _wizard_config(tmp_path)
    monkeypatch.setattr(source_cmd, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    rc = source_cmd.cmd_source_wizard(_args("wizard", str(cfg)))
    assert rc == 0

    text = _log_text(gls_logs)
    # The positive row: a reachable source still reads as reachable. A survey
    # that marked everything failed would satisfy the two rows below.
    assert _mark(text, "good-notes (files)") == "✓"
    assert _mark(text, "broken-notes (files)") == "⚠"
    assert _mark(text, "fleet-mirror (gitlab)") == "⊘"
    assert _mark(text, "broken-notes (files)") != _mark(text, "fleet-mirror (gitlab)"), (
        "a broken source and a source nothing can dial collapsed into one mark")
    # The dead source is listed first and the run still reached the third one:
    # one dead provider must not stop the survey.
    assert text.index("broken-notes") < text.index("good-notes")


def test_wizard_does_not_probe_a_disabled_source(tmp_path, monkeypatch, gls_logs):
    live = tmp_path / "live"
    live.mkdir()
    (live / "a.md").write_text("# a\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        f'[[sources]]\ntype = "files"\nname = "live-notes"\npath = "{live}"\n'
        '[[sources]]\ntype = "files"\nname = "parked-notes"\nenabled = false\n'
        f'path = "{tmp_path / "absent"}"\n'
    )
    probed: list[str] = []
    real = source_cmd.verify_source

    def spy(src, timeout=None):
        probed.append(src.name)
        return real(src, timeout=timeout)

    monkeypatch.setattr(source_cmd, "verify_source", spy)
    monkeypatch.setattr(source_cmd, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert source_cmd.cmd_source_wizard(_args("wizard", str(cfg))) == 0

    # One assertion, both directions: the disabled source was not dialled and the
    # enabled one was.
    assert probed == ["live-notes"]
    text = _log_text(gls_logs)
    assert _mark(text, "parked-notes (files)") == "⊘"
    assert "disabled" in text.split("parked-notes (files)")[1].splitlines()[0]
    assert _mark(text, "live-notes (files)") == "✓"


def test_wizard_loops_to_offer_another_source(tmp_path, monkeypatch, gls_logs):
    """Yes adds one and re-surveys; the next answer ends the run."""
    cfg = _wizard_config(tmp_path)
    monkeypatch.setattr(source_cmd, "_interactive", lambda: True)
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    adds: list[object] = []

    def fake_add(add_args):
        adds.append(add_args)
        return 0

    monkeypatch.setattr(source_cmd, "cmd_source_add", fake_add)

    # Counted through the survey's own consumer rather than by counting log lines:
    # the package logger can end a test with the capture handler attached twice,
    # which doubles every line in `gls_logs.text` and makes a line count a
    # function of what ran before this test.
    surveyed: list[str] = []
    real_survey = source_cmd.survey_source

    def survey_spy(src, timeout=8):
        surveyed.append(src.name)
        return real_survey(src, timeout=timeout)

    monkeypatch.setattr(source_cmd, "survey_source", survey_spy)

    rc = source_cmd.cmd_source_wizard(_args("wizard", str(cfg)))
    assert rc == 0
    assert len(adds) == 1, "answering yes must run one add, and answering no must stop"
    # The add step inherits the wizard's write target and asks for nothing it was
    # already given, so `_prompt_missing` fires for type and name.
    assert adds[0].config == str(cfg)
    assert adds[0].type is None and adds[0].name is None
    # Two surveys over the three sources: the one before the add, and the one that
    # shows the result of it.
    assert surveyed == ["broken-notes", "fleet-mirror", "good-notes"] * 2
    assert "1 source(s) added" in gls_logs.text


# --- dispatch parity ----------------------------------------------------------

def test_every_parsed_source_action_has_a_handler():
    """Three places list the source actions and nothing pinned them together.

    The parser's `choices`, `_ACTIONS`, and the unknown-action message. A name in
    the parser with no handler exits 2 with "unknown source action", which reads
    as a typo by the user rather than a gap in the build.
    """
    import argparse

    from contextlake.cli import build_parser

    def _source_action_choices(parser):
        """Walk every subparser for the `kb source` positional's choices."""
        for act in parser._actions:
            if isinstance(act, argparse._SubParsersAction):
                for name, sub in act.choices.items():
                    if name == "source":
                        for a in sub._actions:
                            if a.dest == "action" and a.choices:
                                return set(a.choices)
                    found = _source_action_choices(sub)
                    if found:
                        return found
        return None

    choices = _source_action_choices(build_parser())
    assert choices, "could not find the `kb source` action choices in the parser"
    assert {"add", "list", "remove", "test", "enable", "disable"} <= choices, (
        "the six pre-existing actions must still be parsed")
    assert choices == set(source_cmd._ACTIONS), (
        "a `kb source` action the parser accepts has no handler, or the other way round")
