"""Bounded subgraph extraction + rendering for ``contextlake graph``.

The full knowledge graph is far too large to draw (hundreds of thousands of nodes
across hundreds of repos), so every view here is *scoped*: a seed (a node, a name,
a search, or a whole repo) expanded a few hops with hard node/fan-out caps. Caps
are enforced *during* expansion and any truncation is logged — never silent.

Renders to four formats: ``json`` (the canonical payload), ``dot`` (Graphviz),
``mermaid`` (Markdown-embeddable), and ``html`` (a self-contained, offline-first
cytoscape.js page). A small live server (``serve_graph``) adds click-to-expand.

Pure-Python/stdlib only; no module-level heavy imports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from ..logging_setup import log

if TYPE_CHECKING:  # avoid importing the model at call time; we only need types here
    from .model import Edge, Node
    from .store.base import Store

# ---------------------------------------------------------------------------
# Styling vocab (kind -> colour, confidence -> line style). Generic, no org data.
# ---------------------------------------------------------------------------
KIND_COLORS = {
    "file": "#8ecae6", "module": "#ffb703", "class": "#fb8500", "interface": "#fd9e02",
    "struct": "#f4a261", "function": "#90be6d", "method": "#43aa8b", "enum": "#577590",
    "package": "#e76f51", "repo": "#264653", "issue": "#bc6c25", "page": "#606c38",
    "design": "#9d4edd",
    # flow nodes (from the HTTP / event extractors) — a service surface, not a symbol
    "endpoint": "#f08c3a", "topic": "#b07fd0",
}
DEFAULT_COLOR = "#c9c9c9"
# Relation -> edge hue (within the brand family; greys for structural relations).
# Open vocabulary: unknown relations fall back to DEFAULT_EDGE_COLOR.
RELATION_COLORS = {
    "calls": "#137A8B", "imports": "#2BB3A3", "contains": "#9fb4b8",
    "depends_on": "#E7B53C", "publishes": "#D7C5A0", "tracked_by": "#577590",
    "documented_by": "#9d4edd", "flow": "#e5571f", "exposes": "#f08c3a",
    "calls_http": "#c1440e",
}
DEFAULT_EDGE_COLOR = "#aecace"
_CONF_DOT = {"EXTRACTED": "solid", "INFERRED": "dashed", "AMBIGUOUS": "dotted"}
# Confidence -> human label + trust dot, surfaced in the edge inspector.
CONF_META = {
    "EXTRACTED": ("Extracted", "#2BB3A3", "Direct from source (AST / manifest)"),
    "INFERRED": ("Inferred", "#E7B53C", "Deduced — second-pass / heuristic"),
    "AMBIGUOUS": ("Ambiguous", "#e76f51", "Uncertain — flagged for review"),
}
_CDN_URL = "https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js"

# contextlake brand palette (see BRANDING.md): a lake seen in cross-section.
_BRAND = {"deepwater": "#0E2A33", "lake": "#137A8B", "current": "#2BB3A3",
          "mist": "#EAF4F4", "shore": "#D7C5A0", "sun": "#E7B53C"}
# The brand glyph, inlined so the page stays self-contained/offline.
_GLYPH_SVG = (
    '<svg class="glyph" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg"'
    ' role="img" aria-label="contextlake">'
    '<defs><clipPath id="cglyph"><rect x="2" y="2" width="60" height="60" rx="14"/></clipPath>'
    "</defs>"
    '<rect x="2" y="2" width="60" height="60" rx="14" fill="#EAF4F4"/>'
    '<g clip-path="url(#cglyph)">'
    '<rect x="2" y="26" width="60" height="12" fill="#2BB3A3"/>'
    '<rect x="2" y="38" width="60" height="12" fill="#137A8B"/>'
    '<rect x="2" y="50" width="60" height="12" fill="#0E2A33"/>'
    '<g fill="none" stroke="#EAF4F4" stroke-width="2" stroke-linecap="round" opacity="0.75">'
    '<path d="M23 28 q9 4 18 0"/><path d="M16 32 q16 5 32 0"/></g></g>'
    '<rect x="2" y="24" width="60" height="2.5" fill="#137A8B"/>'
    '<path d="M32 7 C 35 12, 37 14, 37 17 a5 5 0 1 1 -10 0 C 27 14, 29 12, 32 7 Z"'
    ' fill="#E7B53C"/></svg>'
)

# Per-kind glyphs (inner SVG paths, Lucide-style line icons) painted onto nodes so a
# diagram reads by *type* at a glance — a file vs a service vs an HTTP endpoint. Kept
# as bare 24x24 path content; the stroke colour is chosen per node at build time
# (_kind_icons) for contrast, and the data-URI is inlined so the page stays offline.
_KIND_ICON_PATHS = {
    "file": '<path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12'
            'a2 2 0 0 0 2-2V8z"/>',
    "page": '<path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12'
            'a2 2 0 0 0 2-2V8z"/><line x1="8" y1="13" x2="15" y2="13"/>'
            '<line x1="8" y1="17" x2="15" y2="17"/>',
    "module": '<path d="M12 2 2 7l10 5 10-5z"/><path d="M2 17l10 5 10-5"/>'
              '<path d="M2 12l10 5 10-5"/>',
    "class": '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8'
             'v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>',
    "struct": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" '
              'height="7"/><rect x="14" y="14" width="7" height="7"/>'
              '<rect x="3" y="14" width="7" height="7"/>',
    "interface": '<path d="M8 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h1"/>'
                 '<path d="M16 3h1a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-1"/>',
    "enum": '<circle cx="4" cy="6" r="1.3"/><circle cx="4" cy="12" r="1.3"/>'
            '<circle cx="4" cy="18" r="1.3"/><line x1="9" y1="6" x2="21" y2="6"/>'
            '<line x1="9" y1="12" x2="21" y2="12"/><line x1="9" y1="18" x2="21" y2="18"/>',
    "function": '<polyline points="8 7 3 12 8 17"/><polyline points="16 7 21 12 16 17"/>',
    "method": '<polyline points="8 7 3 12 8 17"/><polyline points="16 7 21 12 16 17"/>',
    "package": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8'
               'a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>'
               '<path d="M3.3 7 12 12l8.7-5"/><line x1="12" y1="22" x2="12" y2="12"/>',
    "repo": '<rect x="3" y="4" width="18" height="7" rx="1.5"/>'
            '<rect x="3" y="13" width="18" height="7" rx="1.5"/>'
            '<line x1="7" y1="7.5" x2="7.01" y2="7.5"/>'
            '<line x1="7" y1="16.5" x2="7.01" y2="16.5"/>',
    "issue": '<circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/>'
             '<line x1="12" y1="16" x2="12.01" y2="16"/>',
    "design": '<path d="M12 3l1.9 5.1L19 11l-5.1 1.9L12 18l-1.9-5L5 11l5.1-1.9z"/>',
    "endpoint": '<circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/>'
                '<path d="M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z"/>',
    "topic": '<path d="M21 14a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
}


def _luma(hex_color: str) -> float:
    """Perceived brightness (0-255) of a #rrggbb colour — picks glyph contrast."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b


def _icon_uri(inner: str, stroke: str) -> str:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        'viewBox="0 0 24 24" fill="none" stroke="' + stroke + '" stroke-width="2.2" '
        'stroke-linecap="round" stroke-linejoin="round">' + inner + '</svg>'
    )
    return "data:image/svg+xml;utf8," + quote(svg, safe="")


def _kind_icons() -> dict:
    """kind -> data-URI glyph, stroke coloured for contrast against the node fill."""
    out = {}
    for kind, inner in _KIND_ICON_PATHS.items():
        color = KIND_COLORS.get(kind, DEFAULT_COLOR)
        stroke = "#0E2A33" if _luma(color) > 150 else "#ffffff"
        out[kind] = _icon_uri(inner, stroke)
    return out


# Primary-language lettermark for repo nodes, so the fleet diagram shows its tech
# stack at a glance. Keys are the parser's lang ids; unknown languages keep the
# generic repo glyph. White text reads on the dark navy repo fill.
_LANG_LABELS = {
    "python": "PY", "javascript": "JS", "typescript": "TS", "tsx": "TS",
    "csharp": "C#", "c_sharp": "C#", "java": "JV", "go": "GO", "ruby": "RB",
    "rust": "RS", "php": "PHP", "kotlin": "KT", "cpp": "C++", "c": "C",
}


def _lang_icon(label: str) -> str:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        'viewBox="0 0 24 24"><text x="12" y="16" text-anchor="middle" '
        'font-family="ui-sans-serif,system-ui,sans-serif" font-size="10" '
        'font-weight="700" fill="#ffffff">' + label + '</text></svg>'
    )
    return "data:image/svg+xml;utf8," + quote(svg, safe="")


def _lang_icons() -> dict:
    """lang id -> data-URI lettermark glyph (overlaid on repo nodes)."""
    return {lang: _lang_icon(label) for lang, label in _LANG_LABELS.items()}


# ---------------------------------------------------------------------------
# Canonical serialization (one shape reused by json / html / the /neighbors API)
# ---------------------------------------------------------------------------
def _node_dict(n) -> dict:
    if isinstance(n, dict):
        return n
    return {"id": n.id, "repo": n.repo, "kind": n.kind, "name": n.name,
            "qualified_name": n.qualified_name, "file": n.file, "line": n.line_start,
            "lang": n.lang}


def _edge_dict(e) -> dict:
    if isinstance(e, dict):
        return e
    conf = e.confidence.value if hasattr(e.confidence, "value") else str(e.confidence)
    prov = getattr(e, "provenance", None)
    # verified_at is a datetime.date — must serialize to a string or json.dumps throws.
    verified = getattr(prov, "verified_at", None)
    return {"src": e.src, "dst": e.dst, "relation": e.relation, "confidence": conf,
            "context": e.context, "weight": e.weight,
            "prov_file": getattr(prov, "source_file", None),
            "prov_line": getattr(prov, "source_line", None),
            "verified_at": verified.isoformat() if verified else None}


def to_payload(nodes, edges, meta: dict | None = None) -> dict:
    """Normalize (Node|dict, Edge|dict) lists into the canonical payload."""
    nd = [_node_dict(n) for n in nodes]
    ed = [_edge_dict(e) for e in edges]
    m = dict(meta or {})
    m.setdefault("node_count", len(nd))
    m.setdefault("edge_count", len(ed))
    return {"nodes": nd, "edges": ed, "meta": m}


# ---------------------------------------------------------------------------
# Subgraph builders
# ---------------------------------------------------------------------------
def seed_ids_from_args(store: Store, args) -> list[str]:
    """Resolve --node / --name(+--kind) / --search / positional text into seed ids."""
    node = getattr(args, "node", None)
    if node:
        return [node]
    kind = getattr(args, "kind", None)
    repo = getattr(args, "repo", None)
    name = getattr(args, "name", None)
    limit = getattr(args, "limit", None) or 20
    if name:
        nodes = store.nodes_by_name(name, kind=kind, repo=repo)
    else:
        query = getattr(args, "search", None) or " ".join(getattr(args, "args", []) or []).strip()
        if not query:
            return []
        nodes = store.search(query, kind=kind, repo=repo, limit=limit)
    ids = [n.id for n in nodes]
    if len(ids) > limit:
        log(f"  {len(ids)} seed nodes matched; using the first {limit}")
        ids = ids[:limit]
    return ids


def extract_subgraph(store: Store, seed_ids, *, hops: int = 2, max_nodes: int = 500,
                     max_fanout: int = 50, relation: str | None = None,
                     direction: str = "both",
                     meta: dict | None = None) -> tuple[list[Node], list[Edge]]:
    """BFS a bounded subgraph out from ``seed_ids``.

    Caps are enforced while expanding: each ``neighbors()`` result is sliced to
    ``max_fanout`` (a hub node can otherwise return tens of thousands of edges),
    and node growth stops at ``max_nodes``. Edge objects are retained, then the
    result is filtered to the *induced* subgraph (both endpoints kept) so no
    dangling edges reach the renderers. Truncation is logged, never silent.
    """
    seen: set[str] = set()
    nodes: list[Node] = []
    truncated = False

    for sid in seed_ids:
        if sid in seen:
            continue
        node = store.get_node(sid)
        if node is None:
            log(f"  seed {sid!r} not found in the graph — skipping")
            continue
        seen.add(sid)
        nodes.append(node)

    frontier = list(seen)
    edges_by_key: dict[tuple, Edge] = {}
    for _hop in range(max(0, hops)):
        if len(seen) >= max_nodes:
            truncated = True
            break
        nxt: list[str] = []
        for nid in frontier:
            nbrs = store.neighbors(nid, relation=relation, direction=direction)
            if len(nbrs) > max_fanout:
                truncated = True
                nbrs = sorted(nbrs, key=lambda e: (e.relation, e.src, e.dst))[:max_fanout]
            for e in nbrs:
                edges_by_key.setdefault((e.src, e.dst, e.relation), e)
                other = e.dst if e.src == nid else e.src
                if other in seen:
                    continue
                if len(seen) >= max_nodes:
                    truncated = True
                    continue
                node = store.get_node(other)
                if node is None:
                    continue
                seen.add(other)
                nodes.append(node)
                nxt.append(other)
        frontier = nxt
        if not frontier:
            break

    edges = [e for e in edges_by_key.values() if e.src in seen and e.dst in seen]
    if truncated:
        log(f"  truncated: reached max_nodes={max_nodes} / max_fanout={max_fanout} "
            f"(the real neighbourhood is larger)")
    if meta is not None:
        # BFS early-stops, so the true size is unknown — flag truncation but DON'T
        # report a total (a number here would be a fabrication).
        meta["truncated"] = truncated
    return nodes, edges


def repo_subgraph(store: Store, repo_id: str, *, max_nodes: int = 500,
                  meta: dict | None = None) -> tuple[list[Node], list[Edge]]:
    """One repo's internal graph: its nodes (capped) and the edges among them."""
    rows = store.conn.execute(
        "SELECT node_id FROM nodes WHERE repo_id=? ORDER BY node_id LIMIT ?",
        (repo_id, max_nodes + 1),
    ).fetchall()
    truncated = len(rows) > max_nodes
    ids = [r[0] for r in rows[:max_nodes]]
    seen = set(ids)
    nodes = [n for nid in ids if (n := store.get_node(nid)) is not None]
    edges: list[Edge] = []
    edge_keys: set[tuple] = set()
    for nid in ids:
        for e in store.neighbors(nid, direction="out"):
            if e.dst in seen:
                k = (e.src, e.dst, e.relation)
                if k not in edge_keys:
                    edge_keys.add(k)
                    edges.append(e)
    if truncated:
        log(f"  truncated: repo {repo_id!r} has more than {max_nodes} nodes")
    if meta is not None:
        meta["truncated"] = truncated
        if truncated:  # cheap exact total only when we actually capped
            meta["total"] = store.conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE repo_id=?", (repo_id,)).fetchone()[0]
    return nodes, edges


def overview_subgraph(store: Store, *, max_nodes: int = 5000,
                      meta: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Repos-as-nodes with **real** cross-repo dependency edges (the architecture map).

    Edges come from the package two-hop (``publishes ⨝ depends_on``, see
    ``arch.resolve.repo_dependency_edges``) — the only trustworthy cross-repo
    signal. The raw cross-repo ``imports`` join is deliberately NOT used: it is
    dominated by import-star artifacts (global ``module`` nodes shared fleet-wide),
    which would render hundreds of thousands of phantom edges. Dependencies are
    marked ``INFERRED`` (manifest-derived, a likely undercount — not ground truth).
    """
    from .arch.resolve import repo_dependency_edges, repo_event_flow_edges, repo_http_flow_edges
    sizes = dict(store.conn.execute(
        "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
    log("  resolving real cross-repo dependencies (package two-hop)…")
    # structural deps (depends_on) + runtime flow (HTTP + events); all INFERRED,
    # all repo→repo. Flow is empty until an index has run the flow extractors.
    dep_edges = (repo_dependency_edges(store) + repo_http_flow_edges(store)
                 + repo_event_flow_edges(store))

    degree: dict[str, int] = {}
    for e in dep_edges:
        degree[e["src"]] = degree.get(e["src"], 0) + e["weight"]
        degree[e["dst"]] = degree.get(e["dst"], 0) + e["weight"]

    # One node per repo for the WHOLE fleet: the repo registry (list_repos) unioned
    # with any repo that has nodes — so even a repo with no parsed code (no edges) is
    # present and findable. Rank by connectivity then content so that if the fleet
    # exceeds max_nodes the most-connected/biggest win and empty repos drop first —
    # never alphabetically (which would hide heavily-linked hubs that sort late).
    candidates = {r.id for r in store.list_repos()} | set(sizes)
    ranked = sorted(candidates, key=lambda r: (-degree.get(r, 0), -sizes.get(r, 0), r))
    truncated = len(ranked) > max_nodes
    repo_ids = ranked[:max_nodes]
    keep = set(repo_ids)
    # Label with the short repo name (last path segment) so nodes are distinguishable
    # — the full id is a long shared-prefix path that truncates to an identical,
    # useless stub on every node. The full id stays as qualified_name (searchable +
    # shown in the inspector) and as repo.
    # dominant language per repo -> drives the tech-stack lettermark in the overview,
    # so the fleet architecture map reads its stack at a glance (one GROUP BY pass).
    dom_lang: dict[str, str] = {}
    best_lang_count: dict[str, int] = {}
    for repo, lang, cnt in store.conn.execute(
            "SELECT repo_id, lang, COUNT(*) FROM nodes "
            "WHERE lang IS NOT NULL GROUP BY repo_id, lang").fetchall():
        if repo in keep and cnt > best_lang_count.get(repo, 0):
            best_lang_count[repo] = cnt
            dom_lang[repo] = lang
    nodes = [{"id": r, "repo": r, "kind": "repo", "name": r.rsplit("/", 1)[-1],
              "qualified_name": r, "file": None, "line": None, "lang": dom_lang.get(r),
              "attrs": {"node_count": sizes.get(r, 0)}} for r in repo_ids]
    edges = [e for e in dep_edges if e["src"] in keep and e["dst"] in keep]
    if truncated:
        log(f"  {len(ranked)} repos; showing the {max_nodes} most "
            f"connected (raise --max-nodes to see more)")
    if meta is not None:
        meta["truncated"] = truncated
        meta["total"] = len(ranked)  # exact: every repo is a known candidate
    return nodes, edges


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------
def to_json(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _dot_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def to_dot(payload: dict) -> str:
    lines = ["digraph contextlake {", "  rankdir=LR;",
             '  node [style=filled, shape=box, fontname="sans-serif"];']
    idmap: dict[str, str] = {}
    for i, n in enumerate(payload["nodes"]):
        sid = f"n{i}"
        idmap[n["id"]] = sid
        color = KIND_COLORS.get(n.get("kind"), DEFAULT_COLOR)
        label = _dot_escape(n.get("name") or n["id"])
        lines.append(f'  {sid} [label="{label}", fillcolor="{color}", '
                     f'tooltip="{_dot_escape(n.get("kind", ""))}"];')
    for e in payload["edges"]:
        s, d = idmap.get(e["src"]), idmap.get(e["dst"])
        if not s or not d:
            continue
        style = _CONF_DOT.get(e.get("confidence", "EXTRACTED"), "solid")
        lines.append(f'  {s} -> {d} [label="{_dot_escape(e["relation"])}", style={style}];')
    lines.append("}")
    return "\n".join(lines)


def _mermaid_escape(s: str) -> str:
    return (s or "").replace('"', "&quot;").replace("[", "(").replace("]", ")")


def to_mermaid(payload: dict) -> str:
    lines = ["graph LR"]
    idmap: dict[str, str] = {}
    for i, n in enumerate(payload["nodes"]):
        mid = f"n{i}"
        idmap[n["id"]] = mid
        lines.append(f'  {mid}["{_mermaid_escape(n.get("name") or n["id"])}"]')
    for e in payload["edges"]:
        s, d = idmap.get(e["src"]), idmap.get(e["dst"])
        if not s or not d:
            continue
        lines.append(f'  {s} -->|{_mermaid_escape(e["relation"])}| {d}')
    return "\n".join(lines)


def _cytoscape_elements(payload: dict) -> list[dict]:
    els = []
    for n in payload["nodes"]:
        attrs = n.get("attrs") or {}
        els.append({"data": {
            "id": n["id"], "label": n.get("name") or n["id"], "kind": n.get("kind", ""),
            "repo": n.get("repo", ""), "qn": n.get("qualified_name") or "",
            "file": n.get("file") or "", "line": n.get("line"),
            "count": attrs.get("node_count"), "href": n.get("href") or "",
            "lang": n.get("lang") or "",
        }})
    for e in payload["edges"]:
        els.append({"data": {"source": e["src"], "target": e["dst"],
                             "relation": e.get("relation", ""),
                             "confidence": e.get("confidence", "EXTRACTED"),
                             "context": e.get("context") or "",
                             "weight": e.get("weight", 1.0),  # always present -> mapData safe
                             "prov_file": e.get("prov_file") or "",
                             "prov_line": e.get("prov_line"),
                             "verified_at": e.get("verified_at") or ""}})
    return els


def _cytoscape_js() -> str:
    """The vendored cytoscape.min.js text, made safe to inline in a <script>."""
    from importlib.resources import files
    js = (files("contextlake.kb") / "static" / "cytoscape.min.js").read_text(encoding="utf-8")
    return js.replace("</script", "<\\/script")


def _app_css() -> str:
    """The visualizer's stylesheet (extracted to static/app.css, inlined at emit)."""
    from importlib.resources import files
    return (files("contextlake.kb") / "static" / "app.css").read_text(encoding="utf-8")


def _app_js() -> str:
    """The visualizer's app JS (static/app.js), made safe to inline in a <script>."""
    from importlib.resources import files
    js = (files("contextlake.kb") / "static" / "app.js").read_text(encoding="utf-8")
    return js.replace("</script", "<\\/script")


LAYOUTS = ("cose", "concentric", "breadthfirst", "circle", "grid")


def to_html(payload: dict, *, cdn: bool = False, live: bool = False,
            layout: str = "cose", title: str = "contextlake graph",
            assets: str = "inline", site: bool = False) -> str:
    """A single self-contained HTML page rendering the subgraph with cytoscape.js.

    Default inlines the vendored lib + CSS/JS so the file works offline / air-gapped;
    pass ``cdn=True`` for a small online-only file. ``assets="sibling"`` references
    ``cytoscape.min.js`` / ``app.css`` / ``app.js`` as relative files instead of
    inlining them — used by ``build_site`` so a folder of cross-linked pages shares
    one copy of each asset rather than inlining ~1 MB per page. ``site=True`` enables
    cross-page navigation (overview repo nodes carry an ``href`` to their repo page).
    ``live=True`` wires node taps to a ``/neighbors`` endpoint (used by ``serve_graph``).
    """
    if cdn:
        lib_tag = f'<script src="{_CDN_URL}"></script>'
    elif assets == "sibling":
        lib_tag = '<script src="cytoscape.min.js"></script>'
    else:
        lib_tag = f"<script>{_cytoscape_js()}</script>"
    if assets == "sibling":
        style_block = '<link rel="stylesheet" href="app.css">'
        app_js_block = '</script>\n<script src="app.js"></script>'
    else:
        style_block = f"<style>{_app_css()}</style>"
        app_js_block = f"  {_app_js()}</script>"
    from collections import Counter
    elements = json.dumps(_cytoscape_elements(payload))
    colors = json.dumps(KIND_COLORS)
    icon_map = _kind_icons()
    lang_icon_map = _lang_icons()
    icons = json.dumps(icon_map)
    lang_icons = json.dumps(lang_icon_map)
    kind_counts = Counter(n.get("kind", "") for n in payload["nodes"])

    def _kind_swatch(k: str, c: str) -> str:
        # Reuse the very data-URI glyph the node paints (zero extra payload, one
        # source of truth). The glyph stroke is contrast-picked for the node FILL, so
        # render it on a fill-coloured swatch — exactly mirroring the node on canvas.
        icon = icon_map.get(k)
        if icon:
            return f'<span class="gl" style="background:{c}"><img src="{icon}" alt=""></span>'
        return f'<i style="background:{c}"></i>'   # open-vocab kind with no glyph

    legend = "".join(
        f'<button type="button" class="lg" data-kind="{k}">'
        f'{_kind_swatch(k, c)}<span class="lbl">{k}</span>'
        f'<span class="cnt">{kind_counts[k]}</span></button>'
        for k, c in KIND_COLORS.items() if kind_counts.get(k, 0) > 0)
    # edge legend = relations actually present (known hues first, then open-vocab)
    rel_counts = Counter(e.get("relation") for e in payload["edges"])
    present = {r for r in rel_counts if r}
    known = [r for r in RELATION_COLORS if r in present]
    rel_order = known + sorted(present - set(RELATION_COLORS))
    edge_legend = "".join(
        f'<button type="button" class="lg rel" data-rel="{r}">'
        f'<i style="background:{RELATION_COLORS.get(r, DEFAULT_EDGE_COLOR)}"></i>'
        f'<span class="lbl">{r}</span><span class="cnt">{rel_counts[r]}</span></button>'
        for r in rel_order)
    # Legend key (collapsible): line-style = edge confidence; lettermark = repo
    # language. Both filtered to what is actually present so the key never lies.
    conf_present = [cf for cf in _CONF_DOT
                    if cf in {e.get("confidence") for e in payload["edges"]}]
    conf_key = "".join(
        f'<div class="ck"><span class="ln {_CONF_DOT[cf]}"></span>'
        f'<span class="lbl">{CONF_META[cf][0]}</span>'
        f'<span class="cnt">{CONF_META[cf][2]}</span></div>'
        for cf in conf_present)
    repo_langs = {n.get("lang") for n in payload["nodes"] if n.get("kind") == "repo"}
    langs_present = [lg for lg in _LANG_LABELS if lg in repo_langs]
    repo_fill = KIND_COLORS.get("repo", DEFAULT_COLOR)
    lang_key = "".join(
        f'<div class="ck"><span class="gl" style="background:{repo_fill}">'
        f'<img src="{lang_icon_map[lg]}" alt=""></span>'
        f'<span class="lbl">{_LANG_LABELS[lg]}</span></div>'
        for lg in langs_present)
    keys_inner = ""
    if conf_key:
        keys_inner += f'<div class="kgroup"><h3>Confidence</h3>{conf_key}</div>'
    if lang_key:
        keys_inner += f'<div class="kgroup"><h3>Languages</h3>{lang_key}</div>'
    legend_keys = (f'<details class="legend-keys"><summary>Legend key</summary>'
                   f'{keys_inner}</details>') if keys_inner else ""
    options = "".join(f'<option value="{n}">{n}</option>' for n in LAYOUTS)
    meta = json.dumps(payload.get("meta", {}))
    return (_HTML_TEMPLATE
            .replace("__STYLE_BLOCK__", style_block)
            .replace("__APP_JS_BLOCK__", app_js_block)
            .replace("__TITLE__", title)
            .replace("__SITE__", "true" if site else "false")
            .replace("__LIB_TAG__", lib_tag)
            .replace("__ELEMENTS__", elements)
            .replace("__COLORS__", colors)
            .replace("__ICONS__", icons)
            .replace("__LANG_ICONS__", lang_icons)
            .replace("__DEFAULT_COLOR__", DEFAULT_COLOR)
            .replace("__REL_COLORS__", json.dumps(RELATION_COLORS))
            .replace("__DEFAULT_EDGE_COLOR__", DEFAULT_EDGE_COLOR)
            .replace("__CONF_META__", json.dumps(CONF_META))
            .replace("__LEGEND__", legend)
            .replace("__EDGE_LEGEND__", edge_legend)
            .replace("__LEGEND_KEYS__", legend_keys)
            .replace("__LAYOUT_OPTIONS__", options)
            .replace("__GLYPH__", _GLYPH_SVG)
            .replace("__META__", meta)
            .replace("__LAYOUT__", layout if layout in LAYOUTS else "cose")
            .replace("__LIVE__", "true" if live else "false"))


# ---------------------------------------------------------------------------
# Static cross-linked site
# ---------------------------------------------------------------------------
def repo_slug(repo_id: str) -> str:
    """Filesystem-safe page name for a repo id (matches the wiki convention)."""
    return repo_id.replace("/", "__")


def _read_static_raw(name: str) -> str:
    from importlib.resources import files
    return (files("contextlake.kb") / "static" / name).read_text(encoding="utf-8")


def _md_to_html(md: str) -> str:
    """A tiny, dependency-free Markdown -> HTML renderer for wiki prose.

    Handles headings, fenced code, unordered lists, paragraphs, and inline
    code/bold/italic/links — enough for generated wiki pages. HTML is escaped
    *first* (the wiki is LLM-derived from repo content, so it's untrusted), then
    the Markdown punctuation that survives escaping is transformed.
    """
    import re as _re

    def esc(s: str) -> str:
        # Escape quotes too: rendered text is interpolated into href="…" attributes
        # below, and the wiki is untrusted (LLM-derived from repo content) — without
        # this, a crafted link URL could break out of the attribute (stored XSS).
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#39;"))

    def inline(s: str) -> str:
        s = esc(s)
        s = _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", s)
        # URL class excludes quotes/brackets/whitespace so it can't escape the
        # attribute even if escaping above ever regressed (defense in depth).
        s = _re.sub(r"\[([^\]]+)\]\((https?://[^)\s\"'<>]+)\)",
                    r'<a href="\2" rel="noopener noreferrer">\1</a>', s)
        return s

    out: list[str] = []
    para: list[str] = []
    in_list = False
    lines = md.split("\n")
    i = 0

    def flush_para():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        h = _re.match(r"(#{1,4})\s+(.*)", line)
        if h:
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            lvl = len(h.group(1))
            out.append(f"<h{lvl}>{inline(h.group(2))}</h{lvl}>")
            i += 1
            continue
        li = _re.match(r"\s*[-*]\s+(.*)", line)
        if li:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline(li.group(1)) + "</li>")
            i += 1
            continue
        if not line.strip():
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            i += 1
            continue
        para.append(line.strip())
        i += 1
    flush_para()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


_WIKI_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>contextlake — __REPO__ wiki</title>
<style>
  :root{--lake:#137A8B;--bg:#f5fafb;--surface:#fff;--line:#dce8ea;--text:#0E2A33;
    --muted:#5b7177;--sun:#E7B53C;--ff:"Inter",system-ui,-apple-system,Segoe UI,sans-serif;
    --ff-d:"Space Grotesk",var(--ff)}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--ff);line-height:1.6}
  header{display:flex;align-items:center;gap:10px;padding:14px 28px;background:var(--surface);
    border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
  .wm{font-family:var(--ff-d);font-weight:600}.wm .l{color:var(--lake)}
  header .repo{color:var(--muted);font-size:14px}
  header a{margin-left:auto;color:var(--lake);text-decoration:none;font-size:14px}
  header a:hover{text-decoration:underline}
  .badge{font-size:12px;padding:3px 10px;border-radius:999px;font-weight:600}
  .badge.fresh{background:#e6f6f1;color:#0f6473}
  .badge.stale{background:#fbf0d6;color:#7a5b16}
  main{max-width:820px;margin:0 auto;padding:8px 28px 64px}
  .advisory{font-size:13px;color:var(--muted);border-left:3px solid var(--sun);
    padding:8px 12px;margin:18px 0;background:var(--surface);border-radius:6px}
  h1,h2,h3{font-family:var(--ff-d);line-height:1.25;margin:1.4em 0 .5em}
  h1{font-size:24px}h2{font-size:19px}h3{font-size:16px}
  code{background:#eef6f7;padding:1px 5px;border-radius:4px;font-size:.92em}
  pre{background:var(--surface);border:1px solid var(--line);border-radius:10px;
    padding:14px;overflow:auto}pre code{background:none;padding:0}
  a{color:var(--lake)} ul{padding-left:22px}
</style></head>
<body>
  <header>__GLYPH__<span class="wm">context<span class="l">lake</span></span>
    <span class="repo">__REPO__</span><span class="badge __STALECLASS__">__STALE__</span>
    <a href="repo-__SLUG__.html">graph →</a></header>
  <main>
    <div class="advisory">Advisory — this page is LLM-synthesised from the knowledge graph.
      Verify against the cited sources; it never outranks extracted facts.</div>
    __BODY__
  </main>
</body></html>
"""


_INDEX_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>contextlake — index</title>
<style>
  :root{--deepwater:#0E2A33;--lake:#137A8B;--current:#2BB3A3;--bg:#f5fafb;
    --surface:#fff;--line:#dce8ea;--text:#0E2A33;--muted:#5b7177;--subtle:#8aa2a6;
    --ff:"Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    --ff-d:"Space Grotesk",var(--ff)}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--ff);
    line-height:1.5}
  header{display:flex;align-items:center;gap:10px;padding:16px 28px;background:var(--surface);
    border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
  .glyph{width:30px;height:30px;border-radius:8px;display:block;
    box-shadow:0 1px 2px rgba(14,42,51,.1)}
  .wm{font-family:var(--ff-d);font-size:19px;font-weight:600;letter-spacing:-.01em}
  .wm .l{color:var(--lake)}
  header .sub{color:var(--muted);font-size:13px;margin-left:4px}
  header a{margin-left:auto;color:var(--lake);text-decoration:none;font-size:14px;font-weight:500}
  header a:hover{text-decoration:underline}
  main{max-width:1100px;margin:0 auto;padding:24px 28px 60px;
    columns:320px;column-gap:24px}
  section{break-inside:avoid;margin:0 0 20px;background:var(--surface);
    border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  h2{font-family:var(--ff-d);font-size:14px;margin:0 0 10px;display:flex;
    align-items:center;gap:8px;text-transform:none}
  h2 .c{margin-left:auto;color:var(--subtle);font-size:12px;font-weight:500;
    font-variant-numeric:tabular-nums}
  ul{list-style:none;margin:0;padding:0}
  li{display:flex;align-items:baseline;gap:8px;padding:4px 0;border-top:1px solid var(--line);
    font-size:13px}
  li:first-of-type{border-top:0}
  li a{color:var(--lake);text-decoration:none;font-weight:500;flex:none}
  li a.wk{font-size:11px;color:var(--muted);font-weight:400;border:1px solid var(--line);
    border-radius:999px;padding:0 7px}
  li a.wk:hover{color:var(--lake);border-color:var(--lake)}
  li a:hover{text-decoration:underline}
  li .p{color:var(--subtle);font-size:11px;overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;flex:1}
  li .c{color:var(--subtle);font-size:11px;font-variant-numeric:tabular-nums;flex:none}
</style></head>
<body>
  <header>__GLYPH__
    <span class="wm">context<span class="l">lake</span></span>
    <span class="sub">__N__ repos with a parsed graph</span>
    <a href="overview.html">Fleet overview →</a></header>
  <main>__BODY__</main>
</body></html>
"""


def _wiki_page(repo: str, md: str, store: Store) -> str:
    """Render a repo's wiki Markdown into a standalone HTML page with a staleness badge."""
    import re as _re
    m = _re.search(r"at commit `([^`]+)`", md)
    wiki_commit = m.group(1) if m else None
    r = store.get_repo(repo)
    current = r.head_commit if r else None
    stale = wiki_commit is None or current is None or wiki_commit != current
    repo_esc = (repo.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#39;"))
    badge = ("stale · regenerate" if stale
             else "fresh · " + (wiki_commit or "")[:8])
    return (_WIKI_TEMPLATE
            .replace("__GLYPH__", _GLYPH_SVG)
            .replace("__REPO__", repo_esc)
            .replace("__SLUG__", repo_slug(repo))
            .replace("__STALECLASS__", "stale" if stale else "fresh")
            .replace("__STALE__", badge)
            .replace("__BODY__", _md_to_html(md)))


def _site_index(repos: list[str], sizes: dict, pages: dict, wiki: dict | None = None) -> str:
    from collections import defaultdict
    wiki = wiki or {}
    groups: dict[str, list[str]] = defaultdict(list)
    for r in repos:
        groups[r.split("/")[0]].append(r)
    sections = []
    for ns in sorted(groups):
        items = "".join(
            f'<li><a href="{pages[r]}">{r.rsplit("/", 1)[-1]}</a>'
            + (f'<a class="wk" href="{wiki[r]}">wiki</a>' if r in wiki else "")
            + f'<span class="p">{r}</span><span class="c">{sizes.get(r, 0)}</span></li>'
            for r in sorted(groups[ns]))
        sections.append(
            f'<section><h2>{ns}<span class="c">{len(groups[ns])}</span></h2>'
            f"<ul>{items}</ul></section>")
    return (_INDEX_TEMPLATE
            .replace("__GLYPH__", _GLYPH_SVG)
            .replace("__N__", str(len(repos)))
            .replace("__BODY__", "\n".join(sections)))


def _match_repo(repo_id: str, patterns: list[str]) -> bool:
    """A repo matches if any pattern is a glob hit or a plain substring of its id."""
    from fnmatch import fnmatch
    return any(fnmatch(repo_id, p) or p in repo_id for p in patterns)


def build_site(store: Store, out_dir, *, max_nodes: int = 5000,
               repo_max_nodes: int = 500, overview_layout: str = "concentric",
               repo_layout: str = "cose", repos: list[str] | None = None,
               log=lambda _m: None) -> Path:
    """Emit a folder of cross-linked, offline HTML pages sharing one set of assets.

    Writes ``index.html`` + ``overview.html`` + one ``repo-<slug>.html`` per repo
    that has parsed nodes, plus a single shared ``cytoscape.min.js`` / ``app.css`` /
    ``app.js`` (referenced, not inlined — so the folder stays small instead of
    repeating ~1 MB per page). Overview repo nodes link to their repo page; every
    page links back to the overview + index. Fully offline.

    ``repos`` is an optional list of filter patterns (glob or substring against the
    repo id); when given, only matching repos get per-repo pages (and overview links
    to them) — the fleet overview itself still shows every repo. This keeps a scoped
    build small instead of materialising a page for all ~hundreds of repos.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("cytoscape.min.js", "app.css", "app.js"):
        (out / name).write_text(_read_static_raw(name), encoding="utf-8")

    sizes = dict(store.conn.execute(
        "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
    repos_with_nodes = sorted(r for r, c in sizes.items() if c)
    if repos:
        repos_with_nodes = [r for r in repos_with_nodes if _match_repo(r, repos)]
    pages = {r: f"repo-{repo_slug(r)}.html" for r in repos_with_nodes}

    meta: dict = {}
    nodes, edges = overview_subgraph(store, max_nodes=max_nodes, meta=meta)
    for n in nodes:
        if n["id"] in pages:
            n["href"] = pages[n["id"]]
    meta["mode"] = "overview"
    (out / "overview.html").write_text(
        to_html(to_payload(nodes, edges, meta),
                layout=overview_layout, assets="sibling", site=True,
                title="contextlake — fleet overview"), encoding="utf-8")

    # wiki pages, if the LLM-wiki has been generated (store_dir/wiki/<slug>.md)
    sp = getattr(store, "path", None)
    wiki_dir = (Path(sp).parent / "wiki") if sp else None
    wiki_pages: dict[str, str] = {}

    for r in repos_with_nodes:
        m: dict = {}
        rn, re_ = repo_subgraph(store, r, max_nodes=repo_max_nodes, meta=m)
        m.update(mode="repo", repo=r)
        (out / pages[r]).write_text(
            to_html(to_payload(rn, re_, m),
                    layout=repo_layout, assets="sibling", site=True,
                    title=f"contextlake — {r}"), encoding="utf-8")
        if wiki_dir:
            wf = wiki_dir / (repo_slug(r) + ".md")
            if wf.exists():
                wiki_pages[r] = f"wiki-{repo_slug(r)}.html"
                (out / wiki_pages[r]).write_text(
                    _wiki_page(r, wf.read_text(encoding="utf-8", errors="replace"), store),
                    encoding="utf-8")
    log(f"  wrote overview + {len(repos_with_nodes)} repo pages "
        f"+ {len(wiki_pages)} wiki pages + index")

    (out / "index.html").write_text(
        _site_index(repos_with_nodes, sizes, pages, wiki_pages), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Live server (click-to-expand)
# ---------------------------------------------------------------------------
def build_graph_server(store: Store, initial_payload: dict, *, host: str = "127.0.0.1",
                       port: int = 8765, cdn: bool = False, layout: str = "cose",
                       max_fanout: int = 50):
    """Build (but do not start) the visualizer HTTP server.

    Serves the page at ``/`` and a ``/neighbors?id=…`` JSON endpoint that returns a
    1-hop subgraph for client-side click-to-expand. Returned non-blocking so the
    CLI loop and tests can drive ``serve_forever``/``shutdown`` themselves.
    """
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page = to_html(initial_payload, cdn=cdn, live=True, layout=layout).encode("utf-8")
    # ThreadingHTTPServer serves each request on its own thread, but a SQLite
    # connection belongs to its creating thread — so open a fresh, short-lived
    # store per /neighbors request instead of sharing the caller's connection.
    store_factory, store_path = type(store), getattr(store, "path", None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # keep request logs off the console
            pass

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
                return
            if parsed.path == "/neighbors":
                q = urllib.parse.parse_qs(parsed.query)
                nid = (q.get("id") or [None])[0]
                if not nid:
                    self._send(400, "application/json", b'{"error":"id required"}')
                    return
                rel = (q.get("relation") or [None])[0]
                direction = (q.get("direction") or ["both"])[0]
                req_store = store_factory(store_path) if store_path else store
                try:
                    nodes, edges = extract_subgraph(
                        req_store, [nid], hops=1, max_nodes=200, max_fanout=max_fanout,
                        relation=rel, direction=direction)
                finally:
                    if req_store is not store:
                        req_store.close()
                body = to_json(to_payload(nodes, edges, {"mode": "expand", "seed": nid}))
                self._send(200, "application/json", body.encode("utf-8"))
                return
            self._send(404, "text/plain", b"not found")

    return ThreadingHTTPServer((host, port), Handler)


def serve_graph(store: Store, initial_payload: dict, *, host: str = "127.0.0.1",
                port: int = 8765, cdn: bool = False, layout: str = "cose",
                max_fanout: int = 50) -> None:
    """Serve the visualizer (blocking until Ctrl-C)."""
    from .. import style

    srv = build_graph_server(store, initial_payload, host=host, port=port, cdn=cdn,
                             layout=layout, max_fanout=max_fanout)
    log(style.ok(f"Graph server on http://{host}:{port}  (Ctrl-C to stop)"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Stopping graph server")
    finally:
        srv.shutdown()


def build_site_server(store: Store, *, host: str = "127.0.0.1", port: int = 8765,
                      max_nodes: int = 5000, repo_max_nodes: int = 500,
                      overview_layout: str = "concentric", repo_layout: str = "cose",
                      max_fanout: int = 50):
    """Build (don't start) a server that serves the cross-linked site *lazily*.

    Same pages as ``build_site`` — ``/`` (overview), ``/repo-<slug>.html`` (a repo's
    internal graph), ``/index.html`` — but each repo page is rendered on demand from
    the store instead of being materialised up front, so nothing inlines the whole
    fleet. Shared ``app.css`` / ``app.js`` / ``cytoscape.min.js`` are served once
    (browser-cached); ``/neighbors`` keeps click-to-expand inside a repo view.
    """
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    sizes = dict(store.conn.execute(
        "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
    repos_with_nodes = sorted(r for r, c in sizes.items() if c)
    pages = {r: f"repo-{repo_slug(r)}.html" for r in repos_with_nodes}
    slug_to_repo = {repo_slug(r): r for r in repos_with_nodes}

    meta: dict = {"mode": "overview"}
    ov_nodes, ov_edges = overview_subgraph(store, max_nodes=max_nodes, meta=meta)
    for n in ov_nodes:
        if n["id"] in pages:
            n["href"] = pages[n["id"]]
    overview_html = to_html(to_payload(ov_nodes, ov_edges, meta), layout=overview_layout,
                            assets="sibling", site=True, live=True,
                            title="contextlake — fleet overview").encode("utf-8")
    index_html = _site_index(repos_with_nodes, sizes, pages).encode("utf-8")
    assets = {"app.css": (_read_static_raw("app.css"), "text/css"),
              "app.js": (_read_static_raw("app.js"), "application/javascript"),
              "cytoscape.min.js": (_read_static_raw("cytoscape.min.js"), "application/javascript")}
    store_factory, store_path = type(store), getattr(store, "path", None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - stdlib handler name
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.lstrip("/")
            if path in ("", "overview.html"):
                self._send(200, "text/html; charset=utf-8", overview_html)
                return
            if path == "index.html":
                self._send(200, "text/html; charset=utf-8", index_html)
                return
            if path in assets:
                text, ctype = assets[path]
                self._send(200, ctype + "; charset=utf-8", text.encode("utf-8"))
                return
            if path.startswith("repo-") and path.endswith(".html"):
                repo = slug_to_repo.get(path[len("repo-"):-len(".html")])
                if not repo:
                    self._send(404, "text/plain", b"unknown repo")
                    return
                req = store_factory(store_path) if store_path else store
                try:
                    m: dict = {"mode": "repo", "repo": repo}
                    rn, re_ = repo_subgraph(req, repo, max_nodes=repo_max_nodes, meta=m)
                    body = to_html(to_payload(rn, re_, m), layout=repo_layout,
                                   assets="sibling", site=True, live=True,
                                   title=f"contextlake — {repo}").encode("utf-8")
                finally:
                    if req is not store:
                        req.close()
                self._send(200, "text/html; charset=utf-8", body)
                return
            if parsed.path == "/neighbors":
                q = urllib.parse.parse_qs(parsed.query)
                nid = (q.get("id") or [None])[0]
                if not nid:
                    self._send(400, "application/json", b'{"error":"id required"}')
                    return
                req = store_factory(store_path) if store_path else store
                try:
                    nodes, edges = extract_subgraph(
                        req, [nid], hops=1, max_nodes=200, max_fanout=max_fanout,
                        relation=(q.get("relation") or [None])[0],
                        direction=(q.get("direction") or ["both"])[0])
                finally:
                    if req is not store:
                        req.close()
                body = to_json(to_payload(nodes, edges, {"mode": "expand", "seed": nid}))
                self._send(200, "application/json", body.encode("utf-8"))
                return
            self._send(404, "text/plain", b"not found")

    return ThreadingHTTPServer((host, port), Handler)


def serve_site(store: Store, *, host: str = "127.0.0.1", port: int = 8765,
               **kwargs) -> None:
    """Serve the lazy cross-linked site (blocking until Ctrl-C)."""
    from .. import style

    srv = build_site_server(store, host=host, port=port, **kwargs)
    log(style.ok(f"Graph site on http://{host}:{port}  (Ctrl-C to stop)"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Stopping graph site server")
    finally:
        srv.shutdown()


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
__STYLE_BLOCK__
__LIB_TAG__
</head>
<body data-theme="light" data-sidebar="open" data-inspect="closed">
<div id="app">
  <header id="topbar">
    <button class="ibtn" id="navToggle" title="Toggle sidebar" aria-label="Toggle sidebar"><svg
      viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path
      d="M2 4h12M2 8h12M2 12h12"/></svg></button>
    __GLYPH__
    <span class="wm">context<span class="l">lake</span></span>
    <span id="mode"></span>
    <span class="grow"></span>
    <div class="tsearch"><svg class="si" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      stroke-width="1.6"><circle cx="7" cy="7" r="4.5"/><path d="M11 11l3 3"/></svg>
      <input id="search" type="search" placeholder="Search nodes…" autocomplete="off"
        aria-label="Search nodes"></div>
    <button class="ibtn" id="theme" title="Toggle dark mode" aria-label="Toggle dark mode"><svg
      viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8"
      r="3.2"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6L13 13M13 3l-1.4 1.4M4.4
      11.6L3 13"/></svg></button>
  </header>
  <aside id="panel" role="complementary" aria-label="Controls">
    <div class="sgroup"><h2>View</h2>
      <div class="row" id="viewmodes" hidden>
        <div class="seg" role="tablist" aria-label="Overview mode">
          <button class="segbtn on" id="vm-clusters" role="tab" aria-selected="true"
            title="Namespace clusters — the repo tree, drill in on click">Namespace</button>
          <button class="segbtn" id="vm-flow" role="tab" aria-selected="false"
            title="Dependency clusters — connected repos grouped by what they depend on"
            >Dependencies</button>
        </div>
      </div>
      <div class="row">
        <label>layout <select id="layout" aria-label="Layout">__LAYOUT_OPTIONS__</select></label>
        <button class="ibtn" id="fit" title="Fit to view" aria-label="Fit to view"><svg
          viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path
          d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"/></svg></button>
        <button class="ibtn" id="reset" title="Reset view &amp; filters" aria-label="Reset"><svg
          viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path
          d="M13 8a5 5 0 1 1-1.5-3.5M13 2v3h-3"/></svg></button>
        <button class="btn primary" id="png" title="Save a PNG snapshot"><svg viewBox="0 0 16 16"
          fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2v8M5 7l3 3 3-3M3
          13h10"/></svg>PNG</button>
      </div>
      <label class="tog" id="nodeprow" hidden><input type="checkbox" id="shownodeps">
        show repos with no detected dependency <span id="nodepn" class="cnt"></span></label>
    </div>
    <div class="sgroup"><h2>Nodes</h2><div id="legend">__LEGEND__</div></div>
    <div class="sgroup"><h2>Relationships</h2><div id="edgelegend">__EDGE_LEGEND__</div></div>
    __LEGEND_KEYS__
  </aside>
  <main id="cy" role="application" aria-label="Knowledge graph" tabindex="0">
    <div id="empty"><div class="et">No nodes in this view</div>
      <div>Widen the seed, raise <code>--max-nodes</code>, or clear filters.</div></div>
    <canvas id="minimap" width="180" height="130" aria-hidden="true"
      title="Overview map — click or drag to navigate"></canvas>
  </main>
  <aside id="info" role="complementary" aria-label="Details"></aside>
  <footer id="statusbar" role="status" aria-live="polite">
    <span id="meta"></span>
    <span id="trunc" class="trunc"></span><span class="grow"></span>
    <span>context<span class="l">lake</span> graph</span>
  </footer>
</div>
<div id="tip" role="tooltip"></div>
<script>
  var ELEMENTS = __ELEMENTS__;
  var COLORS = __COLORS__;
  var ICONS = __ICONS__;
  var LANG_ICONS = __LANG_ICONS__;
  var DEFAULT_COLOR = "__DEFAULT_COLOR__";
  var REL_COLORS = __REL_COLORS__;
  var DEFAULT_EDGE_COLOR = "__DEFAULT_EDGE_COLOR__";
  var CONF_META = __CONF_META__;
  var META = __META__;
  var LIVE = __LIVE__;
  var LAYOUT = "__LAYOUT__";
  var SITE = __SITE__;
__APP_JS_BLOCK__
</body>
</html>
"""
