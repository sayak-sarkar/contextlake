"""`contextlake query` -- full-text + as-of search over the graph."""

from __future__ import annotations

import json

from ... import style
from ...logging_setup import log
from ..config import load_kb_config
from ._common import (
    _open_store,
)


def _print_hit(n) -> None:
    loc = f"{n.file}:{n.line_start}" if n.file and n.line_start else (n.file or "?")
    log(f"  {style.cyan(n.repo)} · {loc} · {n.kind} · {style.bold(n.name)}")


def _hit_json(n) -> dict:
    return {"repo": n.repo, "file": n.file, "line": n.line_start, "kind": n.kind,
            "name": n.name, "qualified_name": n.qualified_name}


def _query_as_of(args, commit: str, *, as_json: bool = False) -> int:
    """Search a repo's snapshot at an indexed commit (bi-temporal 'as of')."""
    from ..store.shards import read_shard_at

    repo = getattr(args, "repo", None)
    if not repo:
        log("--as-of requires --repo (history is per-repo)")
        return 2
    text = " ".join(getattr(args, "args", []) or []).strip().lower()
    store_dir = load_kb_config(getattr(args, "config", None)).store_path
    shard = read_shard_at(store_dir, repo, commit)
    if shard is None:
        if as_json:
            print(json.dumps({"error": "no_snapshot", "repo": repo, "commit": commit}, indent=2))
            return 1
        log(f"No indexed snapshot of {repo!r} at commit {commit!r}")
        return 1
    kind = getattr(args, "kind", None)
    hits = [
        n for n in shard.nodes
        if (text in n.name.lower()
            or (n.qualified_name and text in n.qualified_name.lower()))
        and (kind is None or n.kind == kind)
    ][:getattr(args, "limit", None) or 20]
    if as_json:
        print(json.dumps([_hit_json(n) for n in hits], indent=2))
        return 0
    if not hits:
        log(f"No matches for {text!r} in {repo} as of {commit}")
        return 0
    for n in hits:
        _print_hit(n)
    return 0


_QUERY_USAGE = 'contextlake query "<text>" [--kind K] [--repo R] [--limit N] [--as-of C]'


def cmd_query(args) -> int:
    text = " ".join(getattr(args, "args", []) or []).strip()
    as_json = getattr(args, "json", False)
    if as_json:
        from ...logging_setup import use_stderr
        use_stderr()
    if not text:
        if as_json:
            print(json.dumps({"error": "missing_argument", "usage": _QUERY_USAGE}, indent=2))
        else:
            log(f"usage: {_QUERY_USAGE}")
        return 2
    as_of = getattr(args, "as_of", None)
    if as_of:
        return _query_as_of(args, as_of, as_json=as_json)
    store, _ = _open_store(args)
    try:
        results = store.search(
            text, kind=getattr(args, "kind", None), repo=getattr(args, "repo", None),
            limit=getattr(args, "limit", None) or 20,
        )
        if as_json:
            print(json.dumps([_hit_json(n) for n in results], indent=2))
            return 0
        if not results:
            log(f"No matches for {text!r}")
            # A multi-word phrase reads as a natural-language question; query is
            # keyword (FTS) search, so point at the semantic path instead of a
            # bare dead-end. A single-token lookup (a symbol) stays quiet.
            if len(text.split()) > 1:
                log("  (query is keyword search; for natural-language search run "
                    "`contextlake embed`, then use serve's semantic_search / ask tools)")
            return 0
        for n in results:
            _print_hit(n)
        return 0
    finally:
        store.close()

