"""`schedule install` when the platform refuses the unit (G-5).

The command prints the unit so it can be installed by hand, and exits 1 because nothing is
scheduled. It used to exit 0, so a script reading the exit code saw a failed install as success.
"""

from __future__ import annotations

from pathlib import Path

from contextlake.cli import build_parser
from contextlake.schedule import actions, adapters, cmds
from contextlake.schedule import jobs as jobstore


class _Refusing:
    id = "fake"
    metadata_keys = ()

    def render(self, job, interval_s, exec_argv, **_):
        return {"fake.unit": f"every {interval_s}s run {' '.join(exec_argv)}"}

    def install(self, *a, **k):
        raise OSError("read-only file system")


def test_a_failed_install_exits_1_and_still_prints_the_unit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _Refusing())
    monkeypatch.setattr(actions, "log", lambda m="": print(m))
    cfg = {"cache_dir": str(tmp_path / "cache")}
    args = build_parser().parse_args(["schedule", "install", "--interval", "2h"])
    assert cmds.dispatch(args, cfg) == 1
    out = capsys.readouterr().out
    assert "Could not install the fake unit" in out
    assert "----- fake.unit -----" in out and "Install these yourself" in out
    assert not Path(jobstore.jobs_path(cfg)).exists(), "a job record was written for no unit"


class _StuckUnit:
    id = "fake"

    def uninstall(self, job):
        raise OSError("crontab -l failed")


def _seed_job(cfg):
    jobstore.write_job(jobstore.jobs_path(cfg),
                       jobstore.new_job(jobstore.DEFAULT_JOB, ["version"], "2h", "fake"))


def test_a_unit_that_cannot_be_removed_keeps_its_job_record(tmp_path, monkeypatch, capsys):
    """Stability tier A: the record was deleted anyway, which left a unit firing at a job
    that no longer existed, and `uninstall` exited 0."""
    from contextlake.schedule.platform import base

    monkeypatch.setattr(base, "get", lambda *a, **k: _StuckUnit())
    monkeypatch.setattr(actions, "log", lambda m="": print(m))
    cfg = {"cache_dir": str(tmp_path / "cache")}
    _seed_job(cfg)
    args = build_parser().parse_args(["schedule", "uninstall"])
    assert cmds.dispatch(args, cfg) == 1
    assert "The job record is kept" in capsys.readouterr().out
    assert jobstore.DEFAULT_JOB in jobstore.read_jobs(jobstore.jobs_path(cfg))


def test_a_platform_with_no_backend_here_still_removes_the_record(tmp_path, monkeypatch,
                                                                    capsys):
    """No backend for the platform on this machine means no unit of it is here."""
    from contextlake.schedule.platform import base

    def no_adapter(*a, **k):
        raise base.NoAdapter("no fake backend on this machine")

    monkeypatch.setattr(base, "get", no_adapter)
    monkeypatch.setattr(actions, "log", lambda m="": print(m))
    cfg = {"cache_dir": str(tmp_path / "cache")}
    _seed_job(cfg)
    args = build_parser().parse_args(["schedule", "uninstall"])
    assert cmds.dispatch(args, cfg) == 0
    assert jobstore.DEFAULT_JOB not in jobstore.read_jobs(jobstore.jobs_path(cfg))
