"""`contextlake lint` -- structural graph-health checks."""

from __future__ import annotations

import json
from pathlib import Path

from ... import style
from ...logging_setup import log
from ..state import needs_reindex
from ._common import (
    _git_commit_state,
    _git_head,
    _open_store,
)


def lint_result(store, store_dir) -> dict:
    """Graph health as pure data: stale repos (HEAD moved past the index), repos
    whose graph an older parser built, and dangling edges (an endpoint whose node
    is missing). Shared by ``cmd_lint`` and the ``graph_health`` MCP tool. Reads
    local git HEADs — offline.

    The two staleness questions are reported separately and *both* counts include
    a repo that is both. ``stale`` answers "the code moved on"; ``parser_stale``
    answers "this build would not produce the graph on disk" — the question
    ``doctor`` asks and ``kb index`` acts on by rebuilding. Partitioning them
    (counting a repo only under the first that matched) would make lint's
    parser-stale count disagree with doctor's for any repo that is also
    HEAD-stale, which is the disagreement this exists to end.

    ``empty`` is carved out of ``stale`` rather than folded into it. A repository
    with no commits has no HEAD to compare against, so it satisfies the staleness
    test permanently: it was reported stale on every run, told to re-run index,
    and re-running index could never clear it because there is nothing to index.
    That is worse than saying nothing. It is a fact about the repository, not a
    fault in the store, so it does not count against a clean lint either.
    ``unreadable`` (the path is gone, or git will not answer for it) stays a
    fault: those repositories really are uncitable.
    """
    from ..parse import PARSER_VERSION  # lazy: tree-sitter, and only for this check
    from ..store.shards import read_shard

    repos = store.list_repos()
    stale_repos: list[str] = []
    empty_repos: list[str] = []
    unreadable_repos: list[dict] = []
    parser_stale_repos: list[str] = []
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
            # Only ask git why when it has something to explain. A repo whose HEAD
            # simply moved has a head in hand and needs no second call.
            state = "ok" if head else _git_commit_state(Path(r.path) if r.path else None)
            if state == "empty":
                empty_repos.append(r.id)
            elif state in ("missing", "unreadable"):
                unreadable_repos.append({"repo": r.id, "reason": state,
                                         "path": r.path or ""})
            else:
                stale_repos.append(r.id)
        shard = read_shard(store_dir, r.id)
        if shard is None:
            continue
        # Read off the shard, not the repos row, for two reasons: the shard is
        # already in hand for the dangling-edge walk below (so this costs
        # nothing), and it is the same source doctor reads, so the two commands
        # cannot report different numbers for the same store.
        if shard.parser_version != PARSER_VERSION:
            parser_stale_repos.append(r.id)
        for e in shard.edges:
            checked += 1
            if not _exists(e.src) or not _exists(e.dst):
                dangling.append({"repo": r.id, "src": e.src,
                                 "relation": e.relation, "dst": e.dst})
    return {"repos": len(repos), "checked": checked,
            "stale": len(stale_repos), "dangling": len(dangling),
            "parser_stale": len(parser_stale_repos),
            "empty": len(empty_repos), "unreadable": len(unreadable_repos),
            "stale_repos": stale_repos,
            "empty_repos": empty_repos,
            "unreadable_repos": unreadable_repos,
            "parser_stale_repos": parser_stale_repos,
            "dangling_sample": dangling[:20]}


def cmd_lint(args) -> int:
    """Graph-health checks: stale repos (HEAD moved), repos built by an older
    parser, and dangling edges.

    Exit code deliberately unchanged by the parser-staleness report: it is still
    0 exactly when there are no dangling edges and no HEAD-stale repos. A repo
    indexed by an older parser holds an internally consistent graph of a real
    commit — it is out of date, not broken — and `kb index` rebuilds it on the
    next run without being asked. Folding it into the exit code would turn a
    parser bump into a red CI gate for every pipeline that runs `kb lint`,
    silently, on upgrade. `doctor` is the command that grades it as a fault and
    exits non-zero for it; lint reports the same fact and lets the caller decide.

    Repositories with no commits are the same shape of report and get the same
    treatment: named, explained, and not counted against the exit code. Nothing a
    reader can do clears them, so failing on them would only train people to stop
    reading the exit code.
    """
    as_json = getattr(args, "json", False)
    if as_json:
        from ...logging_setup import use_stderr
        use_stderr()
    store, store_dir = _open_store(args)
    try:
        if not store.list_repos():
            if as_json:
                print(json.dumps({"repos": 0, "checked": 0, "stale": 0, "dangling": 0,
                                  "parser_stale": 0, "empty": 0, "unreadable": 0,
                                  "stale_repos": [], "empty_repos": [],
                                  "unreadable_repos": [],
                                  "parser_stale_repos": [], "dangling_sample": []},
                                 indent=2))
                return 0
            log("Nothing indexed yet — run index first.")
            return 0
        res = lint_result(store, store_dir)
        clean = res["dangling"] == 0 and res["stale"] == 0 and res["unreadable"] == 0
        if as_json:
            print(json.dumps(res, indent=2))
            return 0 if clean else 1
        for rid in res["stale_repos"]:
            log(f"  stale: {rid} (HEAD moved or never finished — re-run index)")
        for rid in res["empty_repos"]:
            log(f"  empty: {rid} (the repository has no commits, so there is nothing "
                f"to index -- this will not clear by re-indexing)")
        for d in res["unreadable_repos"]:
            why = ("its path no longer exists" if d["reason"] == "missing"
                   else "git cannot read a repository there")
            log(f"  unreadable: {d['repo']} ({why}: {d['path'] or 'no path recorded'} -- "
                f"re-clone it, or drop it from the store)")
        for rid in res["parser_stale_repos"]:
            log(f"  parser-stale: {rid} (built by an older parser — `contextlake kb "
                f"index` rebuilds it; not counted in this command's exit code)")
        for d in res["dangling_sample"]:
            log(f"  dangling: {d['repo']}: {d['src']} -{d['relation']}-> {d['dst']}")
        if res["dangling"] > 20:
            log(f"  … and {res['dangling'] - 20} more dangling edge(s)")
        # The glyph tracks the exit code, never the advisory count: a ⚠ over a 0
        # exit reads as a broken command.
        glyph = style.ok() if clean else style.warn()
        parser_note = (f", {res['parser_stale']} built by an older parser"
                       if res["parser_stale"] else "")
        empty_note = f", {res['empty']} empty" if res["empty"] else ""
        unreadable_note = f", {res['unreadable']} unreadable" if res["unreadable"] else ""
        log(f"{glyph} Lint: {res['repos']} repos, {res['checked']} edges checked — "
            f"{res['dangling']} dangling, {res['stale']} stale"
            f"{unreadable_note}{empty_note}{parser_note}")
        return 0 if clean else 1
    finally:
        store.close()
