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


def _spawn_refresh(store_dir: Path, config: str | None,
                   targets: list[str], embed: bool = False) -> tuple[bool, Path]:
    """Start the repair commands in the background, detached from this process.

    Detached on purpose: the caller is often a session-start hook whose exit must not
    wait for, or kill, the work. Output goes to a file in the store because a
    background process with nowhere to write its errors fails invisibly, which is the
    class of bug this whole command exists to fight.

    ``targets`` are the repo **ids** the freshness check found stale. Each becomes its
    own ``index --source <id>``, which works because ``--source`` accepts an indexed
    repo id as well as a path (``cmds/index.py``'s ``_resolve_source_by_id``).

    **Naming the targets is the whole point of this function's signature.** It used to
    run a bare ``kb index`` with ``cwd=Path.home()``, and ``cmd_index`` with no target
    defaults to ``"."`` -- so the "update" indexed the user's HOME DIRECTORY and never
    touched the repositories the check had just reported as moved. Measured: the message
    said "Updating in the background", the store head never advanced, and the process sat
    in uninterruptible I/O past 96 seconds still holding the store's single-writer lock,
    so every other write was refused for the duration. On a machine where ``$HOME`` is
    itself a git repo it would have indexed the home directory into the knowledge store.

    So: never spawn an untargeted index, and never let the child's working directory
    decide what gets indexed. ``cwd`` is the store directory now -- a path that is
    guaranteed to exist and that no command interprets as a source.

    ``steer`` is the one step that does read its working directory (it writes there), so it
    gets ``--out`` with the caller's directory. It runs only where steering already exists
    (:func:`_holds_steering`). It used to write ``AGENTS.md``, ``.mcp.json`` and a session
    hook into the store directory and never touched the workspace it was meant to refresh.

    Returns ``(False, logfile)`` when there is nothing this function can repair, so the
    caller reports honestly rather than promising an update that will not happen.
    """
    logfile = Path(store_dir) / _LOG_NAME
    base = [sys.executable, "-m", "contextlake", "kb"]
    # Absolute: the child runs with cwd=store_dir, where a relative path is not found.
    cfg = ["--config", str(Path(config).expanduser().resolve())] if config else []
    steps = [base + cfg + ["index", "--source", t] for t in targets]
    if embed:
        # Stale vectors are not repaired by indexing; `embed` is their repair. Before
        # this, a store whose ONLY staleness was its vectors still spawned a bare index.
        steps.append(base + cfg + ["embed"])
    if not steps:
        return False, logfile
    lost = _unreachable_store_config(Path(store_dir), config)
    if lost:
        log(style.warn(f"Not updating in the background: the store comes from {lost}, which "
                       "was found by walking up from this directory. The background process "
                       "starts in the store directory and would not find it, so it would "
                       "open a different store. Pass --config with that file to allow it."))
        return False, logfile
    try:
        here = Path.cwd()
    except OSError:   # the directory was removed under us
        here = None
    if here is not None and _holds_steering(here):
        steps.append(base + cfg + ["steer", "--out", str(here)])
    script = " && ".join(" ".join(_quote(s)) for s in steps)
    try:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logfile.open("a", encoding="utf-8")
    except OSError:
        return False, logfile
    try:
        # `child_env` and not the inherited environment: this child runs `kb index`,
        # `kb embed` and `kb steer` in a new process with its own sockets, and
        # `--offline` as a flag leaves nothing for it to read. `kb embed` downloads a
        # model and reaches a remote endpoint, so an offline `kb refresh --refresh`
        # spawned an unguarded child. The env var makes the child install its own guard.
        from ... import netguard

        subprocess.Popen(["/bin/sh", "-c", script], stdout=fh, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True,
                         cwd=str(store_dir), env=netguard.child_env())
    except (OSError, ValueError):
        fh.close()
        return False, logfile
    # The child holds its own dup of the descriptor; this one has done its job.
    fh.close()
    return True, logfile


def _holds_steering(directory: Path) -> bool:
    """Whether ``directory`` already has steering that ``kb steer`` wrote.

    The background ``steer`` refreshes steering that exists. Started where there is none,
    it would create ``AGENTS.md`` and the other files and merge a session hook into
    ``.claude/settings.json``. Run from ``$HOME``, that hook applies to every session on
    the machine.
    """
    from ..steer.generate import BEGIN, SESSION_HOOK_MARK

    for rel, mark in (("AGENTS.md", BEGIN), ("CLAUDE.md", BEGIN),
                      (".claude/settings.json", SESSION_HOOK_MARK)):
        try:
            if mark in (directory / rel).read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
    return False


def _unreachable_store_config(store_dir: Path, config: str | None) -> str | None:
    """The config file that chose this store but that a child started in the store dir
    will not find, else None.

    A config found by walking up from the current directory is not forwarded as
    ``--config``: naming a file on the command line is what makes its gated keys trusted
    (``kb/trust.py``), and nobody named this one. Started in the store directory, the
    child walks up from there instead, and when the file is not on that path it opens
    ``~/.contextlake/kb`` (creating it if it is missing).
    """
    from ...config import find_ancestor_config
    from ..config import LOCAL_CONFIG, _read_toml

    local = find_ancestor_config(LOCAL_CONFIG)
    if not local or find_ancestor_config(LOCAL_CONFIG, start=str(store_dir)) == local:
        return None

    def sets_store(path: str | None) -> bool:
        return "store_dir" in (_read_toml(path).get("kb") or {})

    return local if sets_store(local) and not sets_store(config) else None


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
        # Repair exactly what the check found stale, named by repo id. `moved` and
        # `stale_parser` both mean "this repo's graph is out of date"; a repo can be in
        # both lists, so dedupe while keeping the check's order. `unreadable` is
        # deliberately excluded -- re-indexing a repo whose checkout is gone cannot help.
        targets = list(dict.fromkeys(f.moved + f.stale_parser))
        started, logfile = _spawn_refresh(Path(store_dir), getattr(args, "config", None),
                                          targets, embed=bool(f.vectors_stale))

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
