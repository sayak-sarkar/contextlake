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
