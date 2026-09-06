"""The usage recorder: the row, the vocabulary, the buffer and the file.

Everything here is about `kb/usage.py` on its own. The wrapper anchor, the gate
rows and the `kb keys usage` reader are in `test_serve_usage.py` and
`test_keys_cmd.py`.

Each test names the defect it exists to catch. Five carry the weight, and each
was break-tested by reverting BEHAVIOUR only, never a signature:

* move `record()`'s write above the buffer test -> the fold test reads a short
  CALLS number;
* drop `identity_unset` from `OUTCOMES` -> two tests fail;
* remove the `except` inside `record()` -> `test_record_never_raises` raises;
* replace the margin trim with a trim on every append -> the 500-append test
  reads 400 rewrites instead of 19;
* count lines instead of summing `n` -> the reader reads 1 instead of 500.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from contextlake.kb import usage


@pytest.fixture
def rec(tmp_path):
    """A recorder on a temp file with a fixed clock and a captured stream."""
    def _build(**kwargs):
        kwargs.setdefault("stream", io.StringIO())
        kwargs.setdefault("wall", lambda: "2026-09-07T10:00Z")
        return usage.Recorder(tmp_path / usage.FILENAME, **kwargs)

    return _build


def _rows(recorder):
    recorder.flush()
    return usage.read_rows(recorder.path)


# ---------------------------------------------------------------------------
# the vocabulary and the row
# ---------------------------------------------------------------------------


def test_usage_row_field_set_is_exact(rec):
    """A seventh field. Set equality, not membership: `>=` passes on an extra
    one, and an extra field is how caller text reaches a row."""
    r = rec()
    r.record(tool="search_code", outcome="ok", key="k_abc123", ms=11)
    row = _rows(r)[0]
    assert set(row) == set(usage.FIELDS)
    assert row == {"ts": "2026-09-07T10:00Z", "key": "k_abc123",
                   "tool": "search_code", "outcome": "ok", "ms": 11, "n": 1}


def test_usage_vocabulary_holds_twelve_literals():
    """A dropped literal. Every one of the twelve has a producer, and a
    producer whose literal left the set writes nothing at all."""
    assert len(usage.OUTCOMES) == 12
    assert usage.CALL_OUTCOMES == {"ok", "error", "denied"}
    assert usage.FAULT_OUTCOMES == {"identity_unset"}
    assert usage.AGGREGATED == usage.OUTCOMES - usage.CALL_OUTCOMES
    # `denied` is per-call, not counted. It comes from a key the operator
    # issued, so it is bounded the way an `ok` call is, and the repository axis
    # raises one from inside a tool body with a real duration on it.
    assert "denied" not in usage.AGGREGATED


def test_refusal_class_names_match_the_gate():
    """The two vocabularies drifting. `kb/usage.py` spells the eight gate names
    out so `kb keys` never imports `kb.server`; this is what makes the
    duplication safe, the same way the `kb keys` verbs are pinned to `cli.py`'s
    own `choices=`."""
    from contextlake.kb import server as server_mod

    assert usage.GATE_OUTCOMES - {"throttled"} == set(server_mod.REFUSAL_CLASSES)
    # `throttled` is NOT a 401 class and must not join that tuple: four
    # analytics readers enumerate the seven.
    assert "throttled" not in server_mod.REFUSAL_CLASSES


def test_usage_serve_keys_match_the_keystore():
    """A key this module parses that the config's typo check does not know.
    Without the pin, `usage_max_lines` set in kb.toml warns "unknown [serve]
    key" while being honoured."""
    from contextlake.kb import keyfile

    assert set(usage.SERVE_KEYS) <= set(keyfile.SERVE_KEYS)


def test_usage_module_does_not_import_server():
    """The import that breaks the local-first property.

    A subprocess, because in-process the answer is decided by whatever the rest
    of the session already imported.
    """
    code = ("import sys, contextlake.kb.usage;"
            "print(int('contextlake.kb.server' in sys.modules),"
            "      int('mcp' in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True).stdout.split()
    assert out == ["0", "0"]


# ---------------------------------------------------------------------------
# record(): what it refuses to do
# ---------------------------------------------------------------------------


def test_usage_records_no_argument_text(rec):
    """A field that carries caller text.

    Both halves. The scan alone proves the file is empty, not that the scan
    works, so a POSITIVE CONTROL pushes the needle through a LEGAL row -- a
    valid outcome with the needle as the tool name -- and requires the scan to
    flag it. Through an illegal outcome the row is dropped and the control
    would pass on a scan that sees nothing.
    """
    needle = "team_api/src/secret_pricing.py"
    r = rec()
    r.record(tool="search_code", outcome="ok", key="k_abc123", ms=4)
    r.record(tool="ask", outcome="error", key="k_abc123", ms=9)
    text = "".join(json.dumps(row) for row in _rows(r))
    assert needle not in text

    r.record(tool=needle, outcome="ok", key="k_abc123", ms=1)
    assert needle in "".join(json.dumps(row) for row in _rows(r)), (
        "the scan cannot see a needle it is given, so its zero above proves "
        "nothing")


def test_record_never_raises(rec):
    """A recorder bug turning every tool call into a transport error.

    It runs inside the wrapper's outer `finally`, so a raise there replaces the
    tool's own return value on the success path and its own exception on the
    error path.
    """
    def boom():
        raise RuntimeError("clock")

    r = rec(wall=boom)
    r.record(tool="t", outcome="ok", key="k_1", ms=1)

    r2 = rec()
    r2.record(tool="t", outcome="not_a_real_outcome", key="k_1")
    r2.record(tool="t", outcome="ok", key=object(), ms=1)
    r2.flush()

    r3 = usage.Recorder("/proc/definitely/not/writable/usage.jsonl",
                        stream=io.StringIO())
    r3.record(tool="t", outcome="ok", key="k_1", ms=1)
    r3.flush()
    r3.close()


def test_an_unknown_outcome_is_dropped_and_reported_once(rec):
    """A row with a literal the reader filters out: written, counted by
    nothing, and invisible in every column."""
    stream = io.StringIO()
    r = rec(stream=stream)
    for _ in range(3):
        r.record(tool="t", outcome="throttle", key="k_1")
    assert _rows(r) == []
    assert stream.getvalue().count("not one this reader knows") == 1


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def test_gate_outcomes_are_counted_and_call_outcomes_are_not(rec):
    """500 rows for an unauthenticated flood, which evicts every real row
    inside a minute at a thousand requests a second."""
    r = rec()
    for _ in range(500):
        r.record(tool=None, outcome="unknown", key=None)
    for _ in range(3):
        r.record(tool="ask", outcome="ok", key="k_1", ms=5)
    rows = _rows(r)

    counted = [row for row in rows if row["outcome"] == "unknown"]
    assert len(counted) == 1 and counted[0]["n"] == 500
    assert len([row for row in rows if row["outcome"] == "ok"]) == 3


def test_identity_unset_is_counted_not_kept_per_call(rec):
    """The fault repeats on EVERY call while it lasts, so per-call rows would
    evict the record that shows the server is broken."""
    r = rec()
    for _ in range(200):
        r.record(tool="ask", outcome="identity_unset", key=None)
    rows = _rows(r)
    assert len(rows) == 1
    assert rows[0]["n"] == 200 and rows[0]["key"] is None


def test_buffer_overflow_folds_and_announces(rec):
    """A silent sampling change. CALLS stays exact and only the duration is
    lost, and the operator is told once."""
    stream = io.StringIO()
    r = rec(buffer_max_rows=10, stream=stream)
    for _ in range(50):
        r.record(tool="ask", outcome="ok", key="k_1", ms=7)
    rows = _rows(r)

    assert sum(row["n"] for row in rows) == 50, "a call was lost, not sampled"
    assert len([row for row in rows if row["ms"] == 7]) == 10
    folded = [row for row in rows if row["ms"] is None]
    assert sum(row["n"] for row in folded) == 40
    assert stream.getvalue().count("buffer full") == 1


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def test_recording_does_no_disk_io_until_flush(rec, monkeypatch):
    """A write on the hot path. A tool call must cost a list append."""
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open",
                        lambda *a, **k: (opened.append(a[0]), real_open(*a, **k))[1])
    r = rec()
    for _ in range(10):
        r.record(tool="ask", outcome="ok", key="k_1", ms=3)
    assert opened == []
    r.flush()
    assert opened, "the flush wrote nothing"


def test_truncated_last_line_is_skipped(tmp_path):
    """One bad line discarding every good measurement. A power cut mid-append
    leaves a half line, and refusing the file punishes 3 good rows for it."""
    path = tmp_path / usage.FILENAME
    good = [{"ts": "2026-09-07T10:00Z", "key": "k_1", "tool": "ask",
             "outcome": "ok", "ms": i, "n": 1} for i in range(3)]
    path.write_text("".join(json.dumps(r) + "\n" for r in good)
                    + '{"ts": "2026-09-07T10:0')
    assert len(usage.read_rows(path)) == 3


def test_trim_rewrites_once_per_margin_not_once_per_append(tmp_path, monkeypatch):
    """A copy of `schedule/history.py:_trim`, which rewrites on every append
    once the file is at the cap. At 20,000 rows that is a 2.0 MiB read and a
    2.0 MiB write every ten seconds forever.

    The two implementations are separated by a factor of 20, not by a
    judgement: trim-on-every-append reads 400 here, the margin reads 19.
    """
    monkeypatch.setattr(usage, "MAX_LINES", 100)
    monkeypatch.setattr(usage, "TRIM_MARGIN", 20)
    rewrites = []
    real_replace = usage.os.replace
    monkeypatch.setattr(usage.os, "replace",
                        lambda a, b: (rewrites.append(b), real_replace(a, b))[1])

    r = usage.Recorder(tmp_path / usage.FILENAME, stream=io.StringIO())
    for i in range(500):
        r.record(tool="ask", outcome="ok", key="k_1", ms=i)
        r.flush()
    assert len(rewrites) == 19, len(rewrites)


def test_trim_margin_is_patchable(tmp_path, monkeypatch):
    """A margin hard-coded inside the trim reads the same number whatever the
    constant says. The 0-trim half must not be written at the 2,000 default:
    500 appends never reach it, so it passes on a build with no trim at all."""
    monkeypatch.setattr(usage, "MAX_LINES", 100)
    monkeypatch.setattr(usage, "TRIM_MARGIN", 1000)
    rewrites = []
    monkeypatch.setattr(usage.os, "replace", lambda a, b: rewrites.append(b))

    r = usage.Recorder(tmp_path / usage.FILENAME, stream=io.StringIO())
    for i in range(500):
        r.record(tool="ask", outcome="ok", key="k_1", ms=i)
        r.flush()
    assert rewrites == []


def test_trim_keeps_the_newest_rows(tmp_path, monkeypatch):
    """A trim keeping the wrong end. `[:cap]` passes every count assertion and
    throws away everything that just happened."""
    monkeypatch.setattr(usage, "MAX_LINES", 10)
    monkeypatch.setattr(usage, "TRIM_MARGIN", 2)
    r = usage.Recorder(tmp_path / usage.FILENAME, stream=io.StringIO())
    for i in range(40):
        r.record(tool="ask", outcome="ok", key="k_1", ms=i)
        r.flush()
    kept = [row["ms"] for row in usage.read_rows(r.path)]
    assert kept[-1] == 39 and len(kept) <= 13
    assert kept == sorted(kept)


def test_a_write_failure_is_announced_once(tmp_path):
    """A full disk reading as an idle server.

    The buffer is swapped out before the write, so a failed flush drops one
    batch rather than growing until the process dies. Silent, that is an empty
    file an operator reads as "nobody called this server". Once, not per flush:
    a full disk stays full and a line per ten seconds fills what is left.
    """
    stream = io.StringIO()
    r = usage.Recorder("/proc/definitely/not/writable/usage.jsonl", stream=stream)
    for _ in range(3):
        r.record(tool="ask", outcome="ok", key="k_1", ms=1)
        r.flush()
    assert stream.getvalue().count("cannot write") == 1
    assert "under-report" in stream.getvalue()


def test_read_rows_on_a_missing_file_is_empty(tmp_path):
    """A missing file reading as an error would turn `kb keys list` into a
    failure on a machine that never served anything."""
    assert usage.read_rows(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# the `[serve]` options
# ---------------------------------------------------------------------------


def test_serve_usage_options_refuse_a_value_they_cannot_read():
    """A retention cap this server cannot parse, dropped in silence, leaves the
    file growing while an operator believes it is bounded."""
    assert usage.parse_serve_options({}).enabled is True
    assert usage.parse_serve_options({"usage": False}).enabled is False
    assert usage.parse_serve_options({"usage_max_lines": 50}).max_lines == 50

    for table in ({"usage": "no"}, {"usage_max_lines": "lots"},
                  {"usage_max_lines": 0}, {"usage_flush_seconds": -1},
                  # `bool` is an `int` in Python, so an unguarded isinstance
                  # test reads `true` as a cap of one row.
                  {"usage_max_lines": True}):
        with pytest.raises(usage.UsageError):
            usage.parse_serve_options(table)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def test_percentiles_are_nearest_rank_not_mean():
    """A mean labelled P50 is a wrong number in a place nobody re-checks."""
    values = [10, 10, 10, 10, 1000]
    assert usage.percentile(values, 50) == 10
    assert usage.percentile(values, 95) == 1000
    assert sum(values) / len(values) == 208
    # Nothing measured prints as nothing, never as zero.
    assert usage.percentile([], 50) is None
    assert usage.percentile([7], 50) == usage.percentile([7], 95) == 7


def test_summarize_sums_n_and_never_counts_lines():
    """One row standing for 500 refused requests reads 1 against a line
    counter, which is a two-orders-of-magnitude under-report of a flood."""
    rows = [{"ts": "2026-09-07T10:00Z", "key": None, "tool": None,
             "outcome": "unknown", "ms": None, "n": 500}]
    assert usage.summarize(rows)["refused_total"] == 500


def test_summarize_excludes_null_ms_from_the_percentiles():
    """A refusal row dragging every latency figure down, which is what `ms: 0`
    instead of null would do."""
    rows = ([{"ts": "2026-09-07T10:00Z", "key": "k_1", "tool": "ask",
              "outcome": "ok", "ms": 100, "n": 1}]
            + [{"ts": "2026-09-07T10:00Z", "key": "k_1", "tool": None,
                "outcome": "throttled", "ms": None, "n": 500}])
    summary = usage.summarize(rows)
    assert summary["p50"] == 100 and summary["p95"] == 100
    assert summary["measured"] == 1
    assert summary["keys"][0]["throttled"] == 500


def test_a_repo_axis_denied_row_counts_in_the_percentiles():
    """"Exclude every `denied` row" reads 10 where the answer is 1000.

    A tool-axis refusal is raised above the slot and carries no duration. A
    repository-axis refusal is raised from inside the tool body, which took a
    slot and spent real time, so its `ms` is a measurement.
    """
    rows = [{"ts": "2026-09-07T10:00Z", "key": "k_1", "tool": "search_code",
             "outcome": "ok", "ms": 10, "n": 1},
            {"ts": "2026-09-07T10:00Z", "key": "k_1", "tool": "search_code",
             "outcome": "denied", "ms": 1000, "n": 1}]
    summary = usage.summarize(rows)
    assert summary["p95"] == 1000
    assert summary["keys"][0]["denied"] == 1


def test_summarize_key_and_tool_totals_agree():
    """Two views of one number disagreeing is how a report stops being read."""
    rows = [{"ts": "2026-09-07T10:00Z", "key": f"k_{i % 3}", "tool": f"t{i % 4}",
             "outcome": "ok", "ms": i, "n": 1} for i in range(24)]
    summary = usage.summarize(rows)
    assert sum(k["calls"] for k in summary["keys"]) == summary["calls"] == 24
    assert sum(t["calls"] for t in summary["tools"]) == summary["calls"]


def test_since_filters_by_the_row_stamp():
    """A `--since` that filters nothing. The two counts must DIFFER, or the
    assertion passes on a parser that returns everything."""
    from datetime import datetime, timezone

    rows = [{"ts": "2026-09-06T10:00Z", "key": "k_1", "tool": "ask",
             "outcome": "ok", "ms": 1, "n": 1},
            {"ts": "2026-09-07T10:00Z", "key": "k_1", "tool": "ask",
             "outcome": "ok", "ms": 1, "n": 1}]
    cutoff = datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert usage.summarize(rows)["calls"] == 2
    assert usage.summarize(rows, since=cutoff)["calls"] == 1


def test_the_timestamp_parser_reads_this_modules_own_format():
    """`schedule/history.py:_parse_ts` reads `%Y-%m-%dT%H:%M:%SZ`. Copied here
    it raises on every row, and `--since` then filters everything or
    nothing."""
    assert usage.parse_ts("2026-09-07T10:00Z") is not None
    assert usage.parse_ts("2026-09-07T10:00:31Z") is None
    assert usage.parse_ts(None) is None


def test_last_used_has_three_states(tmp_path):
    """The overloaded null: no file, a file with no row for this key, and a
    real stamp are three answers, and one value for all three has an operator
    revoke a key that was used seconds ago."""
    path = tmp_path / usage.FILENAME
    absent = usage.LastUsed(path)
    assert absent.state("k_1") == usage.LastUsed.NOT_RECORDED
    assert absent.cell("k_1") == "-"
    assert absent.at("k_1") is None

    path.write_text(json.dumps({"ts": "2026-09-07T10:00Z", "key": "k_1",
                                "tool": "ask", "outcome": "ok", "ms": 1,
                                "n": 1}) + "\n")
    present = usage.LastUsed(path)
    assert present.state("k_1") == usage.LastUsed.MEASURED
    assert present.cell("k_1") == "2026-09-07"
    assert present.at("k_1") == "2026-09-07T10:00Z"
    assert present.state("k_2") == usage.LastUsed.NO_ROWS
    assert present.cell("k_2") == "never"
