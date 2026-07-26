"""Live graph/site HTTP servers (click-to-expand)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...logging_setup import log
from .diagrams import to_json
from .html_render import _read_static_raw, _site_index, repo_slug, to_html
from .payload import (
    extract_subgraph,
    overview_subgraph,
    repo_node_sizes,
    repo_subgraph,
    to_payload,
)

if TYPE_CHECKING:  # avoid importing the model at call time; we only need types here
    from ..store.base import Store

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

    sizes = repo_node_sizes(store)
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

