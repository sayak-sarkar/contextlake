"""Generate a curated wiki page for a repo from its knowledge graph.

A page is grounded strictly in facts extracted from the repo's shard (top symbols
by degree, kinds, languages, packages, files) so the model summarizes rather than
invents. Every page ends with a provenance footer citing the commit and sources.
"""

from __future__ import annotations

import os
import re
import threading
from collections import Counter, OrderedDict
from datetime import date
from pathlib import Path

from ..model import PER_SITE_RELATIONS
from ..security import UNTRUSTED_DATA_RULE, untrusted_block
from ..store.shards import (
    read_shard,
    register_shard_invalidator,
    resolve_shard,
)

# Conventional entry-point/config filenames -- presence-only signal for the
# "Setup & Run" section (never file contents beyond the README excerpt below,
# so there's no hallucination surface: the model is told these files exist,
# not what's in them).
_SETUP_FILENAMES = {
    "package.json", "pyproject.toml", "dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "manage.py", "main.py", "__main__.py",
    "program.cs", "makefile",
}

# Legacy C/C++ build-tooling file extensions: too numerous in a large legacy
# repo to list individually (hundreds of .vcxproj/.dsp files would drown the
# prompt), so _setup_signals reports these as a summarized per-category count
# rather than per-file entries like the presence-only allowlist above.
_LEGACY_BUILD_CATEGORIES = {
    ".vcxproj": "modern MSBuild (.vcxproj)",
    ".vcproj": "older MSBuild (.vcproj)",
    ".dsp": "legacy MSVC6 project (.dsp)",
    ".dsw": "legacy MSVC6 workspace (.dsw)",
    ".pbxproj": "Xcode project (.pbxproj)",
    ".cdtproject": "Eclipse CDT project",
}

# None of the extensions above are parsed into graph nodes (they aren't source
# code), so counting them purely from `all_files` would always yield zero on a
# real repo -- the live-checkout walk below is what actually finds them. Cap on
# how many filenames (not directories) that walk will examine before giving up,
# so an uncached, per-request call (only the dashboard's repo-detail panel --
# the MCP `get_repo_brief` tool calls `repo_brief` without `store=`, so it can
# never trigger this walk) can't turn into an unbounded walk over a huge legacy
# monorepo; generous enough that no real repo's legacy-project-file count is
# ever truncated by it.
_LEGACY_BUILD_WALK_FILE_LIMIT = 200_000

def _grounding_cap(node_count: int) -> int:
    """How many symbols to sample into the wiki prompt's ranked lists.

    Grows with repo size (a fixed 15 is ~0.03% of a 50k-node repo) but stays
    bounded -- more symbols means a longer, more expensive LLM call, so this is
    a deliberate depth-vs-cost tradeoff, not a free win.
    """
    return max(15, min(80, node_count // 1500))


SYSTEM = (
    "You are a precise staff engineer writing a short wiki page about a code "
    "repository for other engineers. Use ONLY the facts provided. Do not invent "
    "APIs, files, or behavior; if a fact is not given, omit it. Be concise. "
    "External context from connected sources must always be attributed to its "
    "source when used; never present it as a fact about the code."
)


def external_context(
    store_dir, repo_id: str, *, max_items: int = 8, max_chars: int = 300
) -> list[dict]:
    """Cited snippets from ``repo_id``'s connector-enrichment documents (the
    ``@enrich:<repo_id>`` partition), or ``[]`` if it hasn't been enriched.

    Each item carries its source (issue tracker, docs, design tool, …), title,
    and uri so the wiki prompt can attribute it rather than presenting it as a
    fact about the code.
    """
    from ..connectors.enrich import enrich_partition

    shard = read_shard(store_dir, enrich_partition(repo_id))
    if shard is None:
        return []
    items = []
    for n in shard.nodes:
        if n.kind != "document":
            continue
        snippet = " ".join(((n.attrs or {}).get("snippet") or "").split())[:max_chars]
        title = (n.name or "").strip()
        uri = (n.file or "").strip()
        if not snippet and not title:
            continue
        items.append({
            "source": (n.attrs or {}).get("source"),
            "title": title,
            "uri": uri,
            "snippet": snippet,
        })
        if len(items) >= max_items:
            break
    return items


def _symbol_row(n, *, count: int | None = None) -> dict:
    row = {"kind": n.kind, "name": n.name, "file": n.file,
           "doc": (n.attrs or {}).get("doc"),
           "signature": (n.attrs or {}).get("signature")}
    if count is not None:
        row["count"] = count
    return row


def _is_setup_filename(base: str) -> bool:
    base = base.lower()
    return base in _SETUP_FILENAMES or base.startswith("readme") or base.endswith(".csproj")


def _setup_signals_from_shard(all_files: set) -> tuple[list[str], dict[str, int], list[str]]:
    """The half of :func:`_setup_signals` that depends only on the shard.

    Split out so ``repo_brief`` can cache it beside the rest of its shard-derived
    core and stop parsing a whole graph just to recompute it. Deliberately *not*
    the whole function: the other half reads the live checkout, and freezing that
    would reintroduce exactly the staleness the live read exists to avoid.

    Returns ``(found, by_ext, counted)`` -- all three bounded in practice. ``found``
    is a set of bare filenames; ``by_ext`` and ``counted`` cover only legacy
    build-tooling extensions, which are never parsed into graph nodes, so on a real
    repo they are empty and exist to make the live walk's dedup correct rather than
    to contribute counts.
    """
    found = sorted({f for f in all_files if _is_setup_filename(f.rsplit("/", 1)[-1])})
    by_ext: dict[str, int] = {}
    counted: list[str] = []
    for f in all_files:
        ext = "." + f.rsplit(".", 1)[-1].lower() if "." in f else ""
        if ext in _LEGACY_BUILD_CATEGORIES:
            by_ext[ext] = by_ext.get(ext, 0) + 1
            counted.append(f)
    return found, by_ext, sorted(counted)


def _setup_signals(all_files: set, store=None, repo_id: str | None = None,
                   *, from_shard=None) -> list[str]:
    """Which conventional entry-point/config filenames exist.

    Two sources, merged: (1) every file already in the shard (not the
    truncated 20-file sample below) -- catches source-tree entry points like
    ``main.py``/``manage.py``/``Program.cs``, which tree-sitter indexes like
    any other source file; (2) a top-level-only listing of the live checkout,
    when ``store`` is given -- catches config/manifest files (``package.json``,
    ``Dockerfile``, ``pyproject.toml``, ...) that aren't source code and so
    never become graph nodes at all. Same store-optional, degrade-to-nothing
    guard as :func:`_readme_excerpt` -- omit ``store`` and you just get (1).

    A third, distinct kind of entry is appended: legacy C/C++ build-tooling
    files (``.vcxproj``, ``.dsp``, ``.pbxproj``, ...) are summarized as a
    per-category count string rather than listed per-file -- a large legacy
    repo can carry hundreds of these, which would drown the presence-only
    listing above if treated the same way. None of these extensions are ever
    parsed into graph nodes (they aren't source code), so counting them from
    ``all_files`` alone would always be zero on a real repo -- the counts come
    from a recursive, bounded walk of the live checkout (same ``store``-given
    guard as (2) above), merged with any matches already in ``all_files``
    without double-counting a file present in both.
    """
    # ``from_shard`` lets a caller pass the pre-computed shard half (see
    # _setup_signals_from_shard) so this function never needs the full file set --
    # which is what lets repo_brief answer from cache without parsing the shard.
    if from_shard is None:
        from_shard = _setup_signals_from_shard(all_files)
    shard_found, shard_by_ext, shard_counted = from_shard
    found = set(shard_found)
    base = None
    if store is not None and repo_id is not None:
        r = store.get_repo(repo_id)
        base = Path(r.path) if r and getattr(r, "path", None) else None
        if base and base.is_dir():
            for entry in base.iterdir():
                if entry.is_file() and _is_setup_filename(entry.name):
                    found.add(entry.name)
    by_ext: dict[str, int] = dict(shard_by_ext)
    counted: set[str] = set(shard_counted)  # relative paths already counted, for merge dedup
    if base is not None and base.is_dir():
        # Reuse the parser's own skip-dir set (never duplicate it here); imported
        # lazily, same as `_is_generated_name` below, so this file's module-level
        # imports stay independent of parse.py's tree-sitter dependency.
        from ..parse import _SKIP_DIRS

        n_visited = 0
        stop = False
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                n_visited += 1
                ext = os.path.splitext(fn)[1].lower()
                if ext in _LEGACY_BUILD_CATEGORIES:
                    rel = os.path.relpath(os.path.join(dirpath, fn), base).replace(os.sep, "/")
                    if rel not in counted:
                        by_ext[ext] = by_ext.get(ext, 0) + 1
                        counted.add(rel)
                if n_visited >= _LEGACY_BUILD_WALK_FILE_LIMIT:
                    stop = True
                    break
            if stop:
                break
    summary = [f"{n} {_LEGACY_BUILD_CATEGORIES[ext]} file(s) detected"
              for ext, n in sorted(by_ext.items())]
    return sorted(found)[:20] + summary


def _readme_excerpt(store, repo_id: str, *, max_chars: int = 2000) -> str | None:
    """First ``max_chars`` of the repo's own README, read from its live checkout.

    Degrades to ``None`` (never raises) when ``store`` is omitted or the
    checkout is missing/moved -- mirrors ``dashboard.data._readme_html``'s
    guard for the exact same reason.
    """
    if store is None:
        return None
    r = store.get_repo(repo_id)
    base = Path(r.path) if r and getattr(r, "path", None) else None
    if not base or not base.is_dir():
        return None
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        f = base / name
        if f.is_file():
            return f.read_text(encoding="utf-8", errors="replace")[:max_chars]
    return None


# Share of a ranked list the per-kind floors may claim between them. Two, matching
# `visualize/payload.py`'s constant of the same name and for the same reason: a floor
# that can grow without bound stops being a floor and becomes the ranking.
_KIND_FLOOR_SHARE = 2


def _ranked_with_kind_floor(
    candidates: list[tuple[str, int]], by_id: dict, cap: int,
) -> list[str]:
    """``candidates`` (node_id, count) sorted by count desc -> up to ``cap`` ids,
    reserving at least one slot per distinct kind present so a kind with
    structurally low degree (e.g. a SQL table, which doesn't call anything)
    always gets some representation instead of being silently squeezed out by
    a pure degree-rank cutoff.

    ``candidates`` must already include every id you want considered for a
    floor slot -- a kind absent from ``candidates`` entirely (e.g. because the
    caller only passed nodes with nonzero degree) gets no floor slot, since
    there's nothing here to represent it. Callers that want a zero-degree
    kind included must include it as a (id, 0) candidate themselves.

    One exception to "every distinct kind gets a slot": a file-less
    ``kind="module"`` node is not a symbol the repo defines, it is the
    *target* of an import/``#include`` (``parse.py`` emits one per imported
    module name, with no file of its own). Guaranteeing those a slot put a row
    like "module widget.h (?), 2 caller(s)" in every C/C++ whole-repo page's
    ranked lists -- the floor exists to keep a real but structurally
    low-degree kind (a SQL table) visible, not to promote a pseudo-node. They
    stay eligible by ordinary degree ranking below, just not guaranteed.
    Narrow on purpose: other file-less kinds (``package``, ``endpoint``,
    ``topic``) are real, nameable things and keep their floor slot.

    **The floors are bounded, and that bound is the whole point of this function's
    second revision.** They used to take one slot per kind with no ceiling, which was
    fine while a repo held a handful of kinds and became the dominant behaviour once
    it held many. Measured after the five C/C++ symbol kinds started being emitted:
    on a 12-kind repository with ``cap`` 15, **11 of 15 rows** were per-kind slots and
    only 4 came from degree ranking; constructed at 17 kinds, **14 of 15**, so the
    ranking these lists exist to present had effectively stopped running.

    So floors may claim at most ``cap // _KIND_FLOOR_SHARE`` of the list, and they are
    spent only on kinds the honest degree ranking left out entirely -- a kind already
    represented in the top ``cap`` needs no slot. The same bounded-share rule
    ``visualize/payload.py`` has always used; this is that precedent applied here,
    not a new idea.
    """
    by_kind: dict[str, list[str]] = {}
    order: list[str] = []
    for nid, _count in candidates:
        node = by_id.get(nid)
        if node is None:
            continue
        if node.kind != "module" or node.file:
            by_kind.setdefault(node.kind, []).append(nid)
        order.append(nid)

    # The honest answer first: the top `cap` purely by the count the caller ranked on.
    head = order[:cap]
    represented = {by_id[nid].kind for nid in head if nid in by_id}

    # Then, bounded, the kinds that answer left out. Reserving a slot for a kind that
    # already made the cut buys nothing and costs a real row.
    missing = [ids[0] for kind, ids in by_kind.items() if kind not in represented]
    budget = max(1, cap // _KIND_FLOOR_SHARE) if cap else 0
    floor_ids = missing[:budget]

    # Evict from the tail, which is the lowest-ranked end of the head by construction.
    keep = head[:max(0, cap - len(floor_ids))]
    seen: set[str] = set()
    out: list[str] = []
    for nid in keep + floor_ids:
        if nid not in seen:
            seen.add(nid)
            out.append(nid)
    return out[:cap]


# In-memory cache of repo_brief's shard-derived aggregation (top_symbols/hubs/
# dispatchers/kinds/langs/etc.) -- the pure-Python Counter/sorted/
# _ranked_with_kind_floor work below, which is the other size-scaling cost of
# a repo-detail request alongside the shard JSON parse (see the cache in
# ``store.shards``). It is a pure function of (shard content, path_prefix), so
# it's cached separately from -- and does NOT cover -- ``readme_excerpt`` /
# ``setup_signals``, which read the live checkout and can change without the
# shard itself changing (e.g. editing the README without re-indexing); those
# stay computed fresh on every call. Keyed on the shard file's own
# (mtime_ns, size), so a re-index (which rewrites the shard) invalidates it
# the same way it invalidates the shard-parse cache -- plus the same explicit
# same-process invalidation ``write_shard`` performs (see
# ``_invalidate_for_shard`` below), because that identity alone is not enough
# for a rewrite this process just made.
#
# Deliberately excludes the repo's full ``all_files`` set: every other field
# here is capped (<=80 symbols, <=20 files/packages/decisions), but the raw
# file set is not, so a huge repo would make each cached entry itself large --
# unlike the shard cache in ``store.shards``, this one has no byte budget, so
# an unbounded field would defeat the point of bounding it by entry count.
# ``repo_brief`` recomputes it fresh via ``_scoped_nodes_edges`` instead (a
# cheap O(n) pass, unlike the aggregation actually being cached here).
#
# NOTE for future edits: every list/dict value below is shared, read-only,
# across every caller that hits the cache -- never mutate a returned field
# in place (e.g. ``brief["kinds"][...] = ...``); copy it first.
_CORE_CACHE_MAX = 256
_core_cache: OrderedDict[tuple, dict] = OrderedDict()
_core_cache_lock = threading.Lock()


def _invalidate_for_shard(path_key: str) -> None:
    """Drop every cached aggregation derived from the shard file ``path_key``
    (one entry per ``path_prefix`` this process has briefed that repo under).

    Registered with ``store.shards`` so a shard THIS process rewrites clears
    this cache as well, not just the shard-parse cache. The (mtime_ns, size)
    key alone can miss such a rewrite -- a re-index at an unchanged commit can
    produce a same-length file within one filesystem mtime tick -- and missing
    it here is worse than missing it there: the shard would be re-read fresh
    while this cache still answered from the previous one, putting a stale
    ``node_count``/``top_symbols`` under a fresh ``head`` in the same brief.
    """
    with _core_cache_lock:
        for key in [k for k in _core_cache if k[0] == path_key]:
            del _core_cache[key]


register_shard_invalidator(_invalidate_for_shard)


def _scoped_nodes_edges(shard, path_prefix: str | None) -> tuple[list, list]:
    """``(nodes, edges)`` scoped to ``path_prefix`` (segment-boundary match --
    see ``repo_brief``'s docstring). Shared by the cached core aggregation
    below and by ``repo_brief``'s own, deliberately-uncached recomputation of
    ``all_files`` (kept out of the cached core dict -- see ``_core_cache``'s
    docstring -- so this one small filtering pass runs twice per call; cheap,
    unlike the aggregation it's shared with)."""
    nodes = shard.nodes
    if path_prefix:
        prefix_dir = path_prefix.rstrip("/") + "/"
        nodes = [
            n for n in nodes
            if n.file and (n.file == path_prefix or n.file.startswith(prefix_dir))
        ]
        node_ids = {n.id for n in nodes}
        edges = [e for e in shard.edges if e.src in node_ids and e.dst in node_ids]
    else:
        edges = shard.edges
    return nodes, edges


def _repo_brief_core_uncached(shard, path_prefix: str | None) -> dict:
    nodes, edges = _scoped_nodes_edges(shard, path_prefix)
    # Carried in the cache entry so a HIT needs nothing from the shard at all.
    # These three were the last reason repo_brief had to parse a whole graph even
    # when its answer was already cached: two short strings and a bounded summary.
    # They are derived from THIS parse, so they stay consistent with the counts
    # beside them by construction -- the same one-observation property
    # `read_shard_with_identity` protects, kept rather than weakened.
    _head = shard.head_commit
    _parser_version = shard.parser_version
    _setup_from_shard = _setup_signals_from_shard({n.file for n in nodes if n.file})
    by_id = {n.id: n for n in nodes}
    degree: Counter = Counter()
    in_degree: Counter = Counter()   # callers -- a hub, worth protecting with tests
    out_degree: Counter = Counter()  # callees -- a dispatcher, where behavior branches
    # A per-site relation stores one edge per occurrence in source, so counting rows
    # would make a function called fifty times from one caller look like fifty callers,
    # and that number is rendered beside the row as "N caller(s)". Count each distinct
    # pair once for those relations only -- which is exactly the historical number,
    # since before per-site retention there was one row per pair anyway. Other
    # relations keep counting rows: `contains` legitimately repeats a pair (one edge
    # per declaration site of a merged symbol) and de-duplicating it here would be a
    # silent behavior change nobody asked for.
    seen_pairs: set[tuple[str, str, str]] = set()
    for e in edges:
        if e.relation in PER_SITE_RELATIONS:
            pair = (e.relation, e.src, e.dst)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
        degree[e.src] += 1
        degree[e.dst] += 1
        in_degree[e.dst] += 1
        out_degree[e.src] += 1
    cap = _grounding_cap(len(nodes))
    # Candidates for top_symbols cover every node (defaulting to 0 degree), not
    # just the ones with an edge, so a kind that never appears in `degree` at
    # all (e.g. a SQL table, which doesn't call anything) still gets a floor
    # slot -- top_symbols carries no "count", so a 0-degree row here is not a
    # fabricated claim, just an honestly-listed symbol.
    all_node_candidates = sorted(((n.id, degree[n.id]) for n in nodes), key=lambda x: -x[1])
    top_ids = _ranked_with_kind_floor(all_node_candidates, by_id, cap)
    top = [by_id[i] for i in top_ids]
    # hubs/dispatchers keep counts, so their floor only reorders real
    # candidates (nodes that actually have the relevant degree) -- it must
    # never manufacture a "0 caller(s)" row for a kind with no real signal.
    hub_ids = _ranked_with_kind_floor(in_degree.most_common(), by_id, cap)
    dispatcher_ids = _ranked_with_kind_floor(out_degree.most_common(), by_id, cap)
    all_files = {n.file for n in nodes if n.file}
    # Distinct symbols the model actually saw a grounding fact for, across all
    # three lists combined -- a set union, since a node can legitimately appear
    # in more than one list. Used to state the coverage ratio in the footer.
    #
    # Both halves of that ratio count file-backed nodes only. A module-scoped
    # brief can structurally contain nothing else (its filter is on `file`),
    # so counting file-less nodes -- import targets, packages, endpoints,
    # topics -- on the whole-repo side alone made the SAME grounding depth
    # read as systematically worse on a repo's overview page than on its own
    # subsystem pages, for a reason that has nothing to do with coverage.
    file_backed = {n.id for n in nodes if n.file}
    grounded_ids = (set(top_ids) | set(hub_ids) | set(dispatcher_ids)) & file_backed
    # Reuse the parser's own generated-file detection (never duplicate it here)
    # so the wiki prompt can warn the model off treating machine-emitted files
    # as hand-authored design -- by path segment (a "generated/" directory) or
    # by filename convention (e.g. ``*.designer.cs``), same signal the indexer
    # itself uses to decide what to skip.
    from ..parse import _is_generated_name

    generated_paths_detected = any(
        "generated" in f.lower().split("/") or _is_generated_name(f.rsplit("/", 1)[-1])
        for f in all_files
    )
    return {
        "head": _head,
        "parser_version": _parser_version,
        "setup_from_shard": _setup_from_shard,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "grounded_count": len(grounded_ids),
        "coverage_total": len(file_backed),
        "kinds": dict(Counter(n.kind for n in nodes)),
        "langs": dict(Counter(n.lang for n in nodes if n.lang)),
        "top_symbols": [_symbol_row(n) for n in top],
        # Split combined-degree ranking above into fan-in/fan-out separately --
        # the dashboard's own risk view (Anatomy tab's hotspots section), not
        # folded into top_symbols so existing consumers of that field are
        # unaffected.
        "hubs": [_symbol_row(by_id[i], count=in_degree[i]) for i in hub_ids],
        "dispatchers": [_symbol_row(by_id[i], count=out_degree[i]) for i in dispatcher_ids],
        "packages": [n.name for n in nodes if n.kind == "package"][:20],
        "files": sorted(all_files)[:20],
        "decisions": [{"title": n.name, "file": n.file,
                       "doc": (n.attrs or {}).get("doc")}
                      for n in nodes if n.kind == "adr"][:20],
        "generated_paths_detected": generated_paths_detected,
    }


def _repo_brief_core_cached(identity, path_prefix: str | None = None):
    """The cached core for ``identity``, or ``None`` if nothing is cached.

    Exists so a caller can ask "do I already have this?" **before** paying to parse
    the shard. Reading and writing go through the same lock and the same key shape
    as :func:`_repo_brief_core`; this is a lookup, never a computation.
    """
    if identity is None:
        return None
    key = (*identity, path_prefix)
    with _core_cache_lock:
        cached = _core_cache.get(key)
        if cached is not None:
            _core_cache.move_to_end(key)
        return cached


def _repo_brief_core(
    identity: tuple[str, int, int] | None, shard, path_prefix: str | None,
) -> dict:
    """``_repo_brief_core_uncached`` memoized by ``identity`` (the shard file's
    own ``(path, mtime_ns, size)``, as returned by
    ``read_shard_with_identity`` alongside ``shard`` itself -- NOT re-derived
    here via a second ``stat()`` call, which would race a concurrent rewrite
    between the two observations; see that function's docstring) plus
    ``path_prefix``. Falls back to computing uncached (never raises, never
    serves a wrong result) when ``identity`` is ``None`` (the shard couldn't be
    stat'd at all)."""
    key = (*identity, path_prefix) if identity is not None else None
    if key is not None:
        with _core_cache_lock:
            cached = _core_cache.get(key)
            if cached is not None:
                _core_cache.move_to_end(key)
                return cached
    core = _repo_brief_core_uncached(shard, path_prefix)
    if key is not None:
        with _core_cache_lock:
            _core_cache[key] = core
            _core_cache.move_to_end(key)
            while len(_core_cache) > _CORE_CACHE_MAX:
                _core_cache.popitem(last=False)
    return core


def repo_brief(
    store_dir, repo_id: str, *, store=None, path_prefix: str | None = None,
    subsystem_modules: list[dict] | None = None,
) -> dict | None:
    """Salient, grounded facts about a repo, or None if it has no shard.

    ``store`` is optional and enables two live-checkout filesystem reads that
    everything else here (which comes from the indexed shard alone) doesn't
    need: the ``readme_excerpt`` field, and ``setup_signals``' recursive,
    bounded scan for legacy build-tooling files. Omit ``store`` and both
    degrade to their shard-only behavior (``readme_excerpt`` becomes ``None``;
    ``setup_signals`` skips the live-checkout scan).

    ``path_prefix``, when given, scopes the brief to nodes whose ``file``
    is exactly ``path_prefix`` or starts with ``path_prefix`` plus a ``/``
    separator (a segment-boundary match, not a bare string prefix -- so a
    module named ``api`` does NOT also match sibling files under ``apiv2/``,
    ``core`` does not match ``core_utils/``, etc.) so a caller can generate
    one wiki page per module/subsystem instead of one page summarizing an
    entire repo. Edges are scoped alongside the nodes (kept only when both
    endpoints survive the filter) so degree counts, hubs, and dispatchers
    reflect the same slice.

    Three fields need care from a caller building per-module pages, because
    each is gated by a *different* mechanism -- do not assume passing
    ``path_prefix`` alone scopes all of them:

    - ``external`` (built by :func:`external_context`) IS gated directly on
      ``path_prefix`` **inside this function**: it returns the full
      repo-wide enrichment set when ``path_prefix`` is ``None``, and an empty
      list whenever ``path_prefix`` is given -- regardless of ``store`` --
      since those documents have no file/path concept to scope by and would
      otherwise leak whole-repo enrichment facts into every module page.
    - ``readme_excerpt`` and ``setup_signals``' live-checkout scan (the
      recursive legacy-build-tooling walk and the top-level config-file
      listing) are gated on ``store`` alone, NOT on ``path_prefix`` -- this
      function has no logic that scopes them by prefix. When ``store`` is
      given, both always read from the repo root, regardless of
      ``path_prefix``, so a module-scoped call that still passes a real
      ``store`` gets the *whole repo's* README/setup signals presented as if
      they described just that module. A caller building per-module pages
      MUST pass ``store=None`` whenever ``path_prefix`` is set to avoid this
      (``cmd_wiki`` does exactly that). Only the shard-derived half of
      ``setup_signals``, built from the already-scoped ``all_files``,
      actually scopes by ``path_prefix`` on its own.

    ``subsystem_modules``, when given, is threaded straight into the returned
    dict (see below) so ``render_prompt`` can tell the model this repo is
    federated into named subsystems, each with its own dedicated wiki page.
    It is meaningful only for the whole-repo brief (``path_prefix=None``) --
    a module/subsystem page describing one slice of a repo doesn't itself
    have "subsystems", so callers building per-module pages should leave this
    ``None`` (``cmd_wiki`` does exactly that).
    """
    # Resolve and stat the shard, but DO NOT parse it yet. On a warm cache this
    # request needs nothing from the file's contents, and parsing it anyway was the
    # whole cost of a repo-detail page: measured at 4.2s warm on a 131,603-node
    # graph, where the aggregation the cache already held was 3.7s of it and the
    # parse the rest. The file is still observed exactly once per call, which is
    # what `resolve_shard` preserves and what tests/kb/test_kb_wiki.py pins.
    identity, load = resolve_shard(store_dir, repo_id)
    core = _repo_brief_core_cached(identity, path_prefix)
    if core is None:
        shard, identity = load()
        if shard is None:
            return None
        core = _repo_brief_core(identity, shard, path_prefix)
    return {
        "repo": repo_id,
        "head": core["head"],
        # From the shard, which is the source of truth for what built this graph --
        # not from the running PARSER_VERSION, which would stamp a page with the
        # version that rendered it rather than the one that extracted its facts.
        # Carried through the cache entry so a hit still answers from the parse
        # that produced the counts beside it.
        "parser_version": core["parser_version"],
        "node_count": core["node_count"],
        "edge_count": core["edge_count"],
        "grounded_count": core["grounded_count"],
        "coverage_total": core["coverage_total"],
        "kinds": core["kinds"],
        "langs": core["langs"],
        "top_symbols": core["top_symbols"],
        # Split combined-degree ranking above into fan-in/fan-out separately --
        # the dashboard's own risk view (Anatomy tab's hotspots section), not
        # folded into top_symbols so existing consumers of that field are
        # unaffected.
        "hubs": core["hubs"],
        "dispatchers": core["dispatchers"],
        "packages": core["packages"],
        "files": core["files"],
        "decisions": core["decisions"],
        "external": [] if path_prefix else external_context(store_dir, repo_id),
        "readme_excerpt": _readme_excerpt(store, repo_id),
        # The live-checkout half still runs every call; only the shard-derived half
        # comes from the cache. Freezing the live scan would reintroduce exactly the
        # staleness that scan exists to catch.
        "setup_signals": _setup_signals(
            (), store, repo_id, from_shard=core["setup_from_shard"]),
        "generated_paths_detected": core["generated_paths_detected"],
        "subsystem_modules": subsystem_modules or [],
    }


# The prompt's *directive* prose, split out from `render_prompt` so it has one
# home. The draft validator (see `.validate`) matches a generated page against
# these strings to catch a model that echoed its instructions instead of
# following them, so a reworded instruction keeps being detected without anyone
# remembering to update a second copy of it. Two rules for anything added here:
# only directive prose belongs (never a label line like "Recorded decisions
# (...)", which a page may legitimately quote), and never the interpolated facts
# themselves -- a repo/module/section name is exactly what a good page repeats,
# so each constant holds only the invariant tail, with `render_prompt` supplying
# the fact-bearing lead-in around it.
_SCOPE_INSTRUCTION = (
    "Every fact below (symbols, dependencies, files) was drawn exclusively "
    "from that module, not the whole repository. Write about this module "
    "only -- do not make claims about the repository as a whole, and do not "
    "write as if this module IS the whole repository. The relation count "
    "below excludes any relation to a symbol outside this module -- do not "
    "treat a low count as evidence this module has few dependencies on the "
    "rest of the repository; it may not, this simply isn't counted here."
)
_GENERATED_PATHS_INSTRUCTION = (
    "Some files under a generated-output path or with a generator "
    "marker are present -- treat their contents as derived build "
    "output, not hand-authored design, unless the facts above say "
    "otherwise."
)
_GOTCHAS_INSTRUCTION = (
    "For Gotchas, state only that each symbol above has that many callers in "
    "the graph and is therefore worth extra care/tests when changed — do not "
    "characterize WHY it has that many callers, and do not call it "
    "\"foundational\", \"core\", \"critical infrastructure\", or similar: the "
    "caller count is the only fact given, not an explanation of the symbol's "
    "role or importance."
)
_EXTERNAL_INSTRUCTION = (
    "The External context items come from connected sources (issue trackers, "
    "docs, design tools). You MAY use them to enrich the page, but you MUST "
    "attribute each such statement to its source (name the source/link). "
    "Never present external claims as facts about the code without attribution."
)
_SUBSYSTEMS_INSTRUCTION = (
    "In the Architecture section, name and briefly describe each subsystem "
    "rather than attempting to summarize their internals here -- their own pages "
    "cover that in more depth."
)
_SECTIONS_INSTRUCTION = (
    "Ground every statement in the facts above; do not speculate. Omit a section "
    "entirely if the facts above give you nothing to say for it — do not write a "
    "heading with no content."
)

# The whole directive corpus, static rather than per-brief: a page that echoes an
# instruction its own prompt never carried is still broken output, and building
# the corpus from the brief would mean the validator only ever checks the subset
# that happened to be sent. Order is irrelevant, the validator treats it as a bag.
PROMPT_INSTRUCTIONS = (
    _SCOPE_INSTRUCTION,
    _GENERATED_PATHS_INSTRUCTION,
    _GOTCHAS_INSTRUCTION,
    _EXTERNAL_INSTRUCTION,
    _SUBSYSTEMS_INSTRUCTION,
    _SECTIONS_INSTRUCTION,
)


def render_prompt(brief: dict, *, path_prefix: str | None = None) -> str:
    # Every span below that carries bytes from the indexed repository goes inside an
    # `untrusted_block` (see `security.untrusted_block`): the symbol rows and their
    # docstrings, the README excerpt, the ADR bodies, the connector snippets. The
    # rule naming those blocks as data is stated once, here, rather than per block --
    # repeating it N times would multiply its cost by the number of sections without
    # telling the model anything the first statement didn't.
    #
    # What stays OUTSIDE a block is identity, not content: the repo id, the module
    # prefix, the indexed commit, and the per-kind/per-language counters this project
    # computed itself. The repo id and module prefix are also what the blocks' own
    # `src=` attribute names, and that attribute is flattened by `_marker_safe` for
    # the same reason -- they identify the untrusted material rather than being it.
    scope = f"{brief['repo']}/{path_prefix}" if path_prefix else str(brief["repo"])
    lines = [
        UNTRUSTED_DATA_RULE,
        "",
        f"Repository: {brief['repo']}",
    ]
    if path_prefix:
        lines.append(
            f"Scope: ONLY the `{path_prefix}` module/subsystem of this repository. "
            + _SCOPE_INSTRUCTION
        )
    lines += [
        f"Indexed commit: {brief['head']}",
        f"{brief['node_count']} symbols, {brief['edge_count']} relations.",
        f"Languages: {brief['langs']}",
        f"Symbol kinds: {brief['kinds']}",
        "Key symbols (kind, name, file — with signature/docstring where known):",
    ]
    graph_facts = []
    for t in brief["top_symbols"]:
        sig = t.get("signature") or ""
        line = f"  - {t['kind']} {t['name']}{sig} ({t.get('file') or '?'})"
        if t.get("doc"):
            line += f" — {t['doc'][:160]}"
        graph_facts.append(line)
    if brief["packages"]:
        graph_facts.append("Depends on packages: " + ", ".join(brief["packages"]))
    if brief["files"]:
        graph_facts.append("Notable files: " + ", ".join(brief["files"]))
    # Guarded like every other block here: a repo that indexed to nothing (the case
    # `provenance_footer`'s NOT GROUNDED branch exists for) would otherwise carry an
    # empty block under the "Key symbols" label, which costs marker lines to frame
    # nothing.
    if graph_facts:
        lines.append(untrusted_block("\n".join(graph_facts), source=f"{scope} (indexed graph)"))
    has_setup_signal = (brief.get("readme_excerpt") or brief.get("setup_signals")
                        or brief.get("generated_paths_detected"))
    if has_setup_signal:
        lines.append("")
        lines.append("Setup/run signal (from the repo's own files):")
        checkout_facts = []
        if brief.get("setup_signals"):
            checkout_facts.append("  Entry-point/config files present: "
                                  + ", ".join(brief["setup_signals"]))
        if brief.get("readme_excerpt"):
            checkout_facts.append("  From the repo's own README:")
            checkout_facts.append(brief["readme_excerpt"])
        if checkout_facts:
            lines.append(untrusted_block("\n".join(checkout_facts),
                                         source=f"{brief['repo']} (live checkout)"))
        if brief.get("generated_paths_detected"):
            lines.append("  " + _GENERATED_PATHS_INSTRUCTION)
    if brief.get("hubs"):
        lines.append("")
        lines.append("Most-depended-on symbols (ranked by caller count in the graph):")
        lines.append(untrusted_block(
            "\n".join(f"  - {h['kind']} {h['name']} ({h.get('file') or '?'}), "
                      f"{h['count']} caller(s)" for h in brief["hubs"][:8]),
            source=f"{scope} (indexed graph)"))
        lines.append(_GOTCHAS_INSTRUCTION)
    if brief.get("decisions"):
        lines.append("")
        lines.append("Recorded decisions (from the repo's own ADR/decision docs, "
                     "authored facts, not to be reworded as speculation):")
        lines.append(untrusted_block(
            "\n".join(f"  - {d['title']} ({d.get('file') or '?'}): "
                      f"\"{(d.get('doc') or '')[:200]}\"" for d in brief["decisions"]),
            source=f"{scope} (decision records)"))
    if brief.get("external"):
        lines.append("")
        lines.append("External context (from connected sources):")
        lines.append(untrusted_block(
            "\n".join(f"  - [source: {item.get('source')}] {item.get('title')} "
                      f"({item.get('uri')}): \"{item.get('snippet')}\""
                      for item in brief["external"]),
            source=f"{brief['repo']} (connected sources)"))
        lines.append(_EXTERNAL_INSTRUCTION)
    if brief.get("subsystem_modules"):
        lines.append("")
        names = ", ".join(m["prefix"] for m in brief["subsystem_modules"])
        lines.append(
            f"This repo is broken into subsystems, each with its own dedicated wiki page: "
            f"{names}. " + _SUBSYSTEMS_INSTRUCTION
        )
    sections = "Overview"
    if has_setup_signal:
        sections += ", Setup & Run"
    sections += ", Architecture, Dependencies"
    if brief.get("hubs"):
        sections += ", Gotchas"
    if brief.get("decisions"):
        sections += ", Decisions"
    lines += [
        "",
        f"Write a wiki page in Markdown with sections: {sections}, in that order. "
        + _SECTIONS_INSTRUCTION,
    ]
    return "\n".join(lines)


# The footer records which subsystem pages a whole-repo page names, so a
# caller can answer "was this page generated with the current set of
# generation inputs?" separately from "is its commit unchanged?". Those two
# questions have different answers: a store wiki'd before subsystem naming
# existed, or one whose module set changed without a commit changing, is
# commit-fresh but field-stale, and a freshness check that only asks about the
# commit freezes it that way until the commit moves or `--force` is passed.
_SUBSYSTEM_FOOTER_RE = re.compile(r"Subsystem pages: (`[^*]+`)\.")


def subsystem_names(subsystem_modules: list[dict] | None) -> str:
    """Canonical, order-independent rendering of a page's named subsystems --
    the comparable form of both what a page RECORDS (see
    :func:`recorded_subsystems`) and what a caller is about to generate."""
    return ",".join(sorted(m["prefix"] for m in subsystem_modules or []))


def recorded_subsystems(page: str) -> str:
    """The subsystems an already-written page's footer says it names, in
    :func:`subsystem_names` form. ``""`` for a page that names none -- which is
    also what a page written before subsystem naming existed returns, so a
    non-federated repo's existing page is never regenerated just for this."""
    m = _SUBSYSTEM_FOOTER_RE.search(page)
    return ",".join(sorted(re.findall(r"`([^`]+)`", m.group(1)))) if m else ""


def grounded_symbol_count(brief: dict) -> int | None:
    """File-backed symbols behind a brief: what "grounded" means for a page.

    One definition for the three places that must agree about it -- the
    coverage ratio in :func:`provenance_footer`, ``cmd_wiki``'s refusal to
    write a page with nothing behind it, and the dashboard's estimate of what
    a run would regenerate. When they each carried their own arithmetic, the
    disclosure and the decision could disagree about the same page.

    ``node_count`` is the fallback for a hand-built brief predating the field
    (``is None``, not ``or``, so a legitimate 0 is not silently replaced);
    ``None`` only for a brief carrying neither.
    """
    total = brief.get("coverage_total")
    return brief.get("node_count") if total is None else total


def provenance_footer(brief: dict, verified_at: date | None = None, *,
                      path_prefix: str | None = None) -> str:
    cites = ", ".join(f"`{f}`" for f in brief["files"][:10])
    named = ", ".join(f"`{m['prefix']}`"
                      for m in sorted(brief.get("subsystem_modules") or [],
                                      key=lambda m: m["prefix"]))
    subsystems = f" Subsystem pages: {named}." if named else ""
    coverage = ""
    # Both sides of the ratio count file-backed symbols only (see
    # `_repo_brief_core_uncached`), so a whole-repo page and one of its module
    # pages are comparable. The unit is named in the sentence because the
    # prompt's own "N symbols" line counts every node, file-backed or not, and
    # two different numbers with the same name read as a contradiction.
    total = grounded_symbol_count(brief)
    if brief.get("grounded_count") is not None and total:
        pct = round(100 * brief["grounded_count"] / total, 1)
        coverage = (f" Grounded in {brief['grounded_count']}/{total} "
                    f"file-backed symbols ({pct}%).")
    elif total == 0:
        # The disclosure used to be strictly inverted: the ratio was emitted only
        # when the total was non-zero, so a well-grounded page carried a coverage
        # figure and a page grounded in NOTHING carried none at all. Measured on a
        # repository that indexed to zero nodes, which still published a confident
        # 119-line page presenting the forge's boilerplate README as the project's
        # architecture. A page with no symbols behind it needs the loudest
        # disclosure on the site, not the quietest.
        coverage = (" NOT GROUNDED: this repository indexed to 0 file-backed "
                    "symbols, so nothing below is derived from its code. Treat "
                    "every statement as unverified.")
    scope = (f"the `{path_prefix}` module of `{brief['repo']}`" if path_prefix
            else f"`{brief['repo']}`")
    # The parser version goes AFTER the backticked commit, never inside it: four
    # separate readers extract the commit with `at commit \`([^\`]+)\`` (the MCP server,
    # the HTML renderer, the dashboard mutations and the wiki command's own skip), and
    # putting anything inside those backticks would silently change what all four read.
    # Recorded at all because a page can be commit-fresh and graph-stale: the parser
    # changes what it extracts from an unchanged commit.
    parser = brief.get("parser_version")
    return (
        "\n\n---\n"
        f"*Generated from the knowledge graph of {scope} at commit "
        f"`{brief['head']}`" + (f" (parser {parser})" if parser else "")
        + f" on {verified_at or date.today()}."
        + (f" Sources: {cites}." if cites else "")
        + coverage
        + subsystems
        + "*"
    )


def generate_page(llm, store_dir, repo_id: str, *, verified_at: date | None = None,
                  store=None, path_prefix: str | None = None,
                  subsystem_modules: list[dict] | None = None,
                  brief: dict | None = None) -> str | None:
    """Generate a provenance-stamped wiki page (Markdown), or None without a shard.

    ``path_prefix``, when given, scopes the page to one module/subsystem of the
    repo (see ``repo_brief``) instead of the whole repo -- used for federated
    repos too large/varied for one whole-repo page to summarize well. The
    title, prompt framing, and provenance footer are all adjusted so the page
    is unambiguous about describing a slice of the repo, not the repo as a
    whole -- a whole-repo page and a module page for the same ``repo_id`` must
    never read as if they describe the same scope.

    ``subsystem_modules``, when given, is passed straight through to
    ``repo_brief`` (see there) so the whole-repo page names its subsystem
    pages instead of trying to summarize their internals inline. Callers
    generating a module/subsystem page (``path_prefix`` set) should leave
    this ``None``.

    ``brief``, when given, is a ``repo_brief`` result the caller has ALREADY
    built for this exact scope, reused here instead of building a second,
    identical one. A caller that gates the page on a council review (which
    needs the brief for its own ``render_prompt`` call) otherwise pays for
    two full briefs per page -- and the parts of a brief that the shard-level
    cache deliberately does not cover are live-checkout reads (the README, the
    recursive legacy-build-tooling walk) plus the enrichment shard read, so
    the second one is real work, not a cache hit. The caller owns the match:
    build it with the same ``store``/``path_prefix``/``subsystem_modules``
    passed here, since those arguments no longer reach ``repo_brief`` when
    ``brief`` is supplied.
    """
    if brief is None:
        brief = repo_brief(store_dir, repo_id, store=store, path_prefix=path_prefix,
                           subsystem_modules=subsystem_modules)
    if brief is None:
        return None
    body = llm.generate(render_prompt(brief, path_prefix=path_prefix), system=SYSTEM).strip()
    if path_prefix:
        title = (
            f"# {repo_id} — {path_prefix}\n\n"
            f"*This page covers only the `{path_prefix}` module/subsystem of "
            f"`{repo_id}`, not the repository as a whole.*"
        )
    else:
        title = f"# {repo_id}"
    return f"{title}\n\n{body}{provenance_footer(brief, verified_at, path_prefix=path_prefix)}"
