"""What a networked MCP server recorded about the calls it answered.

One row per tool call that crossed the wrapper, plus one counted row per
``(outcome, key, tool, minute)`` for traffic that was never admitted. Rows
buffer in memory, flush on a timer and on ASGI lifespan shutdown, and land in
``<store_dir>/mcp-usage.jsonl``. ``contextlake kb keys usage`` reads them back
and ``kb keys list`` fills its ``LAST USED`` column from the same file.

THREE RULES SHAPE EVERY FUNCTION HERE.

1. **Nothing raises into the caller.** :meth:`Recorder.record` runs inside the
   tool wrapper's outer ``finally``, so a raise here would replace the tool's
   own return value on the success path and the tool's own exception on the
   error path. Losing a row beats losing an answer. Same stance as
   ``schedule/history.py``, and for the same reason.

2. **A half-written line is normal.** A power cut mid-append leaves a truncated
   last line. Refusing to read the file over one bad line would throw away
   every good measurement to punish it.

3. **Nothing a caller controls reaches a row.** The six fields have six closed
   sources: this process's clock, a key id this process minted, a tool name
   this process registered, one of twelve literals, an integer this process
   measured and an integer this process counted. That is structural, not a
   sanitiser: :meth:`Recorder.record` is keyword-only, takes no free-text
   parameter and takes no ``**kwargs``, so a seventh field is a signature change
   somebody has to argue for. A sanitiser is the thing the next person forgets.

THIS MODULE IMPORTS NOTHING FROM ``kb.server``, and a test asserts it. Reading
usage is a ``kb keys`` verb, and a subprocess test requires that importing
``kb keys`` pulls in neither ``kb.server`` nor ``mcp``. So the twelve literals
are spelled out below and a test pins the eight gate names against
``server.REFUSAL_CLASSES`` plus ``throttled``. That is the shape ``cli.py``
already uses for the ``kb keys`` verbs: it spells its ``choices=`` out rather
than importing ``keys_cmd``, and a test pins the two together.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

FILENAME = "mcp-usage.jsonl"

# Written by the tool wrapper, one row each. They come from a key the operator
# issued, they can carry a duration, and a percentile needs the individual
# values, so they are never counted into a bucket.
CALL_OUTCOMES = frozenset({"ok", "error", "denied"})
# Written by the tool wrapper, counted. The identity fault repeats on EVERY
# call for as long as it lasts, so a server that broke would otherwise evict
# the record showing it broke.
FAULT_OUTCOMES = frozenset({"identity_unset"})
# Written by the ASGI gate, counted. This traffic was never admitted, so
# nothing an operator set bounds how much of it arrives: a scanner at a
# thousand requests a second fills a twenty-thousand-row file in twenty seconds
# and evicts every real row.
GATE_OUTCOMES = frozenset({"throttled", "no_header", "wrong_scheme", "malformed",
                           "bad_checksum", "unknown", "revoked", "expired"})

OUTCOMES = CALL_OUTCOMES | FAULT_OUTCOMES | GATE_OUTCOMES
# The split is on WHO CONTROLS THE VOLUME, not on whether a row can carry a
# duration. A tool-axis `denied` row carries no `ms` and is still per-call: it
# came from an issued key and is bounded exactly the way an `ok` call is.
AGGREGATED = FAULT_OUTCOMES | GATE_OUTCOMES

# Every row carries all six, and a field that does not apply is null. `ms: 0`
# on a call that ran no body would drag every latency figure down.
FIELDS = ("ts", "key", "tool", "outcome", "ms", "n")

# Minute resolution, on a WALL clock. `build_server`'s `now=time.monotonic` is
# the rate and cost clock and is meaningless as a timestamp, so the recorder
# takes its own. A minute is the finest bucket that keeps the counted rows
# small, and it is finer than any question this file is asked.
TS_FORMAT = "%Y-%m-%dT%H:%MZ"

# 20,000 rows at about 105 bytes is 2.0 MiB. The margin is what stops the
# rewrite happening on every append once the file is at the cap: a trim fires
# only above MAX_LINES + TRIM_MARGIN and trims back to MAX_LINES, so it costs
# one rewrite per 2,001 rows rather than one per row. `schedule/history.py`
# trims on every append and says why that is fine there ("only a few tens of
# KB"); that reason does not hold at 2.0 MiB every ten seconds forever.
MAX_LINES = 20000
TRIM_MARGIN = 2000

# The flush interval, in seconds. A tool call does no disk I/O at all: it
# appends a dict to a list under a lock.
FLUSH_SECONDS = 10.0

# The in-memory bound. 5,000 rows at about 105 bytes is 525 KB, and over a
# ten-second flush it is 500 calls a second sustained, which a two-slot tool
# bound cannot reach on real work. At the cap a further call row is FOLDED into
# the counted map with its duration dropped, so the call is still counted and
# only its latency sample is lost. Losing a count is worse than losing a
# sample: CALLS is the number an operator bills and revokes on.
BUFFER_MAX_ROWS = 5000

# The bound on the fold line, copying `server._RefusalLog`'s window (see its
# docstring for why a bounded operator line and not a per-event one). One line
# per window, because an operator reading "CALLS 40000  P50 12ms" has to know
# the percentile is a sample. A silent sampling change is a write with no
# reader.
FOLD_WINDOW = 60.0


def usage_path(store_dir) -> str:
    """Where this store's usage file lives.

    Beside the store, which has a consequence worth stating rather than hiding:
    rebuilding the store discards the usage history with it. The alternative is
    a second directory nothing else uses, and a per-store file is what makes
    two servers on two stores two separate records.
    """
    return os.path.join(str(store_dir), FILENAME)


def utc_minute() -> str:
    """The `ts` every row carries, floored to the minute."""
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def parse_ts(text):
    """One `ts` as a datetime, or None.

    Its own parser rather than ``schedule/history.py:_parse_ts``, which reads
    ``%Y-%m-%dT%H:%M:%SZ``. Copied, it raises on every row here, and `--since`
    then filters everything or nothing.
    """
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _valid(row) -> bool:
    """A row this reader can score. Two checks, and the second is a trade.

    The FIELD check is forward-compatible the way ``schedule/history.py``'s is:
    a row carrying an extra field still scores.

    The VOCABULARY check is not, and that is deliberate. ``summarize`` branches
    on whether an outcome is counted or per-call, so a literal it does not know
    would fall into the per-call branch and be reported as a successful call.
    A thirteenth outcome from a newer contextlake is therefore DROPPED rather
    than mis-filed: an under-count an operator is told about beats a wrong
    number they are not. ``count_lines`` is what makes the drop visible --
    ``kb keys usage`` compares it against the rows it read and says how many
    lines it could not.
    """
    return (isinstance(row, dict) and all(k in row for k in FIELDS)
            and row.get("outcome") in OUTCOMES)


def read_rows(path) -> list:
    """Every readable row, oldest first. Never raises.

    A missing file is an empty list, not an error: the command that reads this
    runs on a machine that has never served anything.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # A truncated final line, or a line from a future format version.
            continue
        if _valid(row):
            rows.append(row)
    return rows


class Recorder:
    """Buffers rows and writes them in batches. Every method swallows.

    Three writers share one buffer and that is why the lock is not optional:
    tool bodies run on a worker thread (the MCP SDK dispatches every
    synchronous tool through ``anyio.to_thread.run_sync``), the ASGI gate runs
    on the event loop, and the flush runs on the event loop too.
    """

    def __init__(self, path, *, flush_seconds: float = FLUSH_SECONDS,
                 max_lines: int | None = None, trim_margin: int | None = None,
                 buffer_max_rows: int | None = None, wall=utc_minute,
                 clock=time.monotonic, stream=None) -> None:
        self.path = str(path)
        self.flush_seconds = flush_seconds
        # None means "read the module constant when it is needed", so a test
        # that patches `usage.MAX_LINES` changes the behaviour of a recorder
        # built before the patch. An explicit argument still wins.
        self._max_lines = max_lines
        self._trim_margin = trim_margin
        self._buffer_max_rows = buffer_max_rows
        self._wall = wall
        self._clock = clock
        self._stream = stream
        self._lock = threading.Lock()
        self._rows: list[dict] = []
        # (ts, outcome, key, tool) -> count
        self._counts: dict[tuple, int] = {}
        self._folded = 0
        self._fold_window = None
        # The file's line count, counted once on the first append and carried
        # from there. Counting it per flush would read the whole file every ten
        # seconds, which is the cost the margin exists to avoid.
        self._lines = None
        self._unknown_outcome_reported = False
        self._write_failure_reported = False

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def record(self, *, tool, outcome, key, ms=None, n: int = 1) -> None:
        """Add one event. Never raises, whatever is wrong with it.

        Keyword-only, with no free-text parameter and no ``**kwargs``. That is
        the whole privacy guarantee: there is nowhere to put caller text.

        An outcome outside :data:`OUTCOMES` is DROPPED rather than written, and
        reported once per process. A row carrying an unknown literal would
        break the reader's own vocabulary filter, so it would be a write that
        reads as a pass and counts as nothing.
        """
        try:
            if outcome not in OUTCOMES:
                self._report_unknown(outcome)
                return
            ts = self._wall()
            announce = False
            with self._lock:
                if outcome in AGGREGATED or len(self._rows) >= self._buffer_cap():
                    fold = outcome in CALL_OUTCOMES
                    bucket = (ts, outcome, key, tool)
                    self._counts[bucket] = self._counts.get(bucket, 0) + n
                    if fold:
                        self._folded += n
                        announce = self._fold_due()
                else:
                    self._rows.append({"ts": ts, "key": key, "tool": tool,
                                       "outcome": outcome, "ms": ms, "n": n})
            if announce:
                # Outside the lock: a write syscall must not serialise the tool
                # wrapper, which is the same rule `_RefusalLog` follows.
                self._write(
                    f"  MCP usage: buffer full, {self._folded} call rows counted "
                    "with no duration. CALLS stays exact; P50 and P95 are a sample.")
        except BaseException:
            return

    def flush(self) -> None:
        """Write everything buffered, then trim if the file is over. Never raises.

        The buffer is swapped out BEFORE the write, so a full disk drops one
        batch rather than growing the buffer until the process dies. That loss
        is announced once: an operator whose disk filled otherwise reads an
        empty file and concludes nobody called the server.
        """
        try:
            with self._lock:
                rows, counts = self._rows, self._counts
                if not rows and not counts:
                    return
                self._rows, self._counts = [], {}
            batch = list(rows)
            for (ts, outcome, key, tool), n in counts.items():
                # `ms` is null on every counted row by construction. A bucket
                # holds a count, and a count cannot produce a percentile.
                batch.append({"ts": ts, "key": key, "tool": tool,
                              "outcome": outcome, "ms": None, "n": n})
            self._append(batch)
        except BaseException as exc:
            self._report_write_failure(exc)
            return

    def close(self) -> None:
        """Flush what is left. One caller: the lifespan shutdown branch."""
        self.flush()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _buffer_cap(self) -> int:
        return BUFFER_MAX_ROWS if self._buffer_max_rows is None else self._buffer_max_rows

    def _limits(self) -> tuple[int, int]:
        cap = MAX_LINES if self._max_lines is None else self._max_lines
        margin = TRIM_MARGIN if self._trim_margin is None else self._trim_margin
        return cap, margin

    def _fold_due(self) -> bool:
        """True on the first fold of a window. Called under the lock."""
        now = self._clock()
        if self._fold_window is None or now - self._fold_window >= FOLD_WINDOW:
            self._fold_window = now
            return True
        return False

    def _report_unknown(self, outcome) -> None:
        if self._unknown_outcome_reported:
            return
        self._unknown_outcome_reported = True
        self._write(f"  MCP usage: dropping rows with outcome {outcome!r}, which is "
                    "not one this reader knows. Further ones are dropped in silence.")

    def _report_write_failure(self, exc) -> None:
        """Say the file is not being written, once. Cannot itself raise."""
        if self._write_failure_reported:
            return
        self._write_failure_reported = True
        try:
            self._write(f"  MCP usage: cannot write {self.path} ({exc!r}). Calls "
                        "are still served and still counted in memory; nothing "
                        "reaches the file, so `contextlake kb keys usage` will "
                        "under-report. Further failures are silent.")
        except BaseException:
            return

    def _write(self, line: str) -> None:
        print(line, file=self._stream or sys.stderr)

    def _append(self, batch) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if self._lines is None:
            self._lines = count_lines(self.path)
        # One open and one write for the whole batch. The flush runs on the
        # event loop, so this is about 1 KB of I/O every ten seconds.
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(row, sort_keys=True) + "\n" for row in batch))
        self._lines += len(batch)
        cap, margin = self._limits()
        if self._lines > cap + margin:
            self._trim(cap)

    def _trim(self, cap: int) -> None:
        """Rewrite to the newest `cap` rows, atomically.

        Bounded at 2.0 MiB read plus 2.0 MiB written at the shipped numbers,
        and it fires once per TRIM_MARGIN rows. Temp neighbour then
        ``os.replace``, the shape ``schedule/history.py:_trim`` and
        ``kb/config_edit.py`` already use, so a reader never sees a half file.
        """
        rows = read_rows(self.path)
        if len(rows) <= cap:
            self._lines = len(rows)
            return
        keep = rows[-cap:]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(row, sort_keys=True) + "\n" for row in keep))
        os.replace(tmp, self.path)
        self._lines = len(keep)


def count_lines(path) -> int:
    """Non-blank lines in the file, however few of them this reader can score.

    Public because it is the reader's only way to tell "nothing was recorded"
    from "I could not read what was recorded". See :func:`_valid`.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


# ----------------------------------------------------------------------
# The `[serve]` options
# ----------------------------------------------------------------------

class UsageError(ValueError):
    """A `[serve]` usage value this module will not parse.

    Raised at startup, before the socket, so the offending string is named on
    the terminal rather than reaching a caller as a 500 with nothing in it.
    """


class UsageOptions:
    """What `[serve]` said about recording. Three values, all resolved."""

    __slots__ = ("enabled", "max_lines", "flush_seconds")

    def __init__(self, *, enabled: bool = True, max_lines: int = MAX_LINES,
                 flush_seconds: float = FLUSH_SECONDS) -> None:
        self.enabled = enabled
        self.max_lines = max_lines
        self.flush_seconds = flush_seconds


# The three keys this module reads. They join `keyfile.SERVE_KEYS`, which is
# what buys them the typo check: `usage_max_line = 1` is then a warned line
# rather than a silently unbounded file.
SERVE_KEYS = ("usage", "usage_max_lines", "usage_flush_seconds")


def parse_serve_options(table) -> UsageOptions:
    """Read the three `[serve]` usage keys out of an already-trusted table.

    The table comes from ``keyfile.trusted_serve_table``, which is the same
    privileged-provenance gate ``keys_file`` and the quota defaults go through:
    a ``.contextlake.kb.toml`` found by walking up from the cwd sits inside a
    repository checkout, and a retention cap a checkout can rewrite is not a
    cap.
    """
    table = table or {}
    enabled = table.get("usage", True)
    if not isinstance(enabled, bool):
        raise UsageError(f"[serve] usage = {enabled!r} is not true or false")
    return UsageOptions(enabled=enabled,
                        max_lines=_positive_int(table, "usage_max_lines", MAX_LINES),
                        flush_seconds=_positive_float(table, "usage_flush_seconds",
                                                      FLUSH_SECONDS))


def _positive_int(table, key, default: int) -> int:
    raw = table.get(key, default)
    # `bool` is an `int` in Python, so `usage_max_lines = true` would otherwise
    # be read as a cap of 1 and trim the file to one row.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise UsageError(f"[serve] {key} = {raw!r} is not a whole number of rows above 0")
    return raw


def _positive_float(table, key, default: float) -> float:
    raw = table.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise UsageError(f"[serve] {key} = {raw!r} is not a number of seconds above 0")
    return float(raw)


# ----------------------------------------------------------------------
# Reading, for `kb keys usage` and `kb keys list`
# ----------------------------------------------------------------------

class LastUsed:
    """The newest row per key, for the ``LAST USED`` column.

    THREE STATES, because null meant three things before this: no usage file at
    all, a file with no row for this key, and a real stamp. An operator reading
    one null for all three revokes a key that is in daily use.
    """

    NOT_RECORDED = "not-recorded"
    NO_ROWS = "no-rows"
    MEASURED = "measured"

    def __init__(self, path, *, rows=None, present: bool | None = None) -> None:
        self.path = str(path)
        if rows is None:
            present = os.path.exists(self.path) if present is None else present
            rows = read_rows(self.path) if present else []
        self.present = bool(present)
        newest: dict[str, str] = {}
        for row in rows:
            key = row.get("key")
            ts = row.get("ts")
            if not isinstance(key, str) or not isinstance(ts, str):
                continue
            if key not in newest or ts > newest[key]:
                # String compare, not a parse: the format is zero-padded and
                # fixed width, so lexical order IS chronological order and a
                # parse per row buys nothing.
                newest[key] = ts
        self._newest = newest

    def at(self, key_id):
        """The newest minute stamp for this key, or None."""
        return self._newest.get(key_id)

    def state(self, key_id) -> str:
        if not self.present:
            return self.NOT_RECORDED
        return self.MEASURED if key_id in self._newest else self.NO_ROWS

    def cell(self, key_id) -> str:
        """The table cell: a date, `never`, or `-`."""
        state = self.state(key_id)
        if state == self.NOT_RECORDED:
            return "-"
        if state == self.NO_ROWS:
            return "never"
        return self._newest[key_id].split("T")[0]


def percentile(values, pct: int):
    """Nearest-rank, over a sorted copy. None when nothing was measured.

    Nearest-rank rather than a mean or an interpolation, and one function for
    both columns so they cannot drift. A mean labelled P50 is a wrong number in
    a place nobody re-checks: [10, 10, 10, 10, 1000] has a median of 10 and a
    mean of 208.
    """
    ordered = sorted(v for v in values if isinstance(v, int))
    if not ordered:
        return None
    rank = -(-pct * len(ordered) // 100) - 1
    return ordered[max(0, min(rank, len(ordered) - 1))]


def summarize(rows, *, since=None) -> dict:
    """The numbers `kb keys usage` prints, text and `--json` alike.

    ONE COMPUTATION FOR BOTH RENDERINGS. Two of them disagree the first time
    somebody fixes a bucket in one place.

    IT SUMS `n`, NEVER COUNTS LINES. One `throttled` row carrying `n = 500`
    stands for five hundred refused requests and is one line in the file.

    `throttled` is counted twice on purpose, and the two counts answer
    different questions. It is a REFUSAL, so it lands in the refusals block
    with the rest of the traffic that never reached a tool. It is also the one
    refusal where the gate had already resolved an identity, so it lands in
    that key's THR column, which is the number an operator raising a quota
    reads.
    """
    if since is not None:
        rows = [r for r in rows if _at_or_after(r.get("ts"), since)]
    keys: dict = {}
    tools: dict[str, int] = {}
    refusals: dict[str, int] = {}
    calls = 0
    latencies: list[int] = []

    def key_entry(key):
        return keys.setdefault(key, {"key": key, "calls": 0, "error": 0,
                                     "denied": 0, "throttled": 0, "ms": []})

    for row in rows:
        outcome = row.get("outcome")
        n = row.get("n")
        n = n if isinstance(n, int) and n > 0 else 1
        key = row.get("key")
        if outcome in AGGREGATED:
            refusals[outcome] = refusals.get(outcome, 0) + n
            if outcome == "throttled" and isinstance(key, str):
                key_entry(key)["throttled"] += n
            continue
        entry = key_entry(key)
        entry["calls"] += n
        calls += n
        if outcome in ("error", "denied"):
            entry[outcome] += n
        ms = row.get("ms")
        if isinstance(ms, int):
            entry["ms"].append(ms)
            latencies.append(ms)
        tool = row.get("tool")
        if isinstance(tool, str):
            tools[tool] = tools.get(tool, 0) + n
    return {
        "calls": calls,
        # Its own number, beside `calls` rather than instead of it. A folded
        # buffer counts the call and drops its duration, so the percentiles are
        # computed over a smaller population than CALLS and an operator reading
        # `--json` a day later has no other way to see it.
        "measured": len(latencies),
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "keys": sorted((_key_summary(v) for v in keys.values()),
                       key=lambda e: (-e["calls"], e["key"] or "")),
        "tools": sorted(({"tool": t, "calls": c} for t, c in tools.items()),
                        key=lambda e: (-e["calls"], e["tool"])),
        "refusals": sorted(({"outcome": o, "n": c} for o, c in refusals.items()),
                           key=lambda e: (-e["n"], e["outcome"])),
        "refused_total": sum(refusals.values()),
    }


def _key_summary(entry) -> dict:
    ms = entry.pop("ms")
    entry["measured"] = len(ms)
    entry["p50"] = percentile(ms, 50)
    entry["p95"] = percentile(ms, 95)
    return entry


def _at_or_after(ts, since) -> bool:
    stamp = parse_ts(ts)
    return stamp is not None and stamp >= since
