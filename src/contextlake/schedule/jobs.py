"""What the scheduler was told to run, and what happened last time.

One JSON object, read and rewritten whole. Unlike the history (append-only,
capped, write-heavy) this is a small mutable map of at most a handful of
entries, so a single document is simpler and atomically replaceable.

The record is authoritative. The INI supplies a default at creation time; the
platform unit is rendered FROM this and is never read back as truth.
"""
from __future__ import annotations

import json
import os
from collections import namedtuple

FILENAME = "schedule-jobs.json"

DEFAULT_JOB = "default"
# The built-in cycle. `bootstrap` is the end-to-end pipeline (mirror, index,
# connect, embed, enrich, wiki, steer) and it is already incremental at every
# stage. The full cycle adds --force, which re-parses and re-embeds everything.
DEFAULT_ARGV = ["bootstrap"]
DEFAULT_FULL_ARGV = ["bootstrap", "--force"]

Job = namedtuple("Job",
                 "name argv full_argv interval created platform "
                 "failures last_run last_exit")

_REQUIRED = ("argv", "interval")


def jobs_path(config) -> str:
    from .history import history_path

    return os.path.join(os.path.dirname(history_path(config)) or ".", FILENAME)


def new_job(name, argv, interval, platform, full_argv=None, created=None) -> Job:
    """A fresh record. ``full_argv`` defaults to ``argv``: an ad-hoc job has one
    command and there is no forced variant of it to invent."""
    from .history import utc_now_iso

    argv = [str(a) for a in argv]
    if full_argv is None:
        full_argv = list(DEFAULT_FULL_ARGV) if argv == DEFAULT_ARGV else list(argv)
    return Job(name=str(name), argv=argv, full_argv=[str(a) for a in full_argv],
               interval=str(interval), created=created or utc_now_iso(),
               platform=str(platform), failures=0, last_run=None, last_exit=None)


def _read_document(path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc.get("jobs", {}) if isinstance(doc, dict) else {}


def read_jobs(path) -> dict:
    """Every valid job, keyed by name. A malformed file reads as empty.

    A record whose ``argv`` is not a list of strings is dropped outright, not
    coerced: a shell string is what this store refuses to represent,
    because these values are handed to a unit file that runs unattended.
    """
    out = {}
    for name, raw in _read_document(path).items():
        if not isinstance(raw, dict) or any(k not in raw for k in _REQUIRED):
            continue
        argv = raw.get("argv")
        full = raw.get("full_argv") or argv
        if not (isinstance(argv, list) and argv
                and all(isinstance(a, str) for a in argv)):
            continue
        if not (isinstance(full, list) and all(isinstance(a, str) for a in full)):
            continue
        out[str(name)] = Job(
            name=str(name), argv=list(argv), full_argv=list(full),
            interval=str(raw.get("interval", "auto")),
            created=raw.get("created"), platform=raw.get("platform") or "",
            failures=int(raw.get("failures") or 0),
            last_run=raw.get("last_run"), last_exit=raw.get("last_exit"))
    return out


def _write_document(path, mapping) -> None:
    payload = {"version": 1, "jobs": {
        name: {"argv": job.argv, "full_argv": job.full_argv,
               "interval": job.interval, "created": job.created,
               "platform": job.platform, "failures": job.failures,
               "last_run": job.last_run, "last_exit": job.last_exit}
        for name, job in mapping.items()}}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def write_job(path, job) -> None:
    """Add or replace one job. Same name means replace, never duplicate."""
    mapping = read_jobs(path)
    mapping[job.name] = job
    _write_document(path, mapping)


def delete_job(path, name) -> bool:
    mapping = read_jobs(path)
    if name not in mapping:
        return False
    del mapping[name]
    _write_document(path, mapping)
    return True


def record_outcome(path, name, exit_code, ts):
    """Note how a run ended. Returns the updated job, or ``None`` if unknown.

    Consecutive failures accumulate so `run` can back off, and reset to zero on
    the first success. Resetting on success is what stops one bad night from
    holding the interval at the maximum for a week.
    """
    mapping = read_jobs(path)
    job = mapping.get(name)
    if job is None:
        return None
    updated = job._replace(
        failures=0 if exit_code == 0 else job.failures + 1,
        last_run=ts, last_exit=int(exit_code))
    mapping[name] = updated
    _write_document(path, mapping)
    return updated
