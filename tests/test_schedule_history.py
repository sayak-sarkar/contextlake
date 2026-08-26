"""The run-history store: append, read, cap, and survive corruption.

The recommender is only as good as this file, and this file is written by a
`finally` block on every run. So it must never raise into a caller, and it must
keep working after a half-written line (a power cut mid-append, a full disk).
"""
from __future__ import annotations

import json

import pytest

from contextlake.schedule import history


def _rec(i, **kw):
    rec = {"ts": f"2026-08-26T{i:02d}:00:00Z", "kind": "incremental",
           "duration_s": 100.0 + i, "exit": 0}
    rec.update(kw)
    return rec


def test_append_then_read_round_trips(tmp_path):
    path = str(tmp_path / "h.jsonl")
    history.append_run(path, _rec(1))
    history.append_run(path, _rec(2))
    runs = history.read_runs(path)
    assert [r["duration_s"] for r in runs] == [101.0, 102.0]


def test_read_of_a_missing_file_is_empty_not_an_error(tmp_path):
    assert history.read_runs(str(tmp_path / "nope.jsonl")) == []


def test_a_corrupt_line_is_skipped_and_the_rest_survive(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text(
        json.dumps(_rec(1)) + "\n"
        + '{"ts": "2026-08-26T02:00:00Z", "kind": "incr\n'   # truncated mid-write
        + json.dumps(_rec(3)) + "\n",
        encoding="utf-8")
    runs = history.read_runs(str(path))
    assert [r["duration_s"] for r in runs] == [101.0, 103.0]


def test_a_record_missing_a_required_key_is_skipped(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text(json.dumps({"ts": "x", "kind": "full"}) + "\n"
                    + json.dumps(_rec(4)) + "\n", encoding="utf-8")
    assert [r["duration_s"] for r in history.read_runs(str(path))] == [104.0]


def test_a_json_scalar_line_is_skipped(tmp_path):
    """Valid JSON that is not an object. `json.loads("7")` succeeds and returns
    an int, which would then explode on `.get()` in every consumer."""
    path = tmp_path / "h.jsonl"
    path.write_text("7\n" + json.dumps(_rec(5)) + "\n", encoding="utf-8")
    assert [r["duration_s"] for r in history.read_runs(str(path))] == [105.0]


def test_the_file_is_capped_at_max_records_keeping_the_newest(tmp_path):
    path = str(tmp_path / "h.jsonl")
    for i in range(history.MAX_RECORDS + 25):
        history.append_run(path, {"ts": "2026-08-26T00:00:00Z", "kind": "incremental",
                                  "duration_s": float(i), "exit": 0})
    runs = history.read_runs(path)
    assert len(runs) == history.MAX_RECORDS
    assert runs[0]["duration_s"] == 25.0
    assert runs[-1]["duration_s"] == float(history.MAX_RECORDS + 24)


def test_append_never_raises_when_the_target_cannot_be_written(tmp_path):
    """This runs inside main()'s `finally`. An exception here would REPLACE the
    real outcome of the run with a traceback about telemetry.

    A plain file standing where a directory component must go makes both
    os.makedirs and open fail with NotADirectoryError. The earlier version of
    this test pointed at a merely non-existent nested path, which makedirs
    creates happily, so it passed with the whole try/except deleted.
    """
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory", encoding="utf-8")
    history.append_run(str(blocker / "sub" / "h.jsonl"), _rec(1))


def test_append_never_raises_on_an_unserializable_record(tmp_path):
    """The TypeError/ValueError half of the same guard. json.dumps cannot
    serialize an arbitrary object, and that must lose the record, not the run."""
    history.append_run(str(tmp_path / "h.jsonl"), {"ts": "x", "kind": "incremental",
                                                   "duration_s": object(), "exit": 0})
    assert history.read_runs(str(tmp_path / "h.jsonl")) == []


def test_clear_runs_reports_how_many_it_discarded(tmp_path):
    path = str(tmp_path / "h.jsonl")
    for i in range(3):
        history.append_run(path, _rec(i))
    assert history.clear_runs(path) == 3
    assert history.read_runs(path) == []
    assert history.clear_runs(path) == 0


def test_summarize_counts_runs_and_spans_days():
    runs = [{"ts": "2026-08-20T00:00:00Z", "kind": "incremental", "duration_s": 1.0, "exit": 0},
            {"ts": "2026-08-26T00:00:00Z", "kind": "incremental", "duration_s": 1.0, "exit": 0}]
    s = history.summarize(runs)
    assert s["count"] == 2
    assert s["days"] == pytest.approx(6.0, abs=0.01)
    assert s["first_ts"] == "2026-08-20T00:00:00Z"


def test_summarize_of_nothing_is_zero_not_a_crash():
    assert history.summarize([])["count"] == 0
    assert history.summarize([])["days"] == 0.0


def test_history_path_lands_beside_the_project_cache(tmp_path):
    config = {"cache_dir": str(tmp_path), "cache_file": "projects.txt"}
    assert history.history_path(config) == str(tmp_path / "schedule-history.jsonl")
