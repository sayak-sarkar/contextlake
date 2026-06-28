"""Local HTTP server for the knowledge-system dashboard.

Mirrors ``visualize.build_site_server``: a stdlib ``ThreadingHTTPServer`` that opens
**one short-lived ``Store`` per request** (a SQLite connection belongs to its creating
thread, and the server threads each request). It serves:

* the SPA shell at ``/`` and its assets (``/dashboard.js`` / ``/dashboard.css``) from
  the vendored ``static/`` folder via ``importlib.resources``;
* a small JSON API under ``/api/*`` backed by :mod:`.data`;
* the existing cytoscape graph pages under ``/graph/*`` (rendered self-contained via
  ``visualize.to_html(assets="inline", live=True)``) plus the ``/neighbors`` endpoint
  the graph page fetches for click-to-expand.

Read-only (v1): no clone/sync/MCP-management routes.
"""

from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..store.sqlite_store import SqliteStore
from . import data as kbdata


def _static(name: str) -> str:
    from importlib.resources import files
    return (files("contextlake.kb.dashboard") / "static" / name).read_text(encoding="utf-8")


def _json_bytes(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def build_dashboard_server(store, store_dir, *, host: str = "127.0.0.1", port: int = 8765):
    """Build (but do not start) the dashboard HTTP server.

    Returned non-blocking so the CLI loop and tests drive ``serve_forever`` /
    ``shutdown`` themselves. ``store`` is used only to render the one-time graph
    overview at build time (main thread); every request opens its own store.
    """
    from .. import visualize as viz

    store_dir = Path(store_dir)
    store_factory, store_path = type(store), getattr(store, "path", None)

    shell = _static("dashboard.html")
    assets = {
        "dashboard.js": (_static("dashboard.js"), "application/javascript"),
        "dashboard.css": (_static("dashboard.css"), "text/css"),
    }

    # The cross-linked graph pages reuse the cytoscape visualizer. Build the fleet
    # overview once (self-contained, live=True for click-to-expand); repo pages are
    # rendered on demand. Repo nodes link to ``repo-<slug>.html`` (resolved under
    # /graph/ in the iframe).
    sizes = dict(store.conn.execute(
        "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
    repos_with_nodes = sorted(r for r, c in sizes.items() if c)
    pages = {r: f"repo-{viz.repo_slug(r)}.html" for r in repos_with_nodes}
    slug_to_repo = {viz.repo_slug(r): r for r in repos_with_nodes}

    ov_meta: dict = {"mode": "overview"}
    ov_nodes, ov_edges = viz.overview_subgraph(store, meta=ov_meta)
    for n in ov_nodes:
        if n["id"] in pages:
            n["href"] = pages[n["id"]]
    overview_html = viz.to_html(
        viz.to_payload(ov_nodes, ov_edges, ov_meta), assets="inline", live=True,
        layout="concentric", title="contextlake — fleet overview").encode("utf-8")

    def _open_store():
        return store_factory(store_path) if store_path else store

    def _neighbors(query: str) -> tuple[int, bytes]:
        q = urllib.parse.parse_qs(query)
        nid = (q.get("id") or q.get("node") or [None])[0]
        if not nid:
            return 400, b'{"error":"id required"}'
        req = _open_store()
        try:
            nodes, edges = viz.extract_subgraph(
                req, [nid], hops=1, max_nodes=200, max_fanout=50,
                relation=(q.get("relation") or [None])[0],
                direction=(q.get("direction") or ["both"])[0])
        finally:
            if req is not store:
                req.close()
        return 200, viz.to_json(
            viz.to_payload(nodes, edges, {"mode": "expand", "seed": nid})).encode("utf-8")

    def _graph(path: str, query: str) -> tuple[int, str, bytes]:
        """Serve a self-contained cytoscape page under /graph/*."""
        leaf = path[len("/graph/"):]
        if leaf in ("", "overview", "overview.html", "index.html"):
            return 200, "text/html; charset=utf-8", overview_html
        if leaf == "neighbors":
            code, body = _neighbors(query)
            return code, "application/json", body
        if leaf.startswith("repo-"):
            slug = leaf[len("repo-"):]
            if slug.endswith(".html"):
                slug = slug[:-len(".html")]
            repo = slug_to_repo.get(slug)
            if not repo:
                return 404, "text/plain", b"unknown repo"
            req = _open_store()
            try:
                m: dict = {"mode": "repo", "repo": repo}
                rn, re_ = viz.repo_subgraph(req, repo, meta=m)
                body = viz.to_html(viz.to_payload(rn, re_, m), assets="inline", live=True,
                                   layout="cose", title=f"contextlake — {repo}").encode("utf-8")
            finally:
                if req is not store:
                    req.close()
            return 200, "text/html; charset=utf-8", body
        return 404, "text/plain", b"not found"

    # /api/<name> -> (data fn, runs against a fresh store), keyed by the leading path
    def _api(path: str, query: str) -> tuple[int, bytes]:
        q = urllib.parse.parse_qs(query)
        req = _open_store()
        rp = getattr(req, "path", None)
        sd = Path(rp).parent if rp else store_dir
        try:
            if path == "/api/overview":
                depth = int((q.get("depth") or [1])[0])
                return 200, _json_bytes(kbdata.fleet_overview(req, group_depth=depth))
            if path == "/api/groups":
                depth = int((q.get("depth") or [1])[0])
                ov = kbdata.fleet_overview(req, group_depth=depth)
                return 200, _json_bytes({"groups": ov["groups"]})
            if path == "/api/health":
                return 200, _json_bytes(kbdata.health(req, sd))
            if path == "/api/impact":
                nid = (q.get("node") or q.get("id") or [None])[0]
                if not nid:
                    return 400, b'{"error":"node required"}'
                hops = int((q.get("hops") or [3])[0])
                limit = int((q.get("limit") or [100])[0])
                return 200, _json_bytes(kbdata.impact(req, nid, hops=hops, limit=limit))
            if path == "/api/search":
                query_text = (q.get("q") or [""])[0]
                if not query_text:
                    return 400, b'{"error":"q required"}'
                kind = (q.get("kind") or [None])[0]
                repo = (q.get("repo") or [None])[0]
                limit = int((q.get("limit") or [20])[0])
                # Semantic search is live-only; without a wired embedder it degrades
                # to lexical and reports semantic=false (honest, never silent).
                return 200, _json_bytes(
                    kbdata.code_search(req, query_text, kind=kind, repo=repo, limit=limit))
            if path.startswith("/api/repo/"):
                rest = path[len("/api/repo/"):]
                if rest.endswith("/rel"):
                    repo_id = urllib.parse.unquote(rest[:-len("/rel")])
                    return 200, _json_bytes(kbdata.repo_relationships(req, repo_id))
                repo_id = urllib.parse.unquote(rest)
                return 200, _json_bytes(kbdata.repo_detail(req, sd, repo_id))
            return 404, b'{"error":"not found"}'
        finally:
            if req is not store:
                req.close()

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
            path, query = parsed.path, parsed.query
            if path in ("/", "/index.html", "/dashboard.html"):
                self._send(200, "text/html; charset=utf-8", shell.encode("utf-8"))
                return
            asset = path.lstrip("/")
            if asset in assets:
                text, ctype = assets[asset]
                self._send(200, ctype + "; charset=utf-8", text.encode("utf-8"))
                return
            if path == "/neighbors":
                code, body = _neighbors(query)
                self._send(code, "application/json", body)
                return
            if path.startswith("/graph/"):
                code, ctype, body = _graph(path, query)
                self._send(code, ctype, body)
                return
            if path.startswith("/api/"):
                code, body = _api(path, query)
                self._send(code, "application/json", body)
                return
            self._send(404, "text/plain", b"not found")

    return ThreadingHTTPServer((host, port), Handler)


def serve_dashboard(store_dir, *, host: str = "127.0.0.1", port: int = 8765,
                    open_browser: bool = False) -> None:
    """Serve the dashboard (blocking until Ctrl-C)."""
    from ... import style
    from ...logging_setup import log

    store_dir = Path(store_dir)
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        srv = build_dashboard_server(store, store_dir, host=host, port=port)
        log(style.ok(f"Dashboard on http://{host}:{port}  (Ctrl-C to stop)"))
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://{host}:{port}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log("Stopping dashboard server")
        finally:
            srv.shutdown()
    finally:
        store.close()
