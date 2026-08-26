"""Append-only record of what past runs actually cost.

Written from ``_RunMetrics.write()``, which lives in ``main()``'s ``finally``.
Two consequences shape every function here:

1. **Nothing raises into the caller.** An exception in a ``finally`` replaces
   the run's real outcome with a traceback about telemetry. Losing a data point
   is strictly better.
2. **A half-written line is normal, not exceptional.** A power cut or a full
   disk mid-append leaves a truncated last line. Refusing to read the file
   because one line is malformed would throw away every good measurement to
   punish one bad one.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# Keeps the file small enough to read whole on every `schedule recommend`, and
# far more history than the median ever needs. At one run an hour that is eight
# days; at one every six hours it is fifty.
MAX_RECORDS = 200

FILENAME = "schedule-history.jsonl"

# A record without all four of these cannot be scored, so it is not a record.
REQUIRED = ("ts", "kind", "duration_s", "exit")


def history_path(config) -> str:
    """Where this workspace's history lives: beside the project cache.

    Keyed on the cache directory rather than on the knowledge store, so a
    mirror-only install with no ``[kb]`` extra still has somewhere to write.
    ``get_cache_paths`` returns file paths, so take the directory from the first
    one, exactly as ``cli.py``'s audit-report path does.
    """
    from ..config import get_cache_paths

    cache_file, _ = get_cache_paths(config)
    return os.path.join(os.path.dirname(cache_file) or ".", FILENAME)


def utc_now_iso() -> str:
    """The ``ts`` format every record uses. Separate so tests can compare
    against a real value rather than re-deriving the format string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid(record) -> bool:
    return isinstance(record, dict) and all(k in record for k in REQUIRED)


def read_runs(path) -> list:
    """Every readable record, oldest first. Never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    runs = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # A truncated final line, or a line from a future format version.
            continue
        if _valid(record):
            runs.append(record)
    return runs


def append_run(path, record) -> None:
    """Add one record and enforce the cap. Never raises.

    The cap is applied by rewriting the file when it is over, not on every
    append: the common path is one ``open(..., "a")`` and one ``write``.
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        return
    _trim(path)


def _trim(path) -> None:
    runs = read_runs(path)
    if len(runs) <= MAX_RECORDS:
        return
    keep = runs[-MAX_RECORDS:]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            for record in keep:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def clear_runs(path) -> int:
    """Delete the history. Returns how many records were discarded, so the
    caller can say what it is about to destroy before it does."""
    count = len(read_runs(path))
    try:
        os.unlink(path)
    except OSError:
        pass
    return count


def _parse_ts(text):
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def summarize(runs) -> dict:
    """What ``reset --history`` and ``status`` print: how much measurement is
    on the table."""
    stamps = [t for t in (_parse_ts(r.get("ts")) for r in runs) if t is not None]
    if not stamps:
        return {"count": len(runs), "days": 0.0, "first_ts": None, "last_ts": None}
    first, last = min(stamps), max(stamps)
    return {
        "count": len(runs),
        "days": (last - first).total_seconds() / 86400.0,
        "first_ts": first.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_ts": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
