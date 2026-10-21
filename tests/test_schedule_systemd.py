"""The systemd adapter: what it renders, and that it installs.

Golden-file assertions on `render` (pure, so no root, no systemd), plus a real
install/fire/uninstall cycle, because a renderable-but-broken unit is what golden files
cannot catch.

Two tests install a real unit into the systemd user manager of the person running the
suite. They skip unless ``CONTEXTLAKE_TEST_REAL_SYSTEMD=1`` is set.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

from contextlake.schedule import jobs, runner
from contextlake.schedule.platform import base, systemd

HAS_SYSTEMD = shutil.which("systemctl") is not None and base.get("systemd").usable()
needs_systemd = pytest.mark.skipif(not HAS_SYSTEMD, reason="systemd is not init here")

REAL_SYSTEMD_ENV = "CONTEXTLAKE_TEST_REAL_SYSTEMD"
"""Set to ``1`` to run the tests that install a unit into the real systemd user manager."""


def _require_real_systemd():
    """Skip unless systemd is here and the person running the suite asked for a real unit."""
    if not HAS_SYSTEMD:
        pytest.skip("systemd is not init here")
    if os.environ.get(REAL_SYSTEMD_ENV) != "1":
        pytest.skip(f"installs a real systemd user unit; set {REAL_SYSTEMD_ENV}=1 to run it")


@pytest.fixture
def real_systemd():
    """Request this first in a test that installs a unit into the real user manager."""
    _require_real_systemd()


def _job(name="default", argv=("version",)):
    return jobs.new_job(name, list(argv), "auto", "systemd")


def _unit_config(tmp_path):
    """``(ini, cache)``: a config file that names a cache directory under ``tmp_path``.

    A unit started by the systemd user manager has your real environment, not the test's, so
    this file, passed with ``--config``, is the only way its job store and run history reach
    ``tmp_path``."""
    cache = tmp_path / "cache"
    ini = tmp_path / "selftest.ini"
    ini.write_text(f"[contextlake]\ngitlab_group = selftest\ncache_dir = {cache}\n",
                   encoding="utf-8")
    return ini, cache


def _exec_argv(name="default", config=None):
    pinned = ["--config", str(config)] if config else []
    return [sys.executable, "-m", "contextlake", *pinned, "schedule", "run", "--job", name]


# ---- render (pure) ------------------------------------------------------

def test_render_produces_a_timer_and_a_service():
    files = systemd.SystemdAdapter().render(_job(), 4200.0, _exec_argv())
    assert sorted(files) == ["contextlake-default.service", "contextlake-default.timer"]


def test_the_timer_uses_the_exact_interval_not_a_rounded_one():
    files = systemd.SystemdAdapter().render(_job(), 4200.0, _exec_argv())
    assert "OnUnitInactiveSec=4200s" in files["contextlake-default.timer"]


def test_the_timer_catches_up_after_the_machine_sleeps():
    timer = systemd.SystemdAdapter().render(_job(), 3600.0, _exec_argv())[
        "contextlake-default.timer"]
    assert "Persistent=true" in timer
    assert systemd.SystemdAdapter().catches_up_after_sleep is True


def test_the_service_lets_systemd_do_the_battery_gate():
    """ConditionACPower needs no code at all, so it is strictly better than
    reading /sys ourselves."""
    service = systemd.SystemdAdapter().render(_job(), 3600.0, _exec_argv())[
        "contextlake-default.service"]
    assert "ConditionACPower=true" in service


def test_battery_gating_can_be_turned_off_in_the_unit():
    adapter = systemd.SystemdAdapter()
    service = adapter.render(_job(), 3600.0, _exec_argv(),
                             on_battery="run")["contextlake-default.service"]
    assert "ConditionACPower" not in service


def test_the_service_runs_the_resolved_interpreter_not_a_bare_name():
    """A moved venv otherwise fails silently forever."""
    service = systemd.SystemdAdapter().render(_job(), 3600.0, _exec_argv())[
        "contextlake-default.service"]
    assert f"ExecStart={sys.executable} -m contextlake schedule run --job default" in service


def _exec_start(argv):
    service = systemd.SystemdAdapter().render(_job(), 3600.0, argv)[
        "contextlake-default.service"]
    return next(line for line in service.splitlines() if line.startswith("ExecStart="))


_RUN = ["-m", "contextlake", "schedule", "run", "--job", "default"]


def test_an_interpreter_path_with_a_space_is_quoted_so_systemd_keeps_it_one_word():
    """systemd splits ExecStart= on white space. A venv under `/mnt/c/Users/First Last`
    ran `/mnt/c/Users/First` on every fire, while install printed success."""
    line = _exec_start(["/tmp/my project/.venv/bin/python", *_RUN])
    assert line == ('ExecStart="/tmp/my project/.venv/bin/python" '
                    '-m contextlake schedule run --job default')


def test_percent_and_dollar_are_doubled_so_systemd_does_not_expand_them():
    """`%d` is a specifier and `$HOME` a variable in ExecStart=. Unescaped, the path
    systemd runs is not the path that was written."""
    line = _exec_start(["/opt/p%d/$HOME/py", *_RUN])
    assert line == "ExecStart=/opt/p%%d/$$HOME/py -m contextlake schedule run --job default"


def _service(job, argv):
    return systemd.SystemdAdapter().render(job, 3600.0, argv)["contextlake-default.service"]


def _cwd_job(cwd):
    return jobs.new_job("default", ["version"], "auto", "systemd", origin={"cwd": cwd})


def test_a_job_with_a_directory_gets_a_working_directory_line():
    """A user unit starts in the user's home directory, so a config found by walking up from
    the install directory is reached only by starting there."""
    service = _service(_cwd_job("/home/me/ws"), _exec_argv())
    lines = service.splitlines()
    assert "WorkingDirectory=/home/me/ws" in lines
    assert lines.index("WorkingDirectory=/home/me/ws") < next(
        i for i, line in enumerate(lines) if line.startswith("ExecStart="))


def test_a_job_without_a_directory_has_no_working_directory_line():
    assert "WorkingDirectory" not in _service(_job(), _exec_argv())


def test_a_working_directory_with_a_space_or_a_quote_is_written_as_it_is():
    """systemd reads the whole value as the path. Quotes would become part of the path:
    `systemd-analyze verify` (systemd 258) on `WorkingDirectory="/tmp/a b/c"` reports "path
    is not absolute"."""
    assert 'WorkingDirectory=/tmp/a b/it"s' in _service(_cwd_job('/tmp/a b/it"s'), _exec_argv())


def test_a_percent_in_the_working_directory_is_doubled_so_systemd_keeps_it():
    assert "WorkingDirectory=/tmp/100%%" in _service(_cwd_job("/tmp/100%"), _exec_argv())


def test_a_control_character_in_the_directory_is_refused_not_written():
    """A newline starts a new directive, so a directory named for one would add its own."""
    with pytest.raises(ValueError, match="control character"):
        _service(_cwd_job("/tmp/ws\nExecStartPre=/bin/evil"), _exec_argv())


def test_a_config_path_with_a_space_stays_one_word_in_exec_start():
    argv = [sys.executable, "-m", "contextlake", "--config", "/home/me/my ws/x.ini",
            "--kb-config", "/home/me/kb%d.toml", "schedule", "run", "--job", "default"]
    service = _service(_job(), argv)
    line = next(ln for ln in service.splitlines() if ln.startswith("ExecStart="))
    assert '"/home/me/my ws/x.ini"' in line and "kb%%d.toml" in line
    assert systemd._split_exec_line(line.split("=", 1)[1]) == argv


@pytest.mark.parametrize("word", ["/a b/py", 'x"y', "x\\y", "100%", "$HOME", "plain", "", "a  b"])
def test_exec_start_words_read_back_as_they_were_written(word):
    line = systemd._exec_word(word)
    assert systemd._split_exec_line(line) == [word]


def test_an_exec_start_with_an_unclosed_quote_reads_as_cannot_tell():
    assert systemd._split_exec_line('"/a b -m contextlake') is None


def test_state_reads_the_command_and_directory_off_the_installed_service(monkeypatch, tmp_path):
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    monkeypatch.setattr(systemd, "_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    monkeypatch.setattr(systemd, "_linger_status", lambda: "Linger=yes\n")
    argv = [sys.executable, "-m", "contextlake", "--config", "/home/me/my ws/x.ini",
            "schedule", "run", "--job", "default"]
    adapter = systemd.SystemdAdapter()
    job = _cwd_job("/home/me/100% ws")
    adapter.install(job, 3600.0, argv)
    assert adapter.state(job)["launch"] == {"argv": argv, "cwd": "/home/me/100% ws"}

    adapter.install(_job(), 3600.0, argv)
    assert adapter.state(_job())["launch"] == {"argv": argv, "cwd": None}


def test_state_reads_a_service_earlier_versions_wrote_as_having_no_directory(
        monkeypatch, tmp_path):
    """The rc4 shape: no WorkingDirectory and no `--config`. `launch` says so, where None
    would say the unit could not be read."""
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    monkeypatch.setattr(systemd, "_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    monkeypatch.setattr(systemd, "_linger_status", lambda: "Linger=yes\n")
    (tmp_path / "contextlake-default.timer").write_text("[Timer]\nOnUnitInactiveSec=60s\n")
    (tmp_path / "contextlake-default.service").write_text(
        "[Service]\nType=oneshot\nExecStart=/opt/py -m contextlake schedule run --job default\n")
    launch = systemd.SystemdAdapter().state(_job())["launch"]
    assert launch == {"argv": ["/opt/py", "-m", "contextlake", "schedule", "run", "--job",
                               "default"], "cwd": None}


def test_state_has_no_launch_when_the_service_cannot_be_read(monkeypatch, tmp_path):
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    monkeypatch.setattr(systemd, "_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    monkeypatch.setattr(systemd, "_linger_status", lambda: "Linger=yes\n")
    (tmp_path / "contextlake-default.timer").write_text("[Timer]\nOnUnitInactiveSec=60s\n")
    assert systemd.SystemdAdapter().state(_job())["launch"] is None


def test_quotes_and_backslashes_in_a_path_are_escaped_inside_the_quotes():
    line = _exec_start(['/tmp/a"b\\c d/py', *_RUN])
    assert line == 'ExecStart="/tmp/a\\"b\\\\c d/py" -m contextlake schedule run --job default'


def test_a_plain_path_is_written_unquoted_as_it_always_was():
    assert _exec_start(["/venv/bin/python3.12", *_RUN]) == (
        "ExecStart=/venv/bin/python3.12 -m contextlake schedule run --job default")


def test_a_job_name_with_a_slash_or_a_space_is_refused():
    """The name becomes a filename and a unit name. Anything that could escape
    the directory or split the unit name is refused at render time."""
    for bad in ("../evil", "with space", "with/slash", "", "a" * 200):
        with pytest.raises(ValueError):
            systemd.SystemdAdapter().render(_job(name=bad), 3600.0, _exec_argv())


def test_render_writes_nothing(monkeypatch):
    """render() is pure, which is what makes the six backends of Plan 2
    testable without being installable.

    Asserts by intercepting the write primitives. An earlier version
    monkeypatched unit_dir and checked that a tmp_path stayed empty, but
    render() never calls unit_dir(), so tmp_path was empty either way and the
    test passed whether or not render wrote.
    """
    def _refuse(*a, **k):
        raise AssertionError(f"render() performed a filesystem write: {a[:1]}")

    monkeypatch.setattr("builtins.open", _refuse)
    monkeypatch.setattr(systemd.os, "makedirs", _refuse)
    monkeypatch.setattr(systemd.os, "replace", _refuse)
    files = systemd.SystemdAdapter().render(_job(), 3600.0, _exec_argv())
    assert sorted(files) == ["contextlake-default.service", "contextlake-default.timer"]


def test_the_rendered_unit_matches_the_golden_file():
    """Pins the whole text, so an accidental change to any directive is caught
    rather than only the three lines other tests assert on."""
    files = systemd.SystemdAdapter().render(_job(), 4200.0,
                                            ["/opt/py", "-m", "contextlake",
                                             "schedule", "run", "--job", "default"])
    assert files["contextlake-default.timer"] == (
        "# Managed by contextlake. Edits are overwritten by `contextlake schedule install`.\n"
        "[Unit]\n"
        "Description=contextlake scheduled run (default)\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=2m\n"
        "OnUnitInactiveSec=4200s\n"
        "Persistent=true\n"
        "AccuracySec=1m\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n")


# ---- ExecStart parsing (pure) --------------------------------------------

def test_exec_path_from_show_parses_the_modern_structured_form():
    """The format `systemctl --user show -p ExecStart` returns on a
    current systemd, captured from a real installed unit."""
    value = ("{ path=/home/x/.venv/bin/python3 ; argv[]=/home/x/.venv/bin/python3 "
             "-m contextlake schedule run --job default ; ignore_errors=no ; "
             "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")
    assert systemd._exec_path_from_show(value) == "/home/x/.venv/bin/python3"


def test_exec_path_from_show_keeps_a_path_that_has_a_space():
    """`schedule status` read `/tmp/my` out of `path=/tmp/my project/...` and reported the
    interpreter as moved or deleted."""
    value = ("{ path=/tmp/my project/.venv/bin/python ; argv[]=/tmp/my project/.venv/bin/python "
             "-m contextlake schedule run --job default ; ignore_errors=no ; pid=0 }")
    assert systemd._exec_path_from_show(value) == "/tmp/my project/.venv/bin/python"
    assert systemd._exec_path_from_show("{ path=/tmp/my project/py }") == "/tmp/my project/py"


def test_exec_path_from_show_reads_a_quoted_first_word_of_a_plain_command_line():
    assert (systemd._exec_path_from_show('"/tmp/my project/py" -m contextlake')
            == "/tmp/my project/py")


def test_exec_path_from_show_parses_a_plain_command_line():
    assert (systemd._exec_path_from_show("/usr/bin/contextlake schedule run")
           == "/usr/bin/contextlake")


def test_exec_path_from_show_returns_none_when_it_cannot_parse():
    """None means cannot tell, not missing: a status check must never accuse
    a healthy install on the strength of an unusual show value."""
    assert systemd._exec_path_from_show("") is None
    assert systemd._exec_path_from_show(None) is None
    assert systemd._exec_path_from_show("{ argv[]=/x/python }") is None


# ---- detection ----------------------------------------------------------

def test_usable_is_false_without_a_user_bus(monkeypatch):
    """systemctl plus /run/systemd/system prove systemd is init, not that a
    user manager is reachable. Installing unit files that can never be enabled
    is worse than reporting the adapter unusable."""
    import subprocess as sp

    monkeypatch.setattr(base.shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(base.os.path, "isdir", lambda _: True)
    monkeypatch.setattr(
        base.subprocess, "run",
        lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 1, b"", b""))
    assert base.systemd_is_init() is False


def test_usable_is_true_when_the_user_bus_answers(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(base.shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(base.os.path, "isdir", lambda _: True)
    monkeypatch.setattr(
        base.subprocess, "run",
        lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 0, b"", b""))
    assert base.systemd_is_init() is True


def test_detect_returns_an_id_that_get_accepts():
    assert base.get(base.detect()) is not None


def test_an_unknown_platform_name_is_a_clear_error():
    with pytest.raises(base.NoAdapter) as excinfo:
        base.get("windows-3.1")
    assert "systemd" in str(excinfo.value), "the error must list what IS available"


def test_available_never_claims_an_unusable_adapter():
    for name in base.available():
        assert base.get(name).usable() is True


# ---- the real thing -----------------------------------------------------

def test_a_real_unit_test_skips_without_the_opt_in(monkeypatch):
    """The tests below install a unit into the user manager of whoever runs the suite. Left
    to run by default, every `pytest` run did that, and fired the unit, which wrote to the
    real schedule history."""
    monkeypatch.setattr(sys.modules[__name__], "HAS_SYSTEMD", True)
    monkeypatch.delenv(REAL_SYSTEMD_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match=REAL_SYSTEMD_ENV):
        _require_real_systemd()
    monkeypatch.setenv(REAL_SYSTEMD_ENV, "0")
    with pytest.raises(pytest.skip.Exception, match=REAL_SYSTEMD_ENV):
        _require_real_systemd()


def test_a_real_unit_test_runs_when_the_opt_in_is_set(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "HAS_SYSTEMD", True)
    monkeypatch.setenv(REAL_SYSTEMD_ENV, "1")
    _require_real_systemd()


def test_a_real_unit_test_still_skips_where_systemd_is_absent(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "HAS_SYSTEMD", False)
    monkeypatch.setenv(REAL_SYSTEMD_ENV, "1")
    with pytest.raises(pytest.skip.Exception, match="not init"):
        _require_real_systemd()


@pytest.mark.parametrize("test", ["test_install_then_fire_then_uninstall",
                                  "test_state_reports_the_units_exec_path"])
def test_every_test_that_installs_a_real_unit_asks_for_the_gate(test):
    """Found by reading the signatures, so the check never installs anything. A test that
    drops `real_systemd` runs by default again."""
    import inspect

    assert "real_systemd" in inspect.signature(globals()[test]).parameters


def test_the_unit_command_with_the_pinned_config_writes_only_under_the_cache_dir(tmp_path):
    """Runs the command the unit runs, without systemd, in a HOME that is not the test's. The
    run history and job store land in the cache directory the config names. Nothing lands in
    that HOME, which stands in for the real one the user manager would hand the unit."""
    if runner.in_container():
        pytest.skip("`schedule run` refuses an ephemeral store inside a container")
    ini, cache = _unit_config(tmp_path)
    # The battery gate skips a run without recording it. This test needs the run.
    ini.write_text(ini.read_text(encoding="utf-8") + "schedule_on_battery = run\n",
                   encoding="utf-8")
    jobs.write_job(jobs.jobs_path({"cache_dir": str(cache)}),
                   jobs.new_job("selftest", ["version"], "1m", "systemd"))
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    env = {**os.environ, "HOME": str(other_home)}
    env.pop("XDG_CACHE_HOME", None)

    run = subprocess.run(_exec_argv("selftest", config=ini), cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120, check=False)

    assert run.returncode == 0, run.stderr
    assert (cache / "schedule-history.jsonl").read_text(encoding="utf-8").strip()
    assert list(other_home.rglob("*")) == []


def test_a_unit_run_for_a_job_with_no_record_creates_nothing_outside_the_cache_dir(tmp_path):
    """A timer can fire the moment it is enabled, before any job record exists. That run exits
    2, and without a pinned config it still creates a directory under the cache root of the
    HOME it is given. This is the run `test_state_reports_the_units_exec_path` can cause."""
    ini, _ = _unit_config(tmp_path)
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    env = {**os.environ, "HOME": str(other_home)}
    env.pop("XDG_CACHE_HOME", None)

    run = subprocess.run(_exec_argv("execpath-selftest", config=ini), cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120, check=False)

    assert run.returncode == 2, run.stderr
    assert "No job named 'execpath-selftest'" in run.stdout + run.stderr
    assert list(other_home.rglob("*")) == []


def test_the_exec_argv_pins_the_config_before_the_subcommand():
    argv = _exec_argv("x", config="/tmp/a.ini")
    assert argv[argv.index("--config") + 1] == "/tmp/a.ini"
    assert argv.index("--config") < argv.index("schedule")
    assert "--config" not in _exec_argv("x")


def test_install_then_fire_then_uninstall(real_systemd, tmp_path, monkeypatch):
    """END TO END on the real init system. A unit that renders but does not
    fire is the failure golden files cannot see.

    Needs ``CONTEXTLAKE_TEST_REAL_SYSTEMD=1``. When it runs it still installs a real
    unit into your systemd user manager, starts it and removes it. The job store and the
    run history go under ``tmp_path``: the unit is given a config that names that cache
    directory, because the user manager starts it with your real environment and not
    with this test's."""
    # The suite-wide `_isolated_home` fixture (tests/conftest.py) points HOME
    # at a throwaway directory so no test can write into the real one. This
    # test is the sanctioned exception: it must reach the systemd --user
    # instance already running for the real user, and that instance resolves
    # unit files under the real home, not this test's fake one. `pwd`, not
    # the environment, gives the real value regardless of what HOME is set to.
    import pwd

    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)

    # The unit's ExecStart runs the real CLI (`schedule run --job selftest`),
    # which looks the job up by name in the job store, the same one
    # `cmd_install` writes to. Installing the unit without a persisted job
    # record leaves nothing for that lookup to find: the process exits 2,
    # "No job named 'selftest'", and the timer never fires even though
    # the unit itself started. Real end to end means both halves are real.
    ini, cache = _unit_config(tmp_path)
    jobs_file = jobs.jobs_path({"cache_dir": str(cache)})

    name = "selftest"
    unit = systemd.unit_name(name)  # contextlake-selftest
    job = jobs.new_job(name, ["version"], "1m", "systemd")
    jobs.write_job(jobs_file, job)
    adapter = systemd.SystemdAdapter()
    written = adapter.install(job, 60.0, _exec_argv(name, config=ini))
    try:
        assert written
        assert adapter.state(job)["installed"] is True
        out = subprocess.run(["systemctl", "--user", "list-timers", f"{unit}.timer"],
                             capture_output=True, text=True, check=False)
        assert unit in out.stdout
        # Fire it now rather than waiting a minute for the timer.
        subprocess.run(["systemctl", "--user", "start", f"{unit}.service"], check=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            result = subprocess.run(
                ["systemctl", "--user", "show", f"{unit}.service",
                 "-p", "ExecMainStatus", "-p", "ActiveState", "-p", "ConditionResult"],
                capture_output=True, text=True, check=False)
            if "ActiveState=inactive" in result.stdout:
                break
            time.sleep(1)
        else:
            pytest.fail("the unit never completed within 60s")
        if "ConditionResult=no" in result.stdout:
            # On battery, `ConditionACPower` skips the unit: nothing ran, and a skipped
            # unit reports ExecMainStatus=0. That read as a pass for as long as the
            # machine was unplugged (found 2026-10-14), so it is reported as a skip.
            pytest.skip("systemd skipped the unit on an unmet condition (on battery?)")
        assert "ExecMainStatus=0" in result.stdout
    finally:
        # delete_job must run even if uninstall raises, or a killed/failed
        # run leaves `selftest` behind in the job store.
        try:
            adapter.uninstall(job)
        finally:
            jobs.delete_job(jobs_file, name)
    assert adapter.state(job)["installed"] is False
    leftovers = subprocess.run(["systemctl", "--user", "list-unit-files", f"{unit}*"],
                               capture_output=True, text=True, check=False)
    assert unit not in leftovers.stdout


def test_state_reports_the_units_exec_path(real_systemd, tmp_path, monkeypatch):
    """The parser above (test_exec_path_from_show_*) is proven against a
    fixture string; this proves the wiring: a real installed unit's
    `ExecStart` is read back and parsed to the same interpreter that was
    installed, not invented. That is the one path `status`'s
    missing-interpreter row depends on.

    Needs ``CONTEXTLAKE_TEST_REAL_SYSTEMD=1``, and installs then removes a real unit in
    your systemd user manager. The unit is given the same pinned config as the end-to-end
    test. Its timer can fire while the test runs, and a run for a job that has no record
    still creates a directory under the cache root it is given."""
    import pwd

    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)

    name = "execpath-selftest"
    job = jobs.new_job(name, ["version"], "1h", "systemd")
    adapter = systemd.SystemdAdapter()
    ini, _ = _unit_config(tmp_path)
    adapter.install(job, 3600.0, _exec_argv(name, config=ini))
    try:
        state = adapter.state(job)
    finally:
        adapter.uninstall(job)
    assert state["exec_path"] == sys.executable


@needs_systemd
def test_uninstalling_something_that_is_not_there_is_not_an_error():
    adapter = systemd.SystemdAdapter()
    assert adapter.uninstall(_job(name="never-installed")) == []


def test_the_unit_name_is_always_prefixed_exactly_once():
    """One naming rule. An earlier draft prefixed conditionally, which gave two
    rules that were each individually tested and each passing."""
    assert systemd.unit_name("default") == "contextlake-default"
    assert systemd.unit_name("selftest") == "contextlake-selftest"


def test_render_install_uninstall_and_state_agree_on_the_filename(monkeypatch, tmp_path):
    """The four methods must derive the same names, or uninstall leaves files
    behind and state reports a unit that is there as missing."""
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    # `state()` reads `.stdout` off whatever `_systemctl` returns, the same
    # as the real `subprocess.run` result it stands in for here. A stub
    # that returns `None` crashes `state()` before the naming assertions
    # below run at all, so this test would fail the same way whether the
    # naming agreed or not. That is not what this test is for.
    monkeypatch.setattr(systemd, "_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    job = _job(name="agree")
    adapter = systemd.SystemdAdapter()
    rendered = set(adapter.render(job, 3600.0, _exec_argv("agree")))
    written = {p.rsplit("/", 1)[-1] for p in adapter.install(job, 3600.0, _exec_argv("agree"))}
    assert rendered == written
    assert adapter.timer_unit(job) in rendered
    assert adapter.state(job)["installed"] is True
    removed = {p.rsplit("/", 1)[-1] for p in adapter.uninstall(job)}
    assert removed == written
    assert adapter.state(job)["installed"] is False
    assert list(tmp_path.iterdir()) == []


def _deny_unlink_in(monkeypatch, directory):
    """Make unlink fail with EACCES for files under `directory` only. chmod does nothing as
    root, which is how CI runs; the rest of the process keeps a working unlink."""
    real = os.unlink

    def _unlink(path, *a, **kw):
        if str(path).startswith(str(directory)):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", _unlink)


def test_uninstall_fails_when_the_unit_files_cannot_be_deleted(monkeypatch, tmp_path):
    """An unwritable unit directory is not "already gone". The files stay and the timer
    still fires, so uninstall must raise: cmd_uninstall then keeps the job record and exits
    1, where a swallowed error deleted the record and printed "Removed job"."""
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    monkeypatch.setattr(systemd, "_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    adapter = systemd.SystemdAdapter()
    job = _job(name="locked")
    adapter.install(job, 3600.0, _exec_argv("locked"))
    _deny_unlink_in(monkeypatch, tmp_path)

    with pytest.raises(PermissionError):
        adapter.uninstall(job)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "contextlake-locked.service", "contextlake-locked.timer"]


def test_uninstalling_units_that_are_not_on_disk_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    monkeypatch.setattr(systemd, "_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    assert systemd.SystemdAdapter().uninstall(_job(name="never-installed")) == []


@needs_systemd
def test_state_reports_linger_when_it_is_off(monkeypatch):
    """A user timer does not fire while logged out unless linger is on, so the
    adapter must say so rather than let a user believe a schedule is running.

    Controls loginctl's output. An earlier version asserted the note is always
    present, which is only true on a machine with linger off: it passed here
    and failed on CI, where the runner has linger enabled.
    """
    monkeypatch.setattr(systemd, "_linger_status", lambda: "Linger=no\n")
    notes = systemd.SystemdAdapter().state(_job())["notes"]
    assert any("linger" in note.lower() for note in notes)


def test_state_stays_quiet_when_linger_is_on(monkeypatch):
    """No note when the timer will fire regardless. A warning that is always
    printed is one users learn to ignore."""
    monkeypatch.setattr(systemd, "_linger_status", lambda: "Linger=yes\n")
    notes = systemd.SystemdAdapter().state(_job())["notes"]
    assert not any("linger" in note.lower() for note in notes)
