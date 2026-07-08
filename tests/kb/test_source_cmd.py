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
import tomllib

from contextlake.kb import config as kbcfg
from contextlake.kb import source_cmd


@pytest.fixture(autouse=True)
def _isolate_kb_config(tmp_path, monkeypatch):
    """`load_kb_config`'s precedence chain must never touch the real machine's
    global/legacy config files or a stray cwd file -- point every fallback at a
    path that does not exist so tests stay hermetic."""
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "no-global.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "no-local.toml"))
    monkeypatch.setattr(kbcfg, "LEGACY_GLOBAL_CONFIG", str(tmp_path / "no-legacy-global.toml"))
    monkeypatch.setattr(kbcfg, "LEGACY_LOCAL_CONFIG", str(tmp_path / "no-legacy-local.toml"))


def _args(action, config, **kw):
    defaults = {"action": action, "config": config, "name": None, "type": None,
                "mcp": None, "set": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _toml(p):
    return tomllib.loads(Path(p).read_text())


# --- CLI wiring ----------------------------------------------------------------

def test_cli_parses_source_add_type_name():
    from contextlake.cli import build_parser

    args = build_parser().parse_args(["source", "add", "jira", "--type", "atlassian"])
    assert args.command == "source"
    assert args.action == "add"
    assert args.name == "jira"
    assert args.type == "atlassian"


def test_cli_dispatches_source_list_through_kb_commands(tmp_path, gls_logs):
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    cfg = tmp_path / "kb.toml"
    args = build_parser().parse_args(["source", "list", "--config", str(cfg)])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("source", args) == 0
    assert "no sources" in gls_logs.text.lower()


# --- add ---------------------------------------------------------------------

def test_add_writes_source_from_flags(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="atlassian", name="jira", mcp="https://mcp.example"))
    assert rc == 0
    srcs = _toml(cfg)["sources"]
    assert srcs == [{"type": "atlassian", "name": "jira", "mcp": "https://mcp.example"}]
    assert "jira" in gls_logs.text
    assert "contextlake connect" in gls_logs.text


def test_add_applies_set_flags(tmp_path):
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="api", name="tickets",
              set=["url=https://api.example.com/v1/x", "text_field=body"]))
    assert rc == 0
    src = _toml(cfg)["sources"][0]
    assert src["url"] == "https://api.example.com/v1/x"
    assert src["text_field"] == "body"


def test_add_missing_required_fields_non_interactive_errors(tmp_path, gls_logs, monkeypatch):
    monkeypatch.setattr(source_cmd.sys.stdin, "isatty", lambda: False)
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(_args("add", str(cfg)))
    assert rc != 0
    assert not cfg.exists()
    assert "requires" in gls_logs.text.lower()


def test_add_never_echoes_secret_set_value(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source(
        _args("add", str(cfg), type="api", name="tickets",
              set=["token=super-secret-value"]))
    assert rc == 0
    assert "super-secret-value" not in gls_logs.text


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


def test_test_unknown_source_name_fails_cleanly(tmp_path, gls_logs):
    cfg = tmp_path / "kb.toml"
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


def test_verify_source_unknown_type(tmp_path):
    from contextlake.kb.config import SourceCfg

    ok, detail = source_cmd.verify_source(SourceCfg(type="files", name="handbook"))
    assert ok is False
    assert "files" in detail
