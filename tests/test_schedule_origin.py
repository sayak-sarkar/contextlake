"""A scheduled job keeps the config it was installed with.

`schedule install` used to write a unit that ran `schedule run --job NAME` from the user's home
directory with no config. The job record sits under the install config's cache directory, so
every fire loaded the global config, found no record and exited 2 with its output discarded.

These tests cover what is captured at install, the command line and directory the unit gets,
and the rule that a config found by walking up from the install directory is reached by
running there and is never passed as `--config` (that would keep keys the walk-up tier drops).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import pytest

from contextlake import config as cfgmod
from contextlake.schedule import actions, adapters, adhoc, gates, runner
from contextlake.schedule import jobs as jobstore


@pytest.fixture(autouse=True)
def _quiet_home(tmp_path, monkeypatch):
    """No global config, no opt-out, and a home of its own. A developer's own
    `~/.contextlake.ini` or an ancestor config must not decide these tests."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv(cfgmod.NO_LOCAL_CONFIG_ENV, raising=False)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", str(home / ".contextlake.ini"))
    monkeypatch.chdir(home)
    return home


def _ws(tmp_path, name="ws", kb=False):
    ws = tmp_path / name
    ws.mkdir()
    (ws / ".contextlake.ini").write_text("[contextlake]\ngitlab_group = g\n", encoding="utf-8")
    if kb:
        (ws / ".contextlake.kb.toml").write_text("[kb]\n", encoding="utf-8")
    return ws


def _args(**kw):
    base = dict(job=None, interval=None, platform="cron", config=None, kb_config=None,
                history=False, yes=True, rest=[])
    base.update(kw)
    return argparse.Namespace(**base)


class _Recorder:
    """An adapter that records what it is asked to install and touches nothing."""

    id = "cron"
    catches_up_after_sleep = False
    metadata_keys = frozenset({"spec"})

    def __init__(self):
        self.installed = []

    def render(self, job, interval_s, exec_argv, **_):
        return {"crontab": "x", "spec": "s"}

    def install(self, job, interval_s, exec_argv, **_):
        self.installed.append((job, list(exec_argv)))
        return ["fake"]

    def state(self, job):
        return {"notes": []}


@pytest.fixture
def recorder(monkeypatch):
    fake = _Recorder()
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: fake)
    monkeypatch.setattr(actions, "log", lambda *a, **k: None)
    monkeypatch.setattr(adapters, "log", lambda *a, **k: None)
    monkeypatch.setattr(adhoc, "log", lambda *a, **k: None)
    return fake


def _config(tmp_path):
    return {"cache_dir": str(tmp_path / "cache"), "cache_file": "p.txt"}


def _stored(tmp_path, name="default"):
    return jobstore.read_jobs(jobstore.jobs_path(_config(tmp_path)))[name]


# ---- what install captures -------------------------------------------------

def test_a_config_named_with_dash_dash_config_is_explicit_and_needs_no_directory(tmp_path):
    ini = tmp_path / "custom.ini"
    ini.write_text("[contextlake]\ngitlab_group = g\n", encoding="utf-8")
    origin = adapters.capture_origin(_args(config=str(ini)))
    assert origin == {"config": str(ini), "config_source": jobstore.EXPLICIT,
                      "kb_config": None, "kb_config_source": None, "cwd": None}


def test_a_relative_config_is_recorded_as_an_absolute_path(tmp_path, monkeypatch):
    (tmp_path / "custom.ini").write_text("[contextlake]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert adapters.capture_origin(_args(config="./custom.ini"))["config"] == str(
        tmp_path / "custom.ini")


def test_a_config_found_by_walking_up_is_discovered_and_records_the_directory(
        tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    (ws / "deep" / "er").mkdir(parents=True)
    monkeypatch.chdir(ws / "deep" / "er")
    origin = adapters.capture_origin(_args())
    assert origin["config"] == str(ws / ".contextlake.ini")
    assert origin["config_source"] == jobstore.DISCOVERED
    assert origin["cwd"] == str(ws / "deep" / "er")


def test_an_explicit_config_still_records_the_directory_of_a_discovered_one(
        tmp_path, monkeypatch):
    """The run merges the explicit file over the discovered one. Without the directory, the
    unit would read only the explicit file and look in a different cache directory."""
    ws = _ws(tmp_path)
    other = tmp_path / "other.ini"
    other.write_text("[contextlake]\n", encoding="utf-8")
    monkeypatch.chdir(ws)
    origin = adapters.capture_origin(_args(config=str(other)))
    assert origin["config"] == str(other)
    assert origin["config_source"] == jobstore.EXPLICIT
    assert origin["cwd"] == str(ws)


def test_the_global_config_found_from_here_needs_neither_a_path_nor_a_directory(
        tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", str(ws / ".contextlake.ini"))
    monkeypatch.chdir(ws)
    assert adapters.capture_origin(_args()) == {key: None for key in jobstore.ORIGIN_KEYS}


def test_naming_the_discovered_file_with_dash_dash_config_makes_it_explicit(
        tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.chdir(ws)
    origin = adapters.capture_origin(_args(config="./.contextlake.ini"))
    assert origin["config_source"] == jobstore.EXPLICIT
    assert origin["cwd"] is None


def test_the_opt_out_for_local_configs_records_nothing_about_one(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv(cfgmod.NO_LOCAL_CONFIG_ENV, "1")
    assert adapters.capture_origin(_args()) == {key: None for key in jobstore.ORIGIN_KEYS}


def test_a_directory_with_no_config_records_nothing(tmp_path, monkeypatch):
    (tmp_path / "plain").mkdir()
    monkeypatch.chdir(tmp_path / "plain")
    assert adapters.capture_origin(_args()) == {key: None for key in jobstore.ORIGIN_KEYS}


def test_a_knowledge_config_is_captured_the_same_two_ways(tmp_path, monkeypatch):
    kb = tmp_path / "kb.toml"
    kb.write_text("[kb]\n", encoding="utf-8")
    explicit = adapters.capture_origin(_args(kb_config=str(kb)))
    assert (explicit["kb_config"], explicit["kb_config_source"]) == (
        str(kb), jobstore.EXPLICIT)
    assert explicit["cwd"] is None

    ws = _ws(tmp_path, "ws2", kb=True)
    monkeypatch.chdir(ws)
    found = adapters.capture_origin(_args())
    assert (found["kb_config"], found["kb_config_source"]) == (
        str(ws / ".contextlake.kb.toml"), jobstore.DISCOVERED)
    assert found["cwd"] == str(ws)


def test_a_knowledge_config_that_is_not_there_is_refused(tmp_path):
    with pytest.raises(ValueError, match="--kb-config path not found"):
        adapters.capture_origin(_args(kb_config=str(tmp_path / "nope.toml")))


def test_the_local_knowledge_config_name_is_the_one_the_loader_walks_up_for():
    kbconfig = pytest.importorskip("contextlake.kb.config")
    assert adapters.KB_LOCAL_CONFIG == kbconfig.LOCAL_CONFIG


def test_a_directory_with_a_newline_in_its_name_cannot_be_recorded(tmp_path, monkeypatch):
    """A newline is legal in a Linux directory name, and in a unit file or a crontab it starts
    a new line. The install is refused before anything is written."""
    ws = _ws(tmp_path, "ws\n[Service]")
    monkeypatch.chdir(ws)
    with pytest.raises(ValueError, match="control character"):
        adapters.capture_origin(_args())


# ---- the command line the unit runs ---------------------------------------

def test_the_default_command_line_is_the_one_earlier_versions_wrote():
    assert adapters.exec_argv_for("nightly") == [
        sys.executable, "-m", "contextlake", "schedule", "run", "--job", "nightly"]


def test_both_config_flags_go_before_the_subcommand():
    """`schedule` takes `--kb-config` only before it, so the pair has to sit there."""
    argv = adapters.exec_argv_for("n", config="/a/x.ini", kb_config="/a/kb.toml")
    assert argv[3:] == ["--config", "/a/x.ini", "--kb-config", "/a/kb.toml",
                        "schedule", "run", "--job", "n"]


def test_the_command_line_parses_to_the_config_it_names():
    """Parsed by the real parser: the flags are accepted where the argv puts them, and mean
    the sync INI and the knowledge config."""
    from contextlake.cli import build_parser

    argv = adapters.exec_argv_for("n", config="/a/x.ini", kb_config="/a/kb.toml")
    parsed = build_parser().parse_args(argv[3:])
    assert (parsed.config, parsed.kb_config, parsed.job) == ("/a/x.ini", "/a/kb.toml", "n")


@pytest.mark.parametrize("config_source, kb_source, expect_config, expect_kb", [
    (jobstore.EXPLICIT, jobstore.EXPLICIT, True, True),
    (jobstore.DISCOVERED, jobstore.DISCOVERED, False, False),
    (jobstore.EXPLICIT, jobstore.DISCOVERED, True, False),
    (None, None, False, False),
])
def test_only_a_path_recorded_as_explicit_becomes_a_flag(
        config_source, kb_source, expect_config, expect_kb):
    job = jobstore.new_job("n", ["bootstrap"], "auto", "cron", origin={
        "config": "/ws/.contextlake.ini", "config_source": config_source,
        "kb_config": "/ws/.contextlake.kb.toml", "kb_config_source": kb_source,
        "cwd": "/ws"})
    argv = adapters.job_exec_argv(job)
    assert ("--config" in argv) is expect_config
    assert ("--kb-config" in argv) is expect_kb


# ---- install, reset and interval write what was captured -------------------

def test_install_in_a_directory_with_a_config_keeps_the_directory_and_no_flag(
        tmp_path, monkeypatch, recorder):
    """The case from the report: `cd ws && contextlake schedule install`. The unit runs in
    ws, and the config is NOT passed as `--config`."""
    ws = _ws(tmp_path)
    monkeypatch.chdir(ws)
    assert actions.cmd_install(_args(), _config(tmp_path)) == 0
    job, argv = recorder.installed[0]
    assert job.cwd == str(ws)
    assert "--config" not in argv
    assert str(ws) not in " ".join(argv)
    stored = _stored(tmp_path)
    assert (stored.config_source, stored.cwd) == (jobstore.DISCOVERED, str(ws))


def test_install_with_dash_dash_config_puts_it_in_the_units_command(tmp_path, recorder):
    """The case from the report: `contextlake --config X schedule install`."""
    ini = tmp_path / "custom.ini"
    ini.write_text("[contextlake]\ngitlab_group = g\n", encoding="utf-8")
    assert actions.cmd_install(_args(config=str(ini)), _config(tmp_path)) == 0
    job, argv = recorder.installed[0]
    assert argv[argv.index("--config") + 1] == str(ini)
    assert argv.index("--config") < argv.index("schedule")
    assert job.cwd is None
    assert _stored(tmp_path).config == str(ini)


def test_the_dry_run_and_the_install_get_the_same_command_line(tmp_path, monkeypatch, recorder):
    ini = tmp_path / "custom.ini"
    ini.write_text("[contextlake]\n", encoding="utf-8")
    seen = []
    recorder.render = lambda job, interval_s, exec_argv, **_: seen.append(
        (job, list(exec_argv))) or {"crontab": "x", "spec": "s"}
    actions.cmd_install(_args(config=str(ini)), {**_config(tmp_path), "dry_run": "true"})
    actions.cmd_install(_args(config=str(ini)), _config(tmp_path))
    assert seen[0] == recorder.installed[0]
    assert seen[0][1] != adapters.exec_argv_for("default")


def test_installing_again_takes_the_config_of_the_new_run(tmp_path, monkeypatch, recorder):
    ini = tmp_path / "custom.ini"
    ini.write_text("[contextlake]\n", encoding="utf-8")
    actions.cmd_install(_args(config=str(ini)), _config(tmp_path))
    ws = _ws(tmp_path)
    monkeypatch.chdir(ws)
    actions.cmd_install(_args(), _config(tmp_path))
    stored = _stored(tmp_path)
    assert (stored.config_source, stored.cwd) == (jobstore.DISCOVERED, str(ws))
    assert "--config" not in recorder.installed[-1][1]


def test_install_refuses_a_directory_a_unit_cannot_hold(tmp_path, monkeypatch, recorder):
    ws = _ws(tmp_path, "ws\nExecStartPre=x")
    monkeypatch.chdir(ws)
    assert actions.cmd_install(_args(), _config(tmp_path)) == 2
    assert recorder.installed == []
    assert not os.path.exists(jobstore.jobs_path(_config(tmp_path)))


def test_reset_from_another_directory_writes_the_unit_the_record_describes(
        tmp_path, monkeypatch, recorder):
    ws = _ws(tmp_path)
    monkeypatch.chdir(ws)
    actions.cmd_install(_args(), _config(tmp_path))
    ini = tmp_path / "custom.ini"
    ini.write_text("[contextlake]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path / "home")
    assert actions.cmd_reset(_args(config=str(ini)), _config(tmp_path)) == 0
    job, argv = recorder.installed[-1]
    assert job.cwd == str(ws)
    assert "--config" not in argv


def test_reset_of_an_explicit_config_job_keeps_the_flag(tmp_path, recorder):
    ini = tmp_path / "custom.ini"
    ini.write_text("[contextlake]\n", encoding="utf-8")
    actions.cmd_install(_args(config=str(ini)), _config(tmp_path))
    actions.cmd_reset(_args(), _config(tmp_path))
    assert recorder.installed[-1][1] == recorder.installed[0][1]


def test_reset_never_turns_a_discovered_path_into_dash_dash_config(tmp_path, recorder):
    """The record is a file on disk, and reset renders the unit from it. A record that names
    a config found by walking up is rendered with the directory and no flag. Passing the path
    as `--config` would give the file the trust of one the user named, and keep keys such as
    `gitlab_host` that a discovered file may not set."""
    path = jobstore.jobs_path(_config(tmp_path))
    jobstore.write_job(path, jobstore.new_job("default", ["bootstrap"], "auto", "cron", origin={
        "config": str(tmp_path / "ws" / ".contextlake.ini"),
        "config_source": jobstore.DISCOVERED,
        "kb_config": str(tmp_path / "ws" / ".contextlake.kb.toml"),
        "kb_config_source": jobstore.DISCOVERED, "cwd": str(tmp_path / "ws")}))
    assert actions.cmd_reset(_args(), _config(tmp_path)) == 0
    job, argv = recorder.installed[0]
    assert "--config" not in argv and "--kb-config" not in argv
    assert str(tmp_path / "ws") not in " ".join(argv)
    assert job.cwd == str(tmp_path / "ws")


def test_reset_of_a_record_from_before_these_fields_writes_the_unit_it_always_did(
        tmp_path, monkeypatch, recorder):
    """Run from a workspace that has a config, so a reset that took the directory from this
    run instead of the record would show it."""
    monkeypatch.chdir(_ws(tmp_path))
    path = jobstore.jobs_path(_config(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"jobs": {"default": {"argv": ["bootstrap"], "interval": "2h",
                                        "platform": "cron"}}}, fh)
    assert actions.cmd_reset(_args(), _config(tmp_path)) == 0
    job, argv = recorder.installed[0]
    assert argv == adapters.exec_argv_for("default")
    assert job.cwd is None


def _interval_args(**kw):
    return _args(rest=["1h", "run", "version"], job="adhoc", **kw)


def test_a_new_ad_hoc_job_takes_the_config_of_the_run_that_creates_it(
        tmp_path, monkeypatch, recorder):
    ws = _ws(tmp_path)
    monkeypatch.chdir(ws)
    assert adhoc.cmd_interval(_interval_args(), _config(tmp_path)) == 0
    job, argv = recorder.installed[0]
    assert job.cwd == str(ws)
    assert "--config" not in argv


def test_an_ad_hoc_job_that_exists_keeps_its_config_when_run_again_elsewhere(
        tmp_path, monkeypatch, recorder):
    ini = tmp_path / "custom.ini"
    ini.write_text("[contextlake]\n", encoding="utf-8")
    adhoc.cmd_interval(_interval_args(config=str(ini)), _config(tmp_path))
    ws = _ws(tmp_path)
    monkeypatch.chdir(ws)
    adhoc.cmd_interval(_interval_args(), _config(tmp_path))
    job, argv = recorder.installed[-1]
    assert job.cwd is None
    assert argv[argv.index("--config") + 1] == str(ini)


def test_the_install_report_says_which_config_each_run_reads(tmp_path, monkeypatch):
    ws = _ws(tmp_path, kb=True)
    kb = tmp_path / "kb.toml"
    kb.write_text("[kb]\n", encoding="utf-8")
    monkeypatch.chdir(ws)
    origin = adapters.capture_origin(_args(kb_config=str(kb)))
    job = jobstore.new_job("default", ["bootstrap"], "auto", "cron", origin=origin)
    lines = adapters.origin_lines(job)
    assert lines[0] == f"knowledge config: {kb} (in the unit's command as --kb-config)"
    assert lines[1].startswith(f"working directory: {ws} ")
    assert str(ws / ".contextlake.ini") in lines[1]
    assert adapters.origin_lines(jobstore.new_job("x", ["bootstrap"], "auto", "cron")) == []


def test_the_install_report_prints_the_config_lines_only_for_an_adapter_that_starts_a_host_process(
        tmp_path, monkeypatch):
    """A container adapter does not pass the config or the directory to the image, and says so
    in its own note, so the report must not also say that the unit reads them."""
    ini = tmp_path / "custom.ini"
    ini.write_text("[contextlake]\n", encoding="utf-8")
    job = jobstore.new_job("default", ["bootstrap"], "auto", "k8s", origin={
        "config": str(ini), "config_source": jobstore.EXPLICIT, "cwd": str(tmp_path)})
    for reads_host_config, expected in ((True, True), (False, False)):
        adapter = _Recorder()
        adapter.reads_host_config = reads_host_config
        lines = []
        monkeypatch.setattr(adapters, "log", lines.append)
        adapters._report_installed(adapter, job, 3600, ["x"], "skip", "because", [],
                                   lambda interval: f"Installed, every {interval}.")
        assert any("in the unit's command as --config" in line for line in lines) is expected


# ---- the scheduled command gets the config the unit was installed with -----

INI, KB = "/a/x.ini", "/a/kb.toml"


@pytest.mark.parametrize("job_argv, lead", [
    (["bootstrap"], ["--config", INI, "--kb-config", KB]),
    (["bootstrap", "--force"], ["--config", INI, "--kb-config", KB]),
    (["kb", "wiki", "--force"], ["--config", KB]),
    (["--verbose", "kb", "wiki"], ["--config", KB]),
    (["kb", "who-knows", "x"], ["--config", KB]),
    (["doctor"], ["--config", KB]),
    (["mirror", "sync"], ["--config", INI]),
    (["version"], []),
    (["mirror"], []),
    (["no-such-command"], []),
])
def test_each_class_of_command_gets_the_file_it_reads(job_argv, lead):
    assert runner.child_argv(job_argv, INI, KB) == lead + job_argv


def test_a_knowledge_job_is_not_given_the_sync_ini_nor_a_mirror_job_the_knowledge_config():
    assert runner.child_argv(["kb", "wiki"], INI, None) == ["kb", "wiki"]
    assert runner.child_argv(["mirror", "sync"], None, KB) == ["mirror", "sync"]
    assert runner.child_argv(["bootstrap"], INI, None) == ["--config", INI, "bootstrap"]
    assert runner.child_argv(["bootstrap"], None, KB) == ["--kb-config", KB, "bootstrap"]


def test_a_flag_the_jobs_own_command_sets_is_kept_and_not_added_again():
    own = ["kb", "wiki", "--config", "/own.toml"]
    assert runner.child_argv(own, INI, KB) == own
    assert runner.child_argv(["bootstrap", "--kb-config", "/own.toml"], INI, KB) == [
        "--config", INI, "bootstrap", "--kb-config", "/own.toml"]


def test_a_run_given_no_config_does_not_parse_the_command(monkeypatch):
    monkeypatch.setattr(runner, "_parse_command",
                        lambda argv: pytest.fail("parsed a command with nothing to add"))
    assert runner.child_argv(["bootstrap"]) == ["bootstrap"]


def test_a_command_that_does_not_parse_is_left_for_the_child_to_report(capsys):
    argv = ["--no-such-flag", "bootstrap"]
    assert runner.child_argv(argv, INI, KB) == argv
    seen = capsys.readouterr()
    assert (seen.out, seen.err) == ("", "")


def test_a_command_that_does_not_parse_prints_nothing_once_logging_is_set_up(tmp_path):
    """The check above runs where the test harness has cleared the logger, so a message sent
    through `log()` would reach the redirected stream. In a run, logging is set up first and
    is bound to the real stdout. The did-you-mean path (`kb wikki`), an unknown flag, a
    namespace with no verb and `-h` are tried there."""
    code = (
        "from contextlake.logging_setup import setup_logging, log\n"
        "setup_logging(verbose=False, quiet=False, log_file=None, log_format='text', redact=None)\n"
        "from contextlake.schedule import runner\n"
        "for argv in (['kb', 'wikki'], ['--no-such-flag', 'bootstrap'], ['mirror'], ['kb'],\n"
        "             ['-h']):\n"
        f"    assert runner.child_argv(argv, {INI!r}, {KB!r}) == argv\n"
        "log('end of run')\n")
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120, check=False, env={**os.environ, "HOME": str(tmp_path)})
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stderr == ""
    assert run.stdout.strip().endswith("end of run") and len(run.stdout.strip().splitlines()) == 1


@pytest.mark.parametrize("job_argv, expected", [
    (["bootstrap"], {"config": INI, "kb_config": KB}),
    (["kb", "wiki"], {"config": KB, "kb_config": None}),
    (["mirror", "sync"], {"config": INI, "kb_config": None}),
])
def test_the_command_with_the_config_in_front_parses_to_the_config_it_names(
        job_argv, expected):
    from contextlake.cli import build_parser

    parsed = build_parser().parse_args(runner.child_argv(job_argv, INI, KB))
    assert {key: getattr(parsed, key) for key in expected} == expected


def test_the_schedule_help_says_the_config_flags_go_before_the_command():
    from contextlake.cli import build_parser

    parser = build_parser()
    help_text = parser._all_parsers["schedule"].format_help()
    assert "both before `schedule`" in help_text
    assert "does not take --kb-config after its own name" in help_text


def _cycle(tmp_path, monkeypatch, run_args, job):
    """One cycle of ``job`` under ``run_args``. Returns the argv the child was started with,
    and what the cycle logged."""
    config = _config(tmp_path)
    jobs_file = jobstore.jobs_path(config)
    jobstore.write_job(jobs_file, job)
    spawned, lines = [], []
    monkeypatch.setattr(gates, "check", lambda cfg: gates.GateResult(True, ""))
    monkeypatch.setattr(runner, "_spawn",
                        lambda argv, env, timeout=None: spawned.append(argv) or 0)
    monkeypatch.setattr(runner, "log", lines.append)
    assert runner._one_cycle(run_args, config, job, jobs_file) == 0
    return spawned[0], lines


def test_the_cycle_starts_the_child_with_the_config_the_run_was_given(tmp_path, monkeypatch):
    job = jobstore.new_job("default", ["bootstrap"], "auto", "cron")
    argv, lines = _cycle(tmp_path, monkeypatch, argparse.Namespace(
        job="default", config=INI, kb_config=KB), job)
    assert argv == ["--config", INI, "--kb-config", KB] + job.full_argv
    assert f"Running full cycle: contextlake --config {INI} --kb-config {KB} bootstrap --force" \
        in lines


@pytest.mark.parametrize("source", [jobstore.EXPLICIT, jobstore.DISCOVERED])
@pytest.mark.parametrize("run_args", [
    argparse.Namespace(job="default"),
    argparse.Namespace(job="default", config=None, kb_config=None),
])
def test_the_cycle_never_hands_the_child_a_path_it_read_from_the_record(
        tmp_path, monkeypatch, source, run_args):
    """The record is a file on disk, and `reset` renders the unit from it. A path in it that
    the run was not given on its own command line is not a config the user chose for this
    run: a discovered one would reach the child with the trust of a file named with
    `--config`, and an explicit one would reach it without the unit having carried it."""
    job = jobstore.new_job("default", ["bootstrap"], "auto", "cron", origin={
        "config": "/ws/.contextlake.ini", "config_source": source,
        "kb_config": "/ws/.contextlake.kb.toml", "kb_config_source": source, "cwd": "/ws"})
    argv, lines = _cycle(tmp_path, monkeypatch, run_args, job)
    assert argv == job.full_argv
    assert "--config" not in argv and "--kb-config" not in argv
    assert not any("/ws/" in line for line in lines)


# ---- the unit's command line finds the record -------------------------------

def _fire(job, home, config=None):
    """Run the command line a unit runs, in the directory the unit starts in, with the
    environment the user manager would give it."""
    env = {**os.environ, "HOME": str(home)}
    env.pop("XDG_CACHE_HOME", None)
    env.pop(cfgmod.NO_LOCAL_CONFIG_ENV, None)
    return subprocess.run(adapters.job_exec_argv(job), cwd=job.cwd or home, env=env,
                          capture_output=True, text=True, timeout=120, check=False)


@pytest.mark.skipif(runner.in_container(), reason="`schedule run` refuses an ephemeral store "
                                                  "inside a container")
def test_a_unit_installed_from_a_directory_with_a_config_finds_its_job(
        tmp_path, monkeypatch, recorder, _quiet_home):
    """The report's first case, end to end. The job is installed from a workspace whose local
    config names the group, then run from where the unit starts. Run from the home directory
    with no config, as the unit used to be, the same job is not found and the run exits 2."""
    ws = _ws(tmp_path)
    # work_dir is set because its default is expanded when `config` is imported, so a child
    # with a different HOME would otherwise derive a different cache directory.
    ws_ini = ws / ".contextlake.ini"
    ws_ini.write_text(ws_ini.read_text(encoding="utf-8")
                      + f"schedule_on_battery = run\nwork_dir = {tmp_path / 'repos'}\n",
                      encoding="utf-8")
    monkeypatch.chdir(ws)
    config = cfgmod.load_config(None)
    assert adhoc.cmd_interval(_interval_args(), config) == 0
    job, _ = recorder.installed[0]

    fired = _fire(job, _quiet_home)
    assert fired.returncode == 0, fired.stdout + fired.stderr

    old_unit = subprocess.run(
        adapters.exec_argv_for("adhoc"), cwd=_quiet_home, capture_output=True, text=True,
        env={**os.environ, "HOME": str(_quiet_home)}, timeout=120, check=False)
    assert old_unit.returncode == 2, old_unit.stdout + old_unit.stderr
    assert "No job named 'adhoc'" in old_unit.stdout + old_unit.stderr


@pytest.mark.skipif(runner.in_container(), reason="`schedule run` refuses an ephemeral store "
                                                  "inside a container")
def test_a_unit_installed_with_both_config_flags_finds_its_job(
        tmp_path, recorder, _quiet_home):
    """The report's second case. The flags sit before `schedule`, where `--kb-config` is
    accepted, and survive `main()`."""
    ini = tmp_path / "custom.ini"
    ini.write_text(f"[contextlake]\ngitlab_group = g\nschedule_on_battery = run\n"
                   f"cache_dir = {tmp_path / 'cache'}\n", encoding="utf-8")
    kb = tmp_path / "kb.toml"
    kb.write_text("[kb]\n", encoding="utf-8")
    config = cfgmod.load_config(str(ini))
    assert adhoc.cmd_interval(_interval_args(config=str(ini), kb_config=str(kb)), config) == 0
    job, argv = recorder.installed[0]
    assert argv[argv.index("--kb-config") + 1] == str(kb)

    fired = _fire(job, _quiet_home)
    assert fired.returncode == 0, fired.stdout + fired.stderr
    assert (tmp_path / "cache" / "schedule-history.jsonl").read_text(encoding="utf-8").strip()

@pytest.mark.skipif(runner.in_container(), reason="`schedule run` refuses an ephemeral store "
                                                  "inside a container")
def test_a_unit_hands_its_config_to_the_command_it_runs(
        tmp_path, monkeypatch, recorder, _quiet_home):
    """The unit finds its record, and the command it starts reads the same config. `bootstrap
    --dry-run` prints the group and work directory it loaded and runs no stage. Offline, so
    nothing here can reach a forge."""
    monkeypatch.setenv("CONTEXTLAKE_OFFLINE", "1")
    ini = tmp_path / "custom.ini"
    ini.write_text(f"[contextlake]\ngitlab_group = handed-down\n"
                   f"work_dir = {tmp_path / 'repos'}\nschedule_on_battery = run\n"
                   f"cache_dir = {tmp_path / 'cache'}\n", encoding="utf-8")
    kb = tmp_path / "kb.toml"
    kb.write_text("[kb]\n", encoding="utf-8")
    args = _args(config=str(ini), kb_config=str(kb), job="plan",
                 rest=["1h", "run", "bootstrap", "--dry-run"])
    assert adhoc.cmd_interval(args, cfgmod.load_config(str(ini))) == 0
    job, _ = recorder.installed[0]

    fired = _fire(job, _quiet_home)
    assert fired.returncode == 0, fired.stdout + fired.stderr
    assert "GitLab group: handed-down" in fired.stdout
    assert f"Working directory: {tmp_path / 'repos'}" in fired.stdout

    # Without the config in front of it, the same command loads the global one.
    bare = subprocess.run(
        [sys.executable, "-m", "contextlake", "bootstrap", "--dry-run"], cwd=_quiet_home,
        capture_output=True, text=True, timeout=120, check=False,
        env={**os.environ, "HOME": str(_quiet_home)})
    assert "handed-down" not in bare.stdout
