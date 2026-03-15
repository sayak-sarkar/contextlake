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
                     direction: str = "both") -> tuple[list[Node], list[Edge]]:
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
    return nodes, edges


def repo_subgraph(store: Store, repo_id: str, *, max_nodes: int = 500
                  ) -> tuple[list[Node], list[Edge]]:
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
    return nodes, edges


def overview_subgraph(store: Store, *, max_nodes: int = 5000
                      ) -> tuple[list[dict], list[dict]]:
    """Repos-as-nodes with aggregated cross-repo edges (the architecture map).

    Edges carry no per-endpoint repo column, so we double-join through ``nodes``
    on ``src``/``dst``. The ``cross_repo`` flag is deliberately NOT used: it is
    populated order-dependently during indexing (it can be *partially* set), so
    trusting it would silently under-report. We always run the authoritative
    ``repo != repo`` scan — slower, but complete.
    """
    sizes = dict(store.conn.execute(
        "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
    log("  aggregating cross-repo edges…")
    rows = store.conn.execute(
        "SELECT ns.repo_id, nd.repo_id, e.relation, COUNT(*) "
        "FROM edges e "
        "JOIN nodes ns ON ns.node_id = e.src "
        "JOIN nodes nd ON nd.node_id = e.dst "
        "WHERE ns.repo_id != nd.repo_id "
        "GROUP BY ns.repo_id, nd.repo_id, e.relation"
    ).fetchall()

    degree: dict[str, int] = {}
    raw_edges = []
    for src_repo, dst_repo, rel, cnt in rows:
        degree[src_repo] = degree.get(src_repo, 0) + cnt
        degree[dst_repo] = degree.get(dst_repo, 0) + cnt
        raw_edges.append((src_repo, dst_repo, rel, cnt))

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
    edges = [{"src": s, "dst": d, "relation": rel, "confidence": "EXTRACTED", "weight": cnt}
             for (s, d, rel, cnt) in raw_edges if s in keep and d in keep]
    if truncated:
        log(f"  {len(ranked)} repos have cross-repo edges; showing the {max_nodes} most "
            f"connected (raise --max-nodes to see more)")
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
    elements = json.dumps(_cytoscape_elements(payload))
    colors = json.dumps(KIND_COLORS)
    legend = "".join(
        f'<span class="lg" data-kind="{k}"><i style="background:{c}"></i>{k}</span>'
        for k, c in KIND_COLORS.items())
    # edge legend = relations actually present (known hues first, then open-vocab)
    present = {e.get("relation") for e in payload["edges"]} - {None, ""}
    known = [r for r in RELATION_COLORS if r in present]
    rel_order = known + sorted(present - set(RELATION_COLORS))
    edge_legend = "".join(
        f'<span class="lg rel" data-rel="{r}">'
        f'<i style="background:{RELATION_COLORS.get(r, DEFAULT_EDGE_COLOR)}"></i>{r}</span>'
        for r in rel_order)
    options = "".join(f'<option value="{n}">{n}</option>' for n in LAYOUTS)
    meta = json.dumps(payload.get("meta", {}))
    return (_HTML_TEMPLATE
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
<style>
  :root{
    --deepwater:#0E2A33;--lake:#137A8B;--current:#2BB3A3;--mist:#EAF4F4;
    --shore:#D7C5A0;--sun:#E7B53C;
    --ink:#0E2A33;--muted:#5b7177;--line:#d6e4e6;--card:#ffffffe6;
    --elev:0 6px 24px -8px rgba(14,42,51,.28);
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;color:var(--ink);
    font-family:"Space Grotesk",system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  #cy{position:absolute;inset:0;
    background:radial-gradient(120% 90% at 50% -10%,#ffffff 0%,#f1fafb 45%,#e3f1f2 100%)}
  #topbar{position:fixed;top:0;left:0;right:0;z-index:15;height:48px;display:flex;
    align-items:center;gap:10px;padding:0 14px;
    background:linear-gradient(180deg,#fffffff2,#ffffffd9);
    border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}
  #topbar .glyph{width:26px;height:26px;border-radius:8px;display:block;
    box-shadow:0 1px 4px rgba(14,42,51,.25)}
  .wm{font-size:16px;font-weight:600;letter-spacing:-.01em;color:var(--deepwater)}
  .wm .l{color:var(--lake)}
  #topbar .mode{font-size:12px;color:var(--muted);padding:2px 9px;
    border:1px solid var(--line);border-radius:999px;background:var(--mist)}
  #topbar .tag{margin-left:auto;font-size:12px;color:var(--lake);font-weight:500}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
    box-shadow:var(--elev);backdrop-filter:blur(6px)}
  #panel{position:absolute;top:60px;left:12px;z-index:10;width:300px;max-width:46vw;
    padding:12px 14px;font-size:12px}
  #panel h1{font-size:11px;font-weight:600;margin:0 0 9px;color:var(--lake);
    letter-spacing:.06em;text-transform:uppercase}
  #search{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:10px;
    font-size:12px;margin-bottom:10px;background:var(--mist);
    transition:box-shadow .15s,border-color .15s}
  #search:focus{outline:none;border-color:var(--lake);
    box-shadow:0 0 0 3px rgba(43,179,163,.25)}
  .row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
  label{font-size:11px;color:var(--muted)}
  button,select{font:inherit;font-size:12px;padding:6px 10px;border:1px solid var(--line);
    border-radius:9px;background:#fff;color:var(--deepwater);cursor:pointer;
    transition:background .15s,box-shadow .15s,transform .05s}
  button:hover{background:var(--mist)}
  button:active{transform:translateY(1px)}
  button.primary{background:var(--lake);border-color:var(--lake);color:#fff;
    box-shadow:0 2px 8px -2px rgba(19,122,139,.6)}
  button.primary:hover{background:var(--current);border-color:var(--current)}
  #legend{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}
  .lg{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;
    border:1px solid var(--line);background:#fff;cursor:pointer;user-select:none;
    transition:background .12s,box-shadow .12s}
  .lg:hover{box-shadow:0 1px 6px -2px rgba(14,42,51,.3)}
  .lg i{width:9px;height:9px;border-radius:50%;display:inline-block}
  .lg.off{opacity:.4;text-decoration:line-through}
  #meta{color:var(--muted)}
  #info{position:absolute;bottom:34px;left:12px;z-index:10;width:320px;max-width:46vw;
    padding:12px 14px;font-size:12px;display:none}
  #info h2{font-size:13px;margin:0 0 2px;color:var(--deepwater);word-break:break-all}
  #info dl{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;margin:8px 0 0;
    border-top:1px solid var(--line);padding-top:8px}
  #info dt{color:var(--lake);font-weight:500}
  #info dd{margin:0;word-break:break-all}
  #info .hint{color:var(--muted);margin-top:8px;font-style:italic}
  #tip{position:absolute;z-index:20;pointer-events:none;display:none;
    background:var(--deepwater);color:var(--mist);font-size:11px;padding:4px 8px;
    border-radius:7px;max-width:42ch;box-shadow:0 4px 14px -4px rgba(14,42,51,.5)}
  #footer{position:fixed;bottom:8px;right:12px;z-index:10;font-size:11px;color:var(--muted)}
  #footer .l{color:var(--lake)}
  .leg-h{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
    margin:4px 0 5px;font-weight:600}
  #edgelegend{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}
  .lg.rel i{border-radius:1px;width:12px;height:3px}
  .rel-chip{display:inline-block;padding:2px 9px;border-radius:999px;color:#fff;
    font-size:11px;font-weight:600}
  #info .edge-flow{font-size:11px;color:var(--muted);margin:5px 0 2px;word-break:break-all}
  #info .trust{display:flex;align-items:center;gap:6px;margin:7px 0 2px;font-size:11px}
  #info .trust .dot{width:9px;height:9px;border-radius:50%;display:inline-block}
  #info .trust .blurb{color:var(--muted)}
  #info .copy-prov{margin-top:9px;font-size:11px;padding:4px 9px}
</style>
__LIB_TAG__
</head>
<body>
<div id="topbar">
  __GLYPH__
  <span class="wm">context<span class="l">lake</span></span>
  <span class="mode" id="mode"></span>
  <span class="tag">All your real context, in one local lake.</span>
</div>
<div id="panel" class="card">
  <h1>Explore</h1>
  <input id="search" type="search" placeholder="search nodes by name…" autocomplete="off">
  <div class="row">
    <label>layout <select id="layout">__LAYOUT_OPTIONS__</select></label>
    <button id="fit" title="Fit graph to view">Fit</button>
    <button id="reset" title="Clear selection &amp; filters">Reset</button>
    <button id="png" class="primary" title="Save a PNG snapshot">PNG</button>
  </div>
  <div class="leg-h">Nodes</div>
  <div id="legend">__LEGEND__</div>
  <div class="leg-h">Relationships</div>
  <div id="edgelegend">__EDGE_LEGEND__</div>
  <div id="meta"></div>
</div>
<div id="info" class="card"></div>
<div id="tip"></div>
<div id="footer">context<span class="l">lake</span> graph</div>
<div id="cy"></div>
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

  function edgeColor(e){ return REL_COLORS[e.data("relation")] || DEFAULT_EDGE_COLOR; }

  var cy = cytoscape({
    container: document.getElementById("cy"),
    elements: ELEMENTS,
    wheelSensitivity: 0.2,
    style: [
      { selector: "node", style: {
          "background-color": function(n){ return COLORS[n.data("kind")] || DEFAULT_COLOR; },
          "label": "data(label)", "font-size": 9, "color": "#0E2A33",
          "width": "mapData(deg, 0, 24, 14, 52)", "height": "mapData(deg, 0, 24, 14, 52)",
          "text-wrap": "ellipsis", "text-max-width": 120,
          "text-valign": "bottom", "text-margin-y": 2,
          "border-width": 0.5, "border-color": "#fff" } },
      // edges: relation -> hue, confidence -> line-style + opacity, weight -> thickness
      { selector: "edge", style: {
          "line-color": edgeColor, "target-arrow-color": edgeColor,
          "width": "mapData(weight, 1, 10, 0.8, 4.5)",
          "target-arrow-shape": "triangle", "arrow-scale": 0.7, "curve-style": "bezier" } },
      { selector: 'edge[confidence = "EXTRACTED"]',
        style: { "line-style": "solid", "opacity": 0.7 } },
      { selector: 'edge[confidence = "INFERRED"]',
        style: { "line-style": "dashed", "opacity": 0.55 } },
      { selector: 'edge[confidence = "AMBIGUOUS"]',
        style: { "line-style": "dotted", "opacity": 0.45 } },
      { selector: ".faded", style: { "opacity": 0.05, "text-opacity": 0 } },
      { selector: "node.hi", style: { "background-color": "#0E2A33", "color": "#0E2A33",
          "z-index": 99, "border-width": 2, "border-color": "#2BB3A3" } },
      { selector: "node.found", style: { "border-width": 4, "border-color": "#E7B53C",
          "z-index": 100 } },
      // edge labels are hidden by default (clutter); shown only when highlighted
      { selector: "edge.hi", style: { "width": 2.2, "opacity": 1,
          "label": "data(relation)", "font-size": 7, "color": "#0E2A33",
          "text-rotation": "autorotate", "text-background-color": "#EAF4F4",
          "text-background-opacity": 0.9, "z-index": 99 } }
    ],
    layout: { name: "preset" }
  });

  cy.nodes().forEach(function(n){ n.data("deg", n.degree(false)); });
  document.getElementById("mode").textContent = META.mode || "graph";
  document.getElementById("meta").textContent =
    cy.nodes().length + " nodes \\u00b7 " + cy.edges().length + " edges";

  function layoutOpts(name){
    if(name === "cose") return { name:"cose", animate:false, randomize:true, padding:40,
        nodeOverlap:24, componentSpacing:140, gravity:0.2, numIter:1500,
        nodeRepulsion:function(){ return 14000; },
        idealEdgeLength:function(){ return 120; }, edgeElasticity:function(){ return 80; } };
    if(name === "concentric") return { name:"concentric", animate:false, padding:40,
        minNodeSpacing:28, concentric:function(n){ return n.degree(false); },
        levelWidth:function(){ return 2; } };
    if(name === "breadthfirst") return { name:"breadthfirst", animate:false, padding:40,
        spacingFactor:1.5, circle:false };
    if(name === "circle") return { name:"circle", animate:false, padding:40, spacingFactor:1.3 };
    if(name === "grid") return { name:"grid", animate:false, padding:40, avoidOverlap:true,
        avoidOverlapPadding:24 };
    return { name:name, animate:false };
  }
  function runLayout(name){ cy.layout(layoutOpts(name)).run(); cy.fit(undefined, 30); }
  runLayout(LAYOUT);

  var sel = document.getElementById("layout");
  sel.value = LAYOUT;
  sel.addEventListener("change", function(){ runLayout(sel.value); });

  // toolbar
  document.getElementById("fit").onclick = function(){ cy.fit(undefined, 30); };
  document.getElementById("png").onclick = function(){
    var uri = cy.png({ full:true, scale:2, bg:"#ffffff" });
    var a = document.createElement("a");
    a.href = uri; a.download = "contextlake-graph.png"; a.click();
  };
  document.getElementById("reset").onclick = function(){
    cy.elements().removeClass("faded hi found");
    hidden = {}; hiddenRel = {}; applyFilter(); syncLegend();
    document.getElementById("search").value = "";
    hideInfo(); cy.fit(undefined, 30);
  };

  // legends = kind filter (nodes) + relationship filter (edges)
  var hidden = {}, hiddenRel = {};
  function applyFilter(){
    cy.nodes().forEach(function(n){
      n.style("display", hidden[n.data("kind")] ? "none" : "element");
    });
    cy.edges().forEach(function(e){
      e.style("display", hiddenRel[e.data("relation")] ? "none" : "element");
    });
  }
  function syncLegend(){
    document.querySelectorAll("#legend .lg").forEach(function(el){
      el.classList.toggle("off", !!hidden[el.getAttribute("data-kind")]);
    });
    document.querySelectorAll("#edgelegend .lg").forEach(function(el){
      el.classList.toggle("off", !!hiddenRel[el.getAttribute("data-rel")]);
    });
  }
  document.querySelectorAll("#legend .lg").forEach(function(el){
    el.addEventListener("click", function(){
      var k = el.getAttribute("data-kind");
      hidden[k] = !hidden[k]; applyFilter(); syncLegend();
    });
  });
  document.querySelectorAll("#edgelegend .lg").forEach(function(el){
    el.addEventListener("click", function(){
      var r = el.getAttribute("data-rel");
      hiddenRel[r] = !hiddenRel[r]; applyFilter(); syncLegend();
    });
  });

  // search -> highlight + frame matches
  var search = document.getElementById("search");
  search.addEventListener("input", function(){
    var q = search.value.trim().toLowerCase();
    cy.nodes().removeClass("found");
    if(!q) return;
    var hits = cy.nodes().filter(function(n){
      return (n.data("label")||"").toLowerCase().indexOf(q) >= 0
          || (n.data("qn")||"").toLowerCase().indexOf(q) >= 0;
    });
    hits.addClass("found");
    if(hits.length){ cy.animate({ fit:{ eles:hits, padding:90 } }, { duration:300 }); }
  });

  // hover tooltip
  var tip = document.getElementById("tip");
  cy.on("mouseover", "node", function(e){
    var n = e.target;
    tip.textContent = (n.data("label")||"") + "  \\u00b7  " + (n.data("kind")||"");
    tip.style.display = "block";
  });
  cy.on("mousemove", function(e){
    if(tip.style.display === "block"){
      tip.style.left = (e.renderedPosition.x + 12) + "px";
      tip.style.top  = (e.renderedPosition.y + 12) + "px";
    }
  });
  cy.on("mouseout", "node", function(){ tip.style.display = "none"; });
  cy.on("mouseover", "edge", function(e){
    var d = e.target.data();
    var prov = d.prov_file
      ? "  \\u00b7  " + d.prov_file + (d.prov_line ? ":" + d.prov_line : "") : "";
    tip.textContent = d.relation + "  \\u00b7  " + d.confidence + prov;
    tip.style.display = "block";
  });
  cy.on("mouseout", "edge", function(){ tip.style.display = "none"; });

  // selection -> focus + detail panel (nodes AND edges)
  var info = document.getElementById("info");
  function esc(s){ return (s == null ? "" : ("" + s)).replace(/[&<>"]/g, function(c){
    return { "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]; }); }
  function row(k, v){
    return (v === undefined || v === null || v === "")
      ? "" : "<dt>" + k + "</dt><dd>" + esc(v) + "</dd>";
  }
  function showInfo(n){
    var d = n.data();
    var fileline = d.file ? (d.file + (d.line ? ":" + d.line : "")) : "";
    info.innerHTML = "<h2>" + esc(d.label || d.id) + "</h2><dl>"
      + row("kind", d.kind) + row("repo", d.repo) + row("qualified", d.qn)
      + row("file", fileline) + row("nodes", d.count) + row("degree", d.deg) + "</dl>"
      + (LIVE ? '<div class="hint">click again to expand neighbours</div>' : "");
    info.style.display = "block";
  }
  function showEdgeInfo(ed){
    var d = ed.data();
    var c = CONF_META[d.confidence] || CONF_META.EXTRACTED;  // [label, dot, blurb]
    var hue = REL_COLORS[d.relation] || DEFAULT_EDGE_COLOR;
    var sN = cy.getElementById(d.source), tN = cy.getElementById(d.target);
    var prov = d.prov_file ? (d.prov_file + (d.prov_line ? ":" + d.prov_line : "")) : "";
    info.innerHTML =
      '<h2><span class="rel-chip" style="background:' + hue + '">'
      + esc(d.relation) + "</span></h2>"
      + '<div class="edge-flow">' + esc(sN.data("label"))
      + " \\u2192 " + esc(tN.data("label")) + "</div>"
      + '<div class="trust"><span class="dot" style="background:' + c[1] + '"></span>'
      + "<b>" + esc(c[0]) + "</b><span class=\\"blurb\\">" + esc(c[2]) + "</span></div>"
      + "<dl>" + row("context", d.context) + row("weight", d.weight)
      + row("source", prov) + row("verified", d.verified_at) + "</dl>"
      + (prov ? '<button class="copy-prov" data-prov="' + esc(prov)
                + '">copy file:line</button>' : "");
    info.style.display = "block";
  }
  info.addEventListener("click", function(ev){
    var b = ev.target.closest && ev.target.closest(".copy-prov");
    if(b && navigator.clipboard){ navigator.clipboard.writeText(b.getAttribute("data-prov")); }
  });
  function hideInfo(){ info.style.display = "none"; }

  function focus(node){
    cy.elements().addClass("faded").removeClass("hi");
    node.closedNeighborhood().removeClass("faded").addClass("hi");
  }
  cy.on("tap", function(e){
    if(e.target === cy){ cy.elements().removeClass("faded hi"); hideInfo(); }
  });
  cy.on("tap", "node", function(e){
    focus(e.target); showInfo(e.target);
    if(LIVE){ expand(e.target.id()); }
  });
  cy.on("tap", "edge", function(e){
    var ed = e.target;
    cy.elements().addClass("faded").removeClass("hi");
    ed.connectedNodes().add(ed).removeClass("faded").addClass("hi");
    showEdgeInfo(ed);
  });

  function expand(id){
    fetch("/neighbors?id=" + encodeURIComponent(id) + "&direction=both")
      .then(function(r){ return r.json(); })
      .then(function(p){
        var added = [];
        p.nodes.forEach(function(n){
          if(cy.getElementById(n.id).empty()){
            added.push({ group:"nodes", data:{ id:n.id, label:(n.name||n.id),
              kind:(n.kind||""), repo:(n.repo||""), qn:(n.qualified_name||""),
              file:(n.file||""), line:n.line } });
          }
        });
        p.edges.forEach(function(ed){
          var eid = ed.src + "->" + ed.dst + ":" + ed.relation;
          if(cy.getElementById(eid).empty()){
            added.push({ group:"edges", data:{ id:eid, source:ed.src, target:ed.dst,
              relation:ed.relation, confidence:(ed.confidence||"EXTRACTED"),
              context:(ed.context||""), weight:(ed.weight==null?1.0:ed.weight),
              prov_file:(ed.prov_file||""), prov_line:ed.prov_line,
              verified_at:(ed.verified_at||"") } });
          }
        });
        if(added.length){
          cy.add(added);
          cy.nodes().forEach(function(n){ n.data("deg", n.degree(false)); });
          applyFilter();
          runLayout(sel.value || LAYOUT);
        }
      })
      .catch(function(){ /* offline / endpoint absent — ignore */ });
  }
</script>
</body>
</html>
"""
