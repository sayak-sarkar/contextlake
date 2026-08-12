"""`contextlake kb refresh` -- say whether the graph is current, and bring it up to date.

Built to be cheap enough to run at the start of every coding session (see
``--hook``), because the failure this addresses is a *visibility* failure: a store can
be arbitrarily far behind while everything it serves looks healthy.

Two deliberate properties:

* **The check is synchronous and bounded**; the update is **detached**. Blocking a
  session start on a re-index of a large fleet would be the cure being worse than the
  disease, and a re-index killed part-way is not a state this project has proved safe.
  Spawning it means the session opens now and the graph is current shortly after.
* **Concurrency is already handled**, not re-invented here. Write commands take the
  cooperative store lock (``kb/lock.py``), so a second session's refresh refuses
  cleanly instead of interleaving writes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ... import style
from ...logging_setup import log
from ._common import _open_store

_LOG_NAME = "refresh.log"

DISABLE_ENV = "CONTEXTLAKE_NO_SESSION_REFRESH"
"""Switches the installed session hook off without editing a generated settings file.
A hook somebody cannot turn off in one command is a hook they delete."""


def _env_disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _spawn_refresh(store_dir: Path, config: str | None) -> tuple[bool, Path]:
    """Start `kb index` then `kb steer` in the background, detached from this process.

    Detached on purpose: the caller is often a session-start hook whose exit must not
    wait for, or kill, the work. Output goes to a file in the store because a
    background process with nowhere to write its errors fails invisibly, which is the
    class of bug this whole command exists to fight.
    """
    logfile = Path(store_dir) / _LOG_NAME
    base = [sys.executable, "-m", "contextlake", "kb"]
    cfg = ["--config", config] if config else []
    script = " && ".join([
        " ".join(_quote(base + cfg + ["index"])),
        " ".join(_quote(base + cfg + ["steer"])),
    ])
    try:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logfile.open("a", encoding="utf-8")
    except OSError:
        return False, logfile
    try:
        subprocess.Popen(["/bin/sh", "-c", script], stdout=fh, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True,
                         cwd=str(Path.home()))
    except (OSError, ValueError):
        fh.close()
        return False, logfile
    # The child holds its own dup of the descriptor; this one has done its job.
    fh.close()
    return True, logfile


def _quote(parts: list[str]) -> list[str]:
    from shlex import quote
    return [quote(p) for p in parts]


def cmd_refresh(args) -> int:
    """Report graph freshness; with ``--refresh`` also start an update in the background."""
    from .. import freshness

    as_hook = bool(getattr(args, "hook", False))
    as_json = bool(getattr(args, "json", False))
    if as_hook and _env_disabled():
        # Silent and successful: a hook that shouts about being disabled is noise on
        # every single session start.
        return 0
    if as_hook or as_json:
        # stdout carries the payload the caller parses -- a session-start hook's
        # stdout is read as JSON -- so human log lines go to stderr.
        from ...logging_setup import use_stderr
        use_stderr()

    store, store_dir = _open_store(args)
    try:
        budget = getattr(args, "budget", None)
        f = freshness.check(store, store_dir,
                            budget=float(budget) if budget else freshness.DEFAULT_BUDGET)
    finally:
        store.close()

    started = False
    logfile = Path(store_dir) / _LOG_NAME
    if getattr(args, "refresh", False) and f.is_stale:
        started, logfile = _spawn_refresh(Path(store_dir), getattr(args, "config", None))

    line = f.summary()
    if f.is_stale:
        if started:
            line += f" Updating in the background; progress in {logfile}."
        elif getattr(args, "refresh", False):
            line += " Could not start the background update; run `contextlake kb index`."
        else:
            line += " Run `contextlake kb index` to bring it up to date."

    if as_hook:
        # The shape a Claude Code SessionStart hook must print for its output to reach
        # the model. Verified against the schema shipped by Claude Code's own plugins,
        # which is the only session-hook contract this supports -- other editors are
        # not claimed.
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": line,
        }}))
        return 0
    if as_json:
        print(json.dumps({
            "repos": f.repos, "checked": f.checked, "unchecked": f.unchecked,
            "moved": f.moved, "unreadable": f.unreadable,
            "stale_parser": f.stale_parser, "vectors_stale": f.vectors_stale,
            "vectors_missing": f.vectors_missing, "is_stale": f.is_stale,
            "elapsed_seconds": f.elapsed, "refresh_started": started,
            "log": str(logfile) if started else None,
        }, indent=2))
        return 0

    log((style.warn if f.is_stale else style.ok)(line))
    for label, ids in (("moved", f.moved), ("older parser", f.stale_parser),
                       ("unreadable", f.unreadable)):
        for rid in ids[:10]:
            log(f"  {label}: {rid}")
        if len(ids) > 10:
            log(f"  ... and {len(ids) - 10} more {label}")
    log(f"  checked {f.checked}/{f.repos} in {f.elapsed}s"
        + (f", {f.unchecked} left unchecked (raise --budget to cover them)"
           if f.unchecked else ""))
    return 0


