"""`contextlake kb impact` -- blast-radius / dependents analysis."""

from __future__ import annotations

import json

from ... import style
from ...logging_setup import log
from ._common import (
    _open_store,
)


def cmd_impact(args) -> int:
    """What could break if a node changes — reverse blast radius over the graph."""
    from ..impact import blast_radius, resolve_target

    target = (getattr(args, "args", []) or [None])[0]
    hops = getattr(args, "hops", None) or 3
    limit = getattr(args, "limit", None) or 100
    repo = getattr(args, "repo", None)
    as_json = getattr(args, "json", False)
    if as_json:
        from ...logging_setup import use_stderr
        use_stderr()
    if not target:
        usage = "contextlake kb impact <node-id-or-symbol> [--repo R] [--hops N] [--limit N]"
        if as_json:
            print(json.dumps({"error": "missing_argument", "usage": usage}, indent=2))
        else:
            log(f"usage: {usage}")
        return 2
    store, _ = _open_store(args)
    try:
        node, candidates = resolve_target(store, target, repo=repo)
        if node is None and candidates:        # same name in several repos — disambiguate
            by_repo: dict = {}
            for c in candidates:
                by_repo.setdefault(c.repo, c)
            if as_json:
                print(json.dumps({
                    "error": "ambiguous", "target": target,
                    "candidates": [{"repo": r, "kind": c.kind, "name": c.name}
                                  for r, c in by_repo.items()],
                }, indent=2))
                return 1
            log(f"{style.cyan(target)} is ambiguous — defined in {len(by_repo)} repos. "
                f"Narrow it with --repo:")
            for r, c in list(by_repo.items())[:10]:
                log(f"  --repo {r}   ({c.kind} {c.name})")
            return 1
        if node is None:
            if as_json:
                print(json.dumps({"error": "not_found", "target": target}, indent=2))
                return 1
            log(f"No node matches {target!r} — index first, or try `query`")
            return 1
        hits, truncated = blast_radius(store, node.id, hops=hops, limit=limit)
        if as_json:
            print(json.dumps({
                "target": {"id": node.id, "repo": node.repo, "kind": node.kind,
                          "name": node.name},
                "hops": hops, "truncated": truncated,
                "affected": [{"hop": h.hop, "repo": h.repo, "kind": h.kind, "name": h.name,
                             "via": h.via, "confidence": h.confidence.lower()}
                            for h in hits],
            }, indent=2))
            return 0
        head = f"Impact of changing {style.cyan(node.name)} ({node.id})"
        if not hits:
            log(f"{head}: nothing depends on it within {hops} hop(s)")
            return 0
        log(f"{head}: {len(hits)} affected node(s) within {hops} hop(s)"
            + (style.dim(" (truncated)") if truncated else ""))
        for h in hits:
            log(f"  h{h.hop}  {h.repo}:{h.name}  ({h.kind}, via {h.via}, "
                f"{h.confidence.lower()})")
        return 0
    finally:
        store.close()
