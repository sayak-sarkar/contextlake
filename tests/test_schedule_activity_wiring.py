"""The activity signal has to LEAVE the index, and something has to write it down.

Unit tests of a recommender pass while nothing ever calls it. That is
the branch_map lesson of 2026-08-25, and it is why the last two tests here run
the real CLI in a subprocess instead of asserting on a mock.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from contextlake import observability
from contextlake.schedule import history


def test_repo_activity_starts_unmeasured():
    observability.note_repo_activity(None, None)
    assert observability.repo_activity() == (None, None)


def test_note_repo_activity_round_trips():
    observability.note_repo_activity(480, 7)
    try:
        assert observability.repo_activity() == (480, 7)
    finally:
        observability.note_repo_activity(None, None)


def test_the_index_reports_its_unchanged_count(tmp_path, monkeypatch):
    """The gate at index.py:116 already knows which repos moved. This asserts
    the number reaches observability, not merely a log line."""
    pytest.importorskip("tree_sitter")
    index_cmd = pytest.importorskip("contextlake.kb.cmds.index")

    observability.note_repo_activity(None, None)
    workspace = tmp_path / "ws"
    for name in ("alpha", "beta"):
        repo = workspace / "acme" / name
        repo.mkdir(parents=True)
        (repo / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@e.x", "-c", "user.name=t",
                        "commit", "-qm", "c"], cwd=repo, check=True)

    args = _index_args(workspace, tmp_path / "store")
    assert index_cmd.cmd_index(args) == 0
    total, changed = observability.repo_activity()
    assert total == 2
    assert changed == 2, "a first index has nothing to skip, so everything changed"

    observability.note_repo_activity(None, None)
    assert index_cmd.cmd_index(args) == 0
    total, changed = observability.repo_activity()
    assert total == 2
    assert changed == 0, "a second index over unmoved HEADs must report zero changed"


def _index_args(workspace, store_dir):
    """The argparse namespace cmd_index reads. Built by hand so this test does
    not depend on the parser, which Task 4 changes."""
    import argparse

    return argparse.Namespace(
        workspace=str(workspace), source=None, path=None, repo=None, config=None,
        force=False, watch=False, interval=None, workers=None, quiet=True,
        verbose=False, store_dir=str(store_dir), out=None, args=[])


def _run_cli(tmp_path, argv, env_extra=None):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    env.update(env_extra or {})
    os.makedirs(env["HOME"], exist_ok=True)
    return subprocess.run([sys.executable, "-m", "contextlake"] + argv,
                          env=env, capture_output=True, text=True, timeout=120)


def test_a_scheduled_child_appends_a_history_record(tmp_path):
    """THE WIRING TEST. Runs the real CLI end to end and asserts the file grew.

    `version` is used because it is instant and cannot fail for an unrelated
    reason. The point is not what ran, it is that the two environment variables
    make ANY command record itself, which is what an ad-hoc job relies on.
    """
    hist = tmp_path / "h.jsonl"
    result = _run_cli(tmp_path, ["version"], {
        "CONTEXTLAKE_SCHEDULE_HISTORY": str(hist),
        "CONTEXTLAKE_SCHEDULE_KIND": "incremental"})
    assert result.returncode == 0, result.stderr
    runs = history.read_runs(str(hist))
    assert len(runs) == 1
    assert runs[0]["kind"] == "incremental"
    assert runs[0]["exit"] == 0
    assert runs[0]["duration_s"] >= 0.0


def test_the_recorded_kind_comes_from_the_environment(tmp_path):
    hist = tmp_path / "h.jsonl"
    _run_cli(tmp_path, ["version"], {
        "CONTEXTLAKE_SCHEDULE_HISTORY": str(hist),
        "CONTEXTLAKE_SCHEDULE_KIND": "full"})
    assert history.read_runs(str(hist))[0]["kind"] == "full"


def _history_files(tmp_path):
    """Every schedule-history.jsonl the CLI could have written under this test's
    isolated HOME and XDG_CACHE_HOME. The default path is derived from config, so
    a broken gate writes here rather than anywhere the test names."""
    return sorted((tmp_path / "cache").rglob("schedule-history.jsonl"))


def test_an_unscheduled_trivial_command_records_nothing(tmp_path):
    """Recording every invocation, `version` included, would flood the history
    with sub-second records and drive the median to zero.

    Asserts on the DEFAULT history path, which is where a broken gate would
    write. An earlier version of this test asserted on a path it never gave the
    subprocess, so it passed unconditionally.
    """
    result = _run_cli(tmp_path, ["version"])
    assert result.returncode == 0
    assert _history_files(tmp_path) == []


def test_a_hand_typed_sync_command_records_itself_at_the_default_path(tmp_path):
    """The `_HISTORY_COMMANDS` branch in `_run`, not `main`'s env-var branch.

    `mirror update` is the one mirror verb that works purely from what is
    already on disk (see `_GROUP_COMMANDS` in cli.py), so it needs no GitLab
    group and no network to reach the history-write call. No
    `CONTEXTLAKE_SCHEDULE_HISTORY` is set here: this is what a hand-typed
    `contextlake mirror update` does, unprompted.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = _run_cli(tmp_path, ["mirror", "update", "--work-dir", str(workspace)])
    assert result.returncode == 0, result.stderr

    files = _history_files(tmp_path)
    assert len(files) == 1
    runs = history.read_runs(str(files[0]))
    assert len(runs) == 1
    assert runs[0]["kind"] == "incremental"
    assert "duration_s" in runs[0]
    assert "exit" in runs[0]


def test_a_failing_scheduled_child_records_its_real_exit_code(tmp_path):
    hist = tmp_path / "h.jsonl"
    result = _run_cli(tmp_path, ["kb", "query"], {
        "CONTEXTLAKE_SCHEDULE_HISTORY": str(hist),
        "CONTEXTLAKE_SCHEDULE_KIND": "incremental"})
    assert result.returncode != 0
    runs = history.read_runs(str(hist))
    assert len(runs) == 1
    assert runs[0]["exit"] == result.returncode


def test_an_unwritable_history_path_does_not_break_the_run(tmp_path):
    """The append lives in a `finally`. A bad path must lose the data point,
    never the run."""
    result = _run_cli(tmp_path, ["version"], {
        "CONTEXTLAKE_SCHEDULE_HISTORY": "/proc/definitely/not/writable/h.jsonl",
        "CONTEXTLAKE_SCHEDULE_KIND": "incremental"})
    assert result.returncode == 0
    assert "Traceback" not in result.stderr


def test_the_recommender_reads_what_the_producer_wrote(tmp_path):
    """Closes the loop. Two independently-correct halves that never meet is the
    exact failure this test exists to catch."""
    from contextlake.schedule import recommend as R

    hist = tmp_path / "h.jsonl"
    for _ in range(3):
        _run_cli(tmp_path, ["version"], {
            "CONTEXTLAKE_SCHEDULE_HISTORY": str(hist),
            "CONTEXTLAKE_SCHEDULE_KIND": "incremental"})
    rec = R.recommend(history.read_runs(str(hist)))
    assert rec.measured is True
    assert rec.samples == 3
