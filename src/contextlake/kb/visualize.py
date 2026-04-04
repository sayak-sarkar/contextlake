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
from typing import TYPE_CHECKING

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
}
DEFAULT_COLOR = "#c9c9c9"
# Relation -> edge hue (within the brand family; greys for structural relations).
# Open vocabulary: unknown relations fall back to DEFAULT_EDGE_COLOR.
RELATION_COLORS = {
    "calls": "#137A8B", "imports": "#2BB3A3", "contains": "#9fb4b8",
    "depends_on": "#E7B53C", "publishes": "#D7C5A0", "tracked_by": "#577590",
    "documented_by": "#9d4edd",
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
    from .arch.resolve import repo_dependency_edges
    sizes = dict(store.conn.execute(
        "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
    log("  resolving real cross-repo dependencies (package two-hop)…")
    dep_edges = repo_dependency_edges(store)

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
    nodes = [{"id": r, "repo": r, "kind": "repo", "name": r, "qualified_name": None,
              "file": None, "line": None, "lang": None,
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
            "count": attrs.get("node_count"),
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
            layout: str = "cose", title: str = "contextlake graph") -> str:
    """A single self-contained HTML page rendering the subgraph with cytoscape.js.

    Default inlines the vendored lib so the file works offline / air-gapped; pass
    ``cdn=True`` for a small online-only file. ``live=True`` wires node taps to a
    ``/neighbors`` endpoint for click-to-expand (used by ``serve_graph``).
    """
    if cdn:
        lib_tag = f'<script src="{_CDN_URL}"></script>'
    else:
        lib_tag = f"<script>{_cytoscape_js()}</script>"
    from collections import Counter
    elements = json.dumps(_cytoscape_elements(payload))
    colors = json.dumps(KIND_COLORS)
    kind_counts = Counter(n.get("kind", "") for n in payload["nodes"])
    legend = "".join(
        f'<button type="button" class="lg" data-kind="{k}">'
        f'<i style="background:{c}"></i><span class="lbl">{k}</span>'
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
    options = "".join(f'<option value="{n}">{n}</option>' for n in LAYOUTS)
    meta = json.dumps(payload.get("meta", {}))
    return (_HTML_TEMPLATE
            .replace("__APP_CSS__", _app_css())
            .replace("__APP_JS__", _app_js())
            .replace("__TITLE__", title)
            .replace("__LIB_TAG__", lib_tag)
            .replace("__ELEMENTS__", elements)
            .replace("__COLORS__", colors)
            .replace("__DEFAULT_COLOR__", DEFAULT_COLOR)
            .replace("__REL_COLORS__", json.dumps(RELATION_COLORS))
            .replace("__DEFAULT_EDGE_COLOR__", DEFAULT_EDGE_COLOR)
            .replace("__CONF_META__", json.dumps(CONF_META))
            .replace("__LEGEND__", legend)
            .replace("__EDGE_LEGEND__", edge_legend)
            .replace("__LAYOUT_OPTIONS__", options)
            .replace("__GLYPH__", _GLYPH_SVG)
            .replace("__META__", meta)
            .replace("__LAYOUT__", layout if layout in LAYOUTS else "cose")
            .replace("__LIVE__", "true" if live else "false"))


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


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__APP_CSS__</style>
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
  </aside>
  <main id="cy" role="application" aria-label="Knowledge graph" tabindex="0">
    <div id="empty"><div class="et">No nodes in this view</div>
      <div>Widen the seed, raise <code>--max-nodes</code>, or clear filters.</div></div>
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
  var DEFAULT_COLOR = "__DEFAULT_COLOR__";
  var REL_COLORS = __REL_COLORS__;
  var DEFAULT_EDGE_COLOR = "__DEFAULT_EDGE_COLOR__";
  var CONF_META = __CONF_META__;
  var META = __META__;
  var LIVE = __LIVE__;
  var LAYOUT = "__LAYOUT__";

  __APP_JS__</script>
</body>
</html>
"""
