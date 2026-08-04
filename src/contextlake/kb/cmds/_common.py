"""Cross-cutting helpers shared by multiple kb command modules."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ... import observability, style
from ...logging_setup import log
from ..config import load_kb_config
from ..state import check_schema
from ..store.sqlite_store import SqliteStore


def _open_store(args) -> tuple[SqliteStore, Path]:
    cfg = load_kb_config(getattr(args, "config", None))
    store_dir = cfg.store_path
    db_path = store_dir / "index.sqlite"
    # Every kb command funnels through here, so this is the one place that knows
    # where the graph lives -- which is what `--metrics-file` needs to publish
    # its node/edge gauges at the end of the run, and what `--redact` needs to
    # keep the store's location out of a shared log.
    observability.note_store_path(db_path)
    observability.add_redactions(paths=[(store_dir, "<store>")])
    store = SqliteStore(db_path)
    check_schema(store)
    return store, store_dir


def _guard_store(store_dir, command: str) -> bool:
    """Take the store's single-writer lock. Returns False (with a clear message)
    if a live peer already holds it — so two writers never interleave on one store.
    Released automatically at process exit; a crashed holder's lock is reclaimed."""
    import atexit

    from ..lock import OVERRIDE_ENV, StoreBusy, StoreLock

    lock = StoreLock(store_dir, command)
    try:
        lock.acquire()
    except StoreBusy as e:
        h = e.holder
        age = max(0, int(time.time()) - int(h.get("started", 0)))
        log(style.warn("Another contextlake process is writing this store — refusing to "
                       "run concurrently (avoids interleaved, corrupting writes)."))
        log(f"  holder: pid {h.get('pid')} · {h.get('command')} · ~{age}s ago · {h.get('host')}")
        log(f"  store:  {store_dir}")
        log(f"  Wait for it to finish, then re-run. To override (rarely correct): "
            f"set {OVERRIDE_ENV}=1.")
        return False
    atexit.register(lock.release)
    return True


def _connect_targets(args, store) -> list[tuple[str, str]]:
    """Repos to enrich: --workspace tree, a single --source dir, else indexed repos —
    scoped to positional repo id(s) when given (``wiki <repo>`` does just that repo)."""
    workspace = getattr(args, "workspace", None)
    if workspace:
        from ..parse import discover_repos  # lazy

        return discover_repos(str(workspace))
    source = getattr(args, "source", None)
    if source and Path(source).is_dir():
        repo_id = getattr(args, "repo", None) or Path(source).name
        return [(repo_id, str(Path(source).resolve()))]
    targets = [(r.id, r.path) for r in store.list_repos() if r.path]
    wanted = {a for a in (getattr(args, "args", None) or []) if a}
    if wanted:
        targets = [t for t in targets if t[0] in wanted]
    return targets


def _repo_id_suggestions(store, target: str, n: int = 3) -> list[str]:
    """Stored repo ids closest to an unknown ``target``.

    Covers typos (fuzzy match) and a partial/suffix id: repo_id is now canonical
    (host/namespace/path, see repo_identity.py), so a user typing just the
    namespace/path tail -- ``team/billing/api`` -- should still point at the
    stored ``gitlab.example.com/acme/team/billing/api``.
    """
    import difflib

    ids = [r.id for r in store.list_repos()]
    tail = [i for i in ids if i == target or i.endswith("/" + target)]
    close = difflib.get_close_matches(target, ids, n=n, cutoff=0.5)
    out: list[str] = []
    for i in tail + close:
        if i not in out:
            out.append(i)
    return out[:n]


def _unknown_repo_msg(store, target: str) -> str:
    sugg = _repo_id_suggestions(store, target)
    if sugg:
        return f"Unknown repo {target!r}. Did you mean: {', '.join(sugg)}?"
    return f"Unknown repo {target!r}: index it first, or pass a path on disk"


def _watch_loop(run_once, *, interval: float = 60, iterations=None, sleep=time.sleep) -> int:
    """Run ``run_once`` every ``interval`` seconds until interrupted or ``iterations``
    is reached. Returns the number of runs; a KeyboardInterrupt stops gracefully."""
    runs = 0
    while iterations is None or runs < iterations:
        run_once()
        runs += 1
        if iterations is not None and runs >= iterations:
            break
        try:
            sleep(interval)
        except KeyboardInterrupt:
            break
    return runs


def _git_head(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None
