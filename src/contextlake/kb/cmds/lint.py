"""`contextlake lint` -- structural graph-health checks."""

from __future__ import annotations

import copy
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

_lint_cache: dict[str, tuple] = {}
_LINT_CACHE_MAX = 8   # a handful of stores per process; bounded so a long-lived server cannot grow


def _invalidate_for_shard(_path_key: str) -> None:
    """Drop everything when this process rewrites any shard.

    The stat-based fingerprint already catches a rewrite in almost every case, but
    "almost" is doing real work in that sentence: a rewrite landing in the same
    nanosecond with an identical size would be invisible to it. This process knows
    when it wrote, so it does not have to infer it. Clearing everything rather than
    one entry is right because the fingerprint spans the whole store: a single
    shard's rewrite invalidates the store-wide answer it contributed to, and the
    cache holds at most eight of them.
    """
    _lint_cache.clear()


_invalidator_registered = False


def _ensure_invalidator_registered() -> None:
    """Register once, lazily -- ``store.shards`` imports tree-sitter transitively in
    some paths, and this module is imported by the CLI's help text."""
    global _invalidator_registered
    if _invalidator_registered:
        return
    from ..store.shards import register_shard_invalidator

    register_shard_invalidator(_invalidate_for_shard)
    _invalidator_registered = True


def _lint_cache_key(store_dir) -> str:
    return str(store_dir)


def _store_fingerprint(store, store_dir, resolve_shard):
    """Every repo's shard identity, or ``None`` if the answer must not be cached.

    ``None`` means "do not cache this run", and it is returned whenever anything
    could not be observed -- a repo whose shard cannot be stat'd at all. An
    unobservable input must never be folded into a fingerprint that then looks
    settled: that is how a cache starts serving a confident answer about a store it
    could not actually see. Repos are sorted so the fingerprint does not depend on
    the order the store happened to list them in.
    """
    try:
        repos = store.list_repos()
    except Exception:  # noqa: BLE001 - a health check must not fail on a store read
        return None
    parts = []
    for r in sorted(repos, key=lambda r: r.id):
        identity, _load = resolve_shard(store_dir, r.id)
        if identity is None:
            # No readable shard. That is a legitimate state (a repo indexed from a
            # source, a shard deleted), and the walk below handles it -- but its
            # absence is not something a stat can detect a change in, so the safe
            # answer is to not cache rather than to encode "absent" as if it were
            # a stable observation.
            return None
        parts.append((r.id, identity))
    return tuple(parts)


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
    fault in the store, so it does not count against a clean lint either. Same
    for ``shard`` -- a repo imported from a graph-shard JSON, which never had a
    checkout and so has no history to be behind. ``unreadable`` (the path is gone,
    or git will not answer for a checkout that should be there) stays a fault:
    those repositories really are uncitable.
    """
    from ..parse import PARSER_VERSION  # lazy: tree-sitter, and only for this check
    from ..store.shards import read_shard, resolve_shard

    _ensure_invalidator_registered()

    # Answer from cache when every shard is byte-for-byte the file we last walked.
    # The dashboard calls this on EVERY request and the walk costs a full parse per
    # repo plus a `get_node` per endpoint -- measured at seconds on a large store,
    # paid again on each refresh of a page nobody had changed anything behind.
    #
    # Keyed on the same `(path, mtime_ns, size)` identity the shard cache already
    # uses, for every repo at once: if any shard has been rewritten, the whole
    # answer is recomputed. That is deliberately coarse. A per-repo cache would be
    # cheaper to invalidate and would also let a stale count for repo A sit beside
    # a fresh one for repo B in a single reported total, which is the kind of
    # quietly-mixed number this codebase keeps having to fix.
    #
    # NOT computed from SQLite instead, though the edges are there and it would be
    # faster still: a store was observed during this investigation whose shards had
    # been deleted while `edges` held 0 rows, where a SQL anti-join answers
    # "0 dangling edges" about a graph with no edges left. This cache changes WHEN
    # the walk happens, never WHAT it measures.
    fingerprint = _store_fingerprint(store, store_dir, resolve_shard)
    if fingerprint is not None:
        cached = _lint_cache.get(_lint_cache_key(store_dir))
        if cached is not None and cached[0] == fingerprint:
            return copy.deepcopy(cached[1])

    repos = store.list_repos()
    stale_repos: list[str] = []
    empty_repos: list[str] = []
    shard_repos: list[str] = []
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
            elif state == "shard":
                shard_repos.append(r.id)
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
    result = {"repos": len(repos), "checked": checked,
            "stale": len(stale_repos), "dangling": len(dangling),
            "parser_stale": len(parser_stale_repos),
            "empty": len(empty_repos), "unreadable": len(unreadable_repos),
            "shard": len(shard_repos),
            "stale_repos": stale_repos,
            "empty_repos": empty_repos,
            "shard_repos": shard_repos,
            "unreadable_repos": unreadable_repos,
            "parser_stale_repos": parser_stale_repos,
            "dangling_sample": dangling[:20]}
    if fingerprint is not None:
        # Copied in and out, so a caller that mutates the dict it was handed cannot
        # corrupt the entry the next caller receives.
        _lint_cache[_lint_cache_key(store_dir)] = (fingerprint, copy.deepcopy(result))
        while len(_lint_cache) > _LINT_CACHE_MAX:
            _lint_cache.pop(next(iter(_lint_cache)))
    return result


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
                                  "shard": 0,
                                  "stale_repos": [], "empty_repos": [],
                                  "shard_repos": [], "unreadable_repos": [],
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
        for rid in res["shard_repos"]:
            log(f"  shard-imported: {rid} (indexed from a graph shard, not a "
                f"checkout, so there is no history to compare it against)")
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
        shard_note = f", {res['shard']} shard-imported" if res["shard"] else ""
        unreadable_note = f", {res['unreadable']} unreadable" if res["unreadable"] else ""
        log(f"{glyph} Lint: {res['repos']} repos, {res['checked']} edges checked — "
            f"{res['dangling']} dangling, {res['stale']} stale"
            f"{unreadable_note}{empty_note}{shard_note}{parser_note}")
        return 0 if clean else 1
    finally:
        store.close()
