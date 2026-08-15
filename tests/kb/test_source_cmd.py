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
    workspace get its first local config from `source add` alone."""
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    project = tmp_path / "myproject"
    project.mkdir()
    monkeypatch.chdir(project)

    rc = source_cmd.cmd_source(
        _args("add", None, type="atlassian", name="jira", mcp="https://mcp.example",
              local=True))
    assert rc == 0
    local_file = project / ".contextlake.kb.toml"
    assert local_file.exists()
    assert _toml(local_file)["sources"] == [
        {"type": "atlassian", "name": "jira", "mcp": "https://mcp.example"}]


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


def test_written_config_is_owner_readable_only(tmp_path):
    """The file a source lands in must not be world-readable: it names every
    source the fleet is wired to, and connector options can carry private URLs."""
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="api", name="tickets",
              set=["url=https://api.example.com/v1/x"]))
    assert rc == 0
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"


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
    monkeypatch.setattr(source_cmd, "verify_source", lambda src: (True, "2 site(s) reachable"))
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
                        lambda src: (False, "connection refused"))
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
