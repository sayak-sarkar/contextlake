"""`contextlake hook` -- install/uninstall the post-commit re-index hook."""

from __future__ import annotations

import os
from pathlib import Path

from ... import style
from ...logging_setup import log
from ._common import (
    _open_store,
)


def _git_root(path: Path) -> Path | None:
    """Nearest ancestor of ``path`` (inclusive) that is a git repository."""
    from ..git_hook import git_dir

    p = path.resolve()
    for cand in (p, *p.parents):
        if git_dir(cand) is not None:
            return cand
    return None


def _canonical_repo_id(root: Path, args) -> str:
    """The id the store already uses for this path, else the directory name.

    Matching the stored id keeps the hook's re-index updating the SAME node rather
    than creating a duplicate under a bare basename.
    """
    store = None
    try:
        store, _ = _open_store(args)
        rp = root.resolve()
        for repo in store.list_repos():
            if repo.path and Path(repo.path).resolve() == rp:
                return repo.id
    except Exception as e:  # noqa: BLE001 - store is optional here; fall back to the dir name
        # A brand-new store (never indexed) doesn't raise here -- SqliteStore creates
        # it on open -- so anything landing in this branch is a real problem (a bad
        # --config path, a corrupt/too-new store) that would otherwise silently wire
        # the hook to the wrong repo_id, or a hook that never fires again. Surface it
        # instead of pretending the fallback to the bare dir name was expected.
        log(style.warn(f"Could not resolve this repo's stored id ({e}); "
                        f"falling back to the directory name {root.name!r}, which may "
                        "not match how it's indexed."))
    finally:
        if store is not None:
            store.close()
    return root.name


def cmd_hook(args) -> int:
    """Install / uninstall / inspect the post-commit re-index hook (see git_hook.py)."""
    from .. import git_hook
    from ..parse import discover_repos

    action = getattr(args, "action", None) or "install"
    if action not in ("install", "uninstall", "status"):
        log(style.warn(f"Unknown hook action {action!r} — use install | uninstall | status."))
        return 1
    config = getattr(args, "config", None)
    workspace = getattr(args, "workspace", None)

    # Resolve targets as (repo_id, repo_path) pairs.
    if workspace:
        ws = Path(os.path.expanduser(workspace))
        targets = discover_repos(str(ws))
        if not targets:
            log(style.warn(f"No git repositories found under {ws}."))
            return 1
    else:
        start = Path(os.path.expanduser(getattr(args, "path", None) or "."))
        root = _git_root(start)
        if root is None:
            log(style.warn(f"{start} is not inside a git repository. Pass a repo path, "
                           "or --workspace DIR to wire a whole mirror."))
            return 1
        repo_id = getattr(args, "repo", None) or _canonical_repo_id(root, args)
        targets = [(repo_id, str(root))]

    if action == "status":
        marks = [(rid, git_hook.is_installed(rp)) for rid, rp in targets]
        on = sum(1 for _, ok in marks if ok)
        for rid, ok in marks:
            if not workspace or ok:   # single repo: always show; fleet: list only the wired ones
                log(f"  {style.ok() if ok else style.dim('·')} {rid}")
        log(style.ok(f"post-commit hook present on {on}/{len(targets)} repo(s)."))
        return 0

    counts: dict[str, int] = {}
    for rid, rp in targets:
        st = (git_hook.uninstall(rp) if action == "uninstall"
              else git_hook.install(rp, rid, config))
        counts[st] = counts.get(st, 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    if action == "uninstall":
        log(style.ok(f"post-commit hook: {summary} across {len(targets)} repo(s)."))
    else:
        log(style.ok(f"post-commit hook: {summary} across {len(targets)} repo(s). "
                     "Each commit now re-indexes that repo into the knowledge store."))
        if not config:
            log("  (uses the default kb.toml; pass --config to pin a specific one.)")
    return 0

