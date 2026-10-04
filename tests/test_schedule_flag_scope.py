"""Every `schedule` flag acts on the actions it names, and is refused everywhere else.

`schedule` has one flat flag namespace, and its help scopes each flag with a prefix
("install:", "recommend/status/list:"). Nothing enforced the prefixes, so a flag given to
another action was accepted and ignored. `install --json` was documented as a preview and
installed the k8s manifest; `uninstall --interval`, `install --purge` and the rest did
nothing at all. `cmds.FLAG_ACTIONS` is the table `dispatch` enforces. These tests hold it to
the source that reads each flag and to the help text that describes it.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from contextlake.cli import build_parser
from contextlake.schedule import adapters, cmds
from contextlake.schedule import jobs as jobstore

SCHEDULE = Path(cmds.__file__).resolve().parent
_CMDS = {"recommend": "cmd_recommend", "install": "cmd_install", "uninstall": "cmd_uninstall",
         "status": "cmd_status", "run": "cmd_run", "list": "cmd_list", "reset": "cmd_reset",
         "interval": "cmd_interval"}


def _parse(argv):
    return build_parser().parse_args(argv)


@pytest.fixture(autouse=True)
def _no_real_adapter(monkeypatch):
    """No test here may reach a real platform adapter, even with a guard under test
    broken. Break-testing the stray-argument refusal without this sent one test into a
    real install, which ran `systemctl --user enable --now` against the developer's own
    user manager. Tests that need an adapter install a fake one over this."""
    monkeypatch.setattr(adapters, "_adapter_for",
                        lambda *a, **k: pytest.fail("this test reached a platform adapter"))


def _reads_by_action():
    """What each action's command function reads from ``args``, through the functions it
    passes ``args`` to, from the source."""
    reads, calls = {}, {}
    for f in ("actions.py", "adhoc.py", "report.py", "runner.py", "adapters.py"):
        for fn in ast.parse((SCHEDULE / f).read_text()).body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            r, c = set(), set()
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr"
                        and len(node.args) >= 2 and getattr(node.args[0], "id", None) == "args"
                        and isinstance(node.args[1], ast.Constant)):
                    r.add(node.args[1].value)
                if isinstance(node, ast.Attribute) and getattr(node.value, "id", None) == "args":
                    r.add(node.attr)
                if isinstance(node, ast.Call) and any(
                        isinstance(a, ast.Name) and a.id == "args" for a in node.args):
                    c.add(getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            reads[fn.name], calls[fn.name] = r, c

    def closure(name, seen):
        if name in seen:
            return set()
        seen.add(name)
        out = set(reads.get(name, ()))
        for c in calls.get(name, ()):
            out |= closure(c, seen)
        return out

    return {action: closure(fn, set()) for action, fn in _CMDS.items()}


def test_the_table_is_what_the_source_reads():
    reads = _reads_by_action()
    for dest, actions in cmds.FLAG_ACTIONS.items():
        if dest == "dry_run":
            continue        # read from the config (`dry_run = true` lands there too)
        assert {a for a, r in reads.items() if dest in r} == set(actions), dest
    # and nothing a command reads is missing from the table
    flags = {d for r in reads.values() for d in r} - {"action", "rest"}
    assert flags <= set(cmds.FLAG_ACTIONS)


def test_the_help_prefixes_match_the_table():
    sub = next(a for a in build_parser()._actions if a.dest == "command").choices["schedule"]
    checked = 0
    for act in sub._actions:
        head = (act.help or "").split(":", 1)[0]
        named = set(head.split("/"))
        if act.dest in cmds.FLAG_ACTIONS and named <= set(_CMDS):
            assert named == set(cmds.FLAG_ACTIONS[act.dest]), act.dest
            checked += 1
    assert checked >= 9


@pytest.mark.parametrize("argv", [
    ["schedule", "install", "--json"],
    ["schedule", "uninstall", "--dry-run"],
    ["schedule", "--dry-run", "status"],
    ["schedule", "install", "--purge"],
    ["schedule", "status", "--interval", "2h"],
    ["schedule", "recommend", "--job", "nightly"],
    ["schedule", "list", "--platform", "cron"],
    ["schedule", "run", "--yes"],
])
def test_a_flag_outside_its_actions_is_refused(argv):
    assert cmds.dispatch(_parse(argv), {}) == 2


def test_a_stray_argument_is_refused():
    assert cmds.dispatch(_parse(["schedule", "install", "nightly"]), {}) == 2


class _Adapter:
    id = "fake"
    metadata_keys = ()

    def render(self, job, interval_s, exec_argv, **_):
        return {"fake.unit": f"every {interval_s}s run {' '.join(exec_argv)}"}

    def install(self, *a, **k):
        raise AssertionError("a dry run must not install")


def _config(tmp_path):
    return {"cache_dir": str(tmp_path / "cache")}


@pytest.mark.parametrize("how", ["flag", "config"])
def test_install_dry_run_prints_the_unit_and_writes_nothing(tmp_path, monkeypatch, how, capsys):
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _Adapter())
    cfg = _config(tmp_path)
    argv = ["schedule", "install", "--interval", "2h"]
    if how == "flag":
        argv.append("--dry-run")
        from contextlake.cli import apply_cli_overrides
        args = _parse(argv)
        apply_cli_overrides(args, cfg)       # what main() does: the flag lands in the config
    else:
        args = _parse(argv)
        cfg["dry_run"] = "true"              # `dry_run = true` in the INI
    from contextlake.schedule import actions
    monkeypatch.setattr(actions, "log", lambda m="": print(m))
    assert cmds.dispatch(args, cfg) == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing installed" in out and "----- fake.unit -----" in out
    assert not Path(jobstore.jobs_path(cfg)).exists()


@pytest.mark.parametrize("action", ["uninstall", "reset"])
def test_a_config_dry_run_refuses_actions_with_no_preview(tmp_path, action):
    cfg = _config(tmp_path) | {"dry_run": "true"}
    assert cmds.dispatch(_parse(["schedule", action]), cfg) == 2


def test_an_unreadable_job_store_is_refused_before_anything_is_installed(tmp_path, monkeypatch):
    """Every writer rewrote the whole store from the empty mapping a malformed file reads
    as: one appended byte, and the next install dropped every other job record while their
    crontab lines stayed installed."""
    cfg = _config(tmp_path)
    store = Path(jobstore.jobs_path(cfg))
    store.parent.mkdir(parents=True, exist_ok=True)
    good = {"jobs": {"nightly": {"argv": ["mirror", "sync"], "interval": "1h"}}}
    store.write_text(json.dumps(good) + "x")
    before = store.read_bytes()
    for action in ("install", "uninstall", "reset"):
        assert cmds.dispatch(_parse(["schedule", action, "--job", "weekly"]), cfg) == 1, action
    assert store.read_bytes() == before
    with pytest.raises(jobstore.JobStoreUnreadable):
        jobstore.write_job(str(store), jobstore.new_job("w", ["kb", "index"], "1h", "cron"))
    with pytest.raises(jobstore.JobStoreUnreadable):
        jobstore.record_outcome(str(store), "nightly", 0, "2026-10-04T00:00:00Z")
    assert jobstore.read_jobs(str(store)) == {}          # readers still treat it as empty
