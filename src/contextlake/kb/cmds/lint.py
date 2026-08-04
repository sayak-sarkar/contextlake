"""`contextlake lint` -- structural graph-health checks."""

from __future__ import annotations

import json
from pathlib import Path

from ... import style
from ...logging_setup import log
from ..state import needs_reindex
from ._common import (
    _git_head,
    _open_store,
)


def lint_result(store, store_dir) -> dict:
    """Graph health as pure data: stale repos (HEAD moved past the index) + dangling
    edges (an endpoint whose node is missing). Shared by ``cmd_lint`` and the
    ``graph_health`` MCP tool. Reads local git HEADs — offline.
    """
    from ..store.shards import read_shard

    repos = store.list_repos()
    stale_repos: list[str] = []
    dangling: list[dict] = []
    checked = 0
    node_cache: dict[str, bool] = {}

    def _exists(node_id: str) -> bool:
        if node_id not in node_cache:
            node_cache[node_id] = store.get_node(node_id) is not None
        return node_cache[node_id]

    for r in repos:
        head = _git_head(Path(r.path)) if r.path else None
        if needs_reindex(store, r.id, head):
            stale_repos.append(r.id)
        shard = read_shard(store_dir, r.id)
        if shard is None:
            continue
        for e in shard.edges:
            checked += 1
            if not _exists(e.src) or not _exists(e.dst):
                dangling.append({"repo": r.id, "src": e.src,
                                 "relation": e.relation, "dst": e.dst})
    return {"repos": len(repos), "checked": checked,
            "stale": len(stale_repos), "dangling": len(dangling),
            "stale_repos": stale_repos, "dangling_sample": dangling[:20]}


def cmd_lint(args) -> int:
    """Graph-health checks: stale repos (HEAD moved) and dangling edges."""
    as_json = getattr(args, "json", False)
    if as_json:
        from ...logging_setup import use_stderr
        use_stderr()
    store, store_dir = _open_store(args)
    try:
        if not store.list_repos():
            if as_json:
                print(json.dumps({"repos": 0, "checked": 0, "stale": 0, "dangling": 0,
                                  "stale_repos": [], "dangling_sample": []}, indent=2))
                return 0
            log("Nothing indexed yet — run index first.")
            return 0
        res = lint_result(store, store_dir)
        clean = res["dangling"] == 0 and res["stale"] == 0
        if as_json:
            print(json.dumps(res, indent=2))
            return 0 if clean else 1
        for rid in res["stale_repos"]:
            log(f"  stale: {rid} (HEAD moved or never finished — re-run index)")
        for d in res["dangling_sample"]:
            log(f"  dangling: {d['repo']}: {d['src']} -{d['relation']}-> {d['dst']}")
        if res["dangling"] > 20:
            log(f"  … and {res['dangling'] - 20} more dangling edge(s)")
        glyph = style.ok() if clean else style.warn()
        log(f"{glyph} Lint: {res['repos']} repos, {res['checked']} edges checked — "
            f"{res['dangling']} dangling, {res['stale']} stale")
        return 0 if clean else 1
    finally:
        store.close()
