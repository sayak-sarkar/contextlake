"""Generate a curated wiki page for a repo from its knowledge graph.

A page is grounded strictly in facts extracted from the repo's shard (top symbols
by degree, kinds, languages, packages, files) so the model summarizes rather than
invents. Every page ends with a provenance footer citing the commit and sources.
"""

from __future__ import annotations

import os
import threading
from collections import Counter, OrderedDict
from datetime import date
from pathlib import Path

from ..store.shards import read_shard, shard_path

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


def _setup_signals(all_files: set, store=None, repo_id: str | None = None) -> list[str]:
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
    found = {f for f in all_files if _is_setup_filename(f.rsplit("/", 1)[-1])}
    base = None
    if store is not None and repo_id is not None:
        r = store.get_repo(repo_id)
        base = Path(r.path) if r and getattr(r, "path", None) else None
        if base and base.is_dir():
            for entry in base.iterdir():
                if entry.is_file() and _is_setup_filename(entry.name):
                    found.add(entry.name)
    by_ext: dict[str, int] = {}
    counted: set[str] = set()  # relative paths already counted, for merge dedup
    for f in all_files:
        ext = "." + f.rsplit(".", 1)[-1].lower() if "." in f else ""
        if ext in _LEGACY_BUILD_CATEGORIES:
            by_ext[ext] = by_ext.get(ext, 0) + 1
            counted.add(f)
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
    """
    by_kind: dict[str, list[str]] = {}
    order: list[str] = []
    for nid, _count in candidates:
        node = by_id.get(nid)
        if node is None:
            continue
        by_kind.setdefault(node.kind, []).append(nid)
        order.append(nid)
    floor_ids = [ids[0] for ids in by_kind.values()][:cap]
    floor_set = set(floor_ids)
    remaining_cap = max(0, cap - len(floor_ids))
    rest = [nid for nid in order if nid not in floor_set][:remaining_cap]
    seen: set[str] = set()
    out: list[str] = []
    for nid in floor_ids + rest:
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
# the same way it invalidates the shard-parse cache.
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
    by_id = {n.id: n for n in nodes}
    degree: Counter = Counter()
    in_degree: Counter = Counter()   # callers -- a hub, worth protecting with tests
    out_degree: Counter = Counter()  # callees -- a dispatcher, where behavior branches
    for e in edges:
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
    grounded_ids = set(top_ids) | set(hub_ids) | set(dispatcher_ids)
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
        "node_count": len(nodes),
        "edge_count": len(edges),
        "grounded_count": len(grounded_ids),
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


def _repo_brief_core(store_dir, repo_id: str, shard, path_prefix: str | None) -> dict:
    """``_repo_brief_core_uncached`` memoized by the shard file's (mtime_ns, size)
    plus ``path_prefix``. Falls back to computing uncached (never raises, never
    serves a wrong result) if the shard file can't be stat'd for any reason."""
    try:
        p = shard_path(store_dir, repo_id)
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size, path_prefix)
    except (ValueError, OSError):
        key = None
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
    shard = read_shard(store_dir, repo_id)
    if shard is None:
        return None
    core = _repo_brief_core(store_dir, repo_id, shard, path_prefix)
    # Recomputed fresh (not part of the cached core -- see _core_cache's
    # docstring): the full, uncapped file set, needed by _setup_signals.
    nodes, _ = _scoped_nodes_edges(shard, path_prefix)
    all_files = {n.file for n in nodes if n.file}
    return {
        "repo": repo_id,
        "head": shard.head_commit,
        "node_count": core["node_count"],
        "edge_count": core["edge_count"],
        "grounded_count": core["grounded_count"],
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
        "setup_signals": _setup_signals(all_files, store, repo_id),
        "generated_paths_detected": core["generated_paths_detected"],
        "subsystem_modules": subsystem_modules or [],
    }


def render_prompt(brief: dict, *, path_prefix: str | None = None) -> str:
    lines = [
        f"Repository: {brief['repo']}",
    ]
    if path_prefix:
        lines.append(
            f"Scope: ONLY the `{path_prefix}` module/subsystem of this repository. "
            "Every fact below (symbols, dependencies, files) was drawn exclusively "
            "from that module, not the whole repository. Write about this module "
            "only -- do not make claims about the repository as a whole, and do not "
            "write as if this module IS the whole repository. The relation count "
            "below excludes any relation to a symbol outside this module -- do not "
            "treat a low count as evidence this module has few dependencies on the "
            "rest of the repository; it may not, this simply isn't counted here."
        )
    lines += [
        f"Indexed commit: {brief['head']}",
        f"{brief['node_count']} symbols, {brief['edge_count']} relations.",
        f"Languages: {brief['langs']}",
        f"Symbol kinds: {brief['kinds']}",
        "Key symbols (kind, name, file — with signature/docstring where known):",
    ]
    for t in brief["top_symbols"]:
        sig = t.get("signature") or ""
        line = f"  - {t['kind']} {t['name']}{sig} ({t.get('file') or '?'})"
        if t.get("doc"):
            line += f" — {t['doc'][:160]}"
        lines.append(line)
    if brief["packages"]:
        lines.append("Depends on packages: " + ", ".join(brief["packages"]))
    if brief["files"]:
        lines.append("Notable files: " + ", ".join(brief["files"]))
    has_setup_signal = (brief.get("readme_excerpt") or brief.get("setup_signals")
                        or brief.get("generated_paths_detected"))
    if has_setup_signal:
        lines.append("")
        lines.append("Setup/run signal (from the repo's own files):")
        if brief.get("setup_signals"):
            lines.append("  Entry-point/config files present: "
                         + ", ".join(brief["setup_signals"]))
        if brief.get("readme_excerpt"):
            lines.append("  From the repo's own README:")
            lines.append(f"  \"{brief['readme_excerpt']}\"")
        if brief.get("generated_paths_detected"):
            lines.append(
                "  Some files under a generated-output path or with a generator "
                "marker are present -- treat their contents as derived build "
                "output, not hand-authored design, unless the facts above say "
                "otherwise."
            )
    if brief.get("hubs"):
        lines.append("")
        lines.append("Most-depended-on symbols (ranked by caller count in the graph):")
        for h in brief["hubs"][:8]:
            lines.append(f"  - {h['kind']} {h['name']} ({h.get('file') or '?'}), "
                         f"{h['count']} caller(s)")
        lines.append(
            "For Gotchas, state only that each symbol above has that many callers in "
            "the graph and is therefore worth extra care/tests when changed — do not "
            "characterize WHY it has that many callers, and do not call it "
            "\"foundational\", \"core\", \"critical infrastructure\", or similar: the "
            "caller count is the only fact given, not an explanation of the symbol's "
            "role or importance."
        )
    if brief.get("decisions"):
        lines.append("")
        lines.append("Recorded decisions (from the repo's own ADR/decision docs, "
                     "authored facts, not to be reworded as speculation):")
        for d in brief["decisions"]:
            doc = (d.get("doc") or "")[:200]
            lines.append(f"  - {d['title']} ({d.get('file') or '?'}): \"{doc}\"")
    if brief.get("external"):
        lines.append("")
        lines.append("External context (from connected sources):")
        for item in brief["external"]:
            lines.append(
                f"  - [source: {item.get('source')}] {item.get('title')} "
                f"({item.get('uri')}): \"{item.get('snippet')}\""
            )
        lines.append(
            "The External context items come from connected sources (issue trackers, "
            "docs, design tools). You MAY use them to enrich the page, but you MUST "
            "attribute each such statement to its source (name the source/link). "
            "Never present external claims as facts about the code without attribution."
        )
    if brief.get("subsystem_modules"):
        lines.append("")
        names = ", ".join(m["prefix"] for m in brief["subsystem_modules"])
        lines.append(
            f"This repo is broken into subsystems, each with its own dedicated wiki page: "
            f"{names}. In the Architecture section, name and briefly describe each subsystem "
            f"rather than attempting to summarize their internals here -- their own pages "
            f"cover that in more depth."
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
        "Ground every statement in the facts above; do not speculate. Omit a section "
        "entirely if the facts above give you nothing to say for it — do not write a "
        "heading with no content.",
    ]
    return "\n".join(lines)


def provenance_footer(brief: dict, verified_at: date | None = None, *,
                      path_prefix: str | None = None) -> str:
    cites = ", ".join(f"`{f}`" for f in brief["files"][:10])
    coverage = ""
    if brief.get("grounded_count") is not None and brief.get("node_count"):
        pct = round(100 * brief["grounded_count"] / brief["node_count"], 1)
        coverage = f" Grounded in {brief['grounded_count']}/{brief['node_count']} symbols ({pct}%)."
    scope = (f"the `{path_prefix}` module of `{brief['repo']}`" if path_prefix
            else f"`{brief['repo']}`")
    return (
        "\n\n---\n"
        f"*Generated from the knowledge graph of {scope} at commit "
        f"`{brief['head']}` on {verified_at or date.today()}."
        + (f" Sources: {cites}." if cites else "")
        + coverage
        + "*"
    )


def generate_page(llm, store_dir, repo_id: str, *, verified_at: date | None = None,
                  store=None, path_prefix: str | None = None,
                  subsystem_modules: list[dict] | None = None) -> str | None:
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
    """
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
