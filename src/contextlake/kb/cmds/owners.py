"""`contextlake kb owners` -- SME-from-commit-history ownership."""

from __future__ import annotations

import json
from pathlib import Path

from ... import style
from ...logging_setup import log
from ._common import (
    _open_store,
    _repo_id_suggestions,
    _unknown_repo_msg,
)


def cmd_owners(args) -> int:
    """Likely owners / SMEs for a repo (or sub-path), from git commit history."""
    from ..ownership import compute_owners

    target = (getattr(args, "args", []) or [None])[0]
    subpath = getattr(args, "path", None)
    limit = getattr(args, "limit", None) or 10
    as_json = getattr(args, "json", False)
    if as_json:
        from ...logging_setup import use_stderr
        use_stderr()
    if not target:
        usage = "contextlake kb owners <repo|path> [--path SUBDIR] [--limit N]"
        if as_json:
            print(json.dumps({"error": "missing_argument", "usage": usage}, indent=2))
        else:
            log(f"usage: {usage}")
        return 2

    # Resolve a working dir: a directory on disk, else a repo id looked up in the store.
    if Path(target).is_dir():
        repo_path = Path(target).resolve()
        label = repo_path.name           # resolve first so "." yields the dir name
    else:
        store, _ = _open_store(args)
        try:
            repo = store.get_repo(target)
            if not repo or not repo.path:
                if as_json:
                    print(json.dumps({"error": "unknown_repo", "target": target,
                                      "suggestions": _repo_id_suggestions(store, target)},
                                     indent=2))
                    return 1
                log(_unknown_repo_msg(store, target))
                return 1
        finally:
            store.close()
        repo_path, label = Path(repo.path), repo.id

    owners = compute_owners(repo_path, subpath, limit=limit)
    scope = f"{label}:{subpath}" if subpath else label
    if as_json:
        print(json.dumps({
            "repo": label, "path": subpath,
            "owners": [{"name": o.name, "commits": o.commits, "lines": o.lines,
                       "last_active": o.last_active, "share": o.share}
                      for o in owners],
        }, indent=2))
        return 0
    if not owners:
        log(f"No commit history found for {scope}")
        return 0
    log(f"Owners / SMEs for {style.cyan(scope)} (recency-weighted):")
    for i, o in enumerate(owners, 1):
        log(f"  {i}. {o.name}  —  {o.commits} commit(s), {o.lines} line(s), "
            f"last {o.last_active}, {o.share * 100:.0f}%")
    return 0

