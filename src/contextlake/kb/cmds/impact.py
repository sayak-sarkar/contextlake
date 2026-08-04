"""`contextlake kb impact` -- blast-radius / dependents analysis."""

from __future__ import annotations

import json

from ... import style
from ...logging_setup import log
from ._common import (
    _open_store,
)


def _loc(file, line) -> str:
    """`file:line`, or the file alone, or `?` -- never a fabricated position."""
    if file and line:
        return f"{file}:{line}"
    return file or "?"


def cmd_impact(args) -> int:
    """What could break if a node changes — reverse blast radius over the graph."""
    from ..impact import blast_radius, chosen_one_of, other_definitions, resolve_target

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
        # Which of several same-named definitions this answer is about. The seed
        # used to be picked silently, so a result about a JavaScript close() read
        # exactly like one about the Python close() the user meant.
        others = other_definitions(store, node, repo=repo)
        hits, truncated = blast_radius(store, node.id, hops=hops, limit=limit)
        if as_json:
            print(json.dumps({
                "target": {"id": node.id, "repo": node.repo, "kind": node.kind,
                          "name": node.name, "file": node.file, "line": node.line_start},
                "ambiguous": bool(others),
                "other_definitions": [
                    {"id": c.id, "repo": c.repo, "kind": c.kind, "name": c.name,
                     "file": c.file, "line": c.line_start} for c in others[:10]],
                "hops": hops, "truncated": truncated,
                # id/file/line make two hits with the same name distinguishable, and
                # via_file/via_line cite the call site the edge was read from.
                "affected": [{"hop": h.hop, "id": h.id, "repo": h.repo, "kind": h.kind,
                             "name": h.name, "file": h.file, "line": h.line,
                             "via": h.via, "via_file": h.via_file, "via_line": h.via_line,
                             "confidence": h.confidence.lower(),
                             "name_candidates": h.name_candidates}
                            for h in hits],
            }, indent=2))
            return 0
        head = f"Impact of changing {style.cyan(node.name)} ({node.id})"
        if not hits:
            log(f"{head}: nothing depends on it within {hops} hop(s)")
        else:
            log(f"{head}: {len(hits)} affected node(s) within {hops} hop(s)"
                + (style.dim(" (truncated)") if truncated else ""))
        log(f"  seed: {node.kind} {_loc(node.file, node.line_start)}"
            + chosen_one_of(node.name, len(others) + 1))
        for c in others[:5]:
            log(style.dim(f"        or: {c.kind} {_loc(c.file, c.line_start)}  --node {c.id}"))
        if len(others) > 5:
            log(style.dim(f"        ... and {len(others) - 5} more named {node.name!r}"))
        if not hits:
            return 0
        for h in hits:
            conf = h.confidence.lower()
            if h.name_candidates:
                conf += f", 1 of {h.name_candidates} same-name definitions"
            log(f"  h{h.hop}  {h.repo}:{h.name}  ({h.kind}, {_loc(h.file, h.line)})"
                f"  via {h.via} at {_loc(h.via_file, h.via_line)}  [{conf}]")
        # What "ambiguous" costs, in this result: a count, not an adjective. The
        # label alone read identically on a hand-verified 11/11 answer and on one
        # with 282 false positives.
        by_name = sum(1 for h in hits if h.confidence == "AMBIGUOUS")
        if by_name:
            log(style.dim(
                f"  {by_name} of {len(hits)} hit(s) came from a reference matched by NAME "
                f"across several same-named definitions; each caller may target a "
                f"different one. Open the cited call site to confirm."))
        return 0
    finally:
        store.close()
