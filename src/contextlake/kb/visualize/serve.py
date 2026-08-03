"""Live graph/site HTTP servers (click-to-expand)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...logging_setup import log
from ..http_base import LocalHttpHandler, allowed_host_headers, host_pinning_hint
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

    Shares the dashboard's request policy via :class:`kb.http_base.LocalHttpHandler`
    -- the ``Host`` pinning in particular: this server hands out the same graph
    (file paths, symbol names) over the same loopback bind, so it needs the same
    DNS-rebinding defence the dashboard has. Keeping the policy in one place is
    what stops the two servers drifting apart again.
    """
    import urllib.parse
    from http.server import ThreadingHTTPServer

    page = to_html(initial_payload, cdn=cdn, live=True, layout=layout).encode("utf-8")
    # ThreadingHTTPServer serves each request on its own thread, but a SQLite
    # connection belongs to its creating thread — so open a fresh, short-lived
    # store per /neighbors request instead of sharing the caller's connection.
    store_factory, store_path = type(store), getattr(store, "path", None)

    def _neighbors(query: str) -> tuple[int, str, bytes]:
        q = urllib.parse.parse_qs(query)
        nid = (q.get("id") or [None])[0]
        if not nid:
            return 400, "application/json", b'{"error":"id required"}'
        req_store = store_factory(store_path) if store_path else store
        try:
            nodes, edges = extract_subgraph(
                req_store, [nid], hops=1, max_nodes=200, max_fanout=max_fanout,
                relation=(q.get("relation") or [None])[0],
                direction=(q.get("direction") or ["both"])[0])
        finally:
            if req_store is not store:
                req_store.close()
        body = to_json(to_payload(nodes, edges, {"mode": "expand", "seed": nid}))
        return 200, "application/json", body.encode("utf-8")

    class Handler(LocalHttpHandler):
        allowed_hosts = allowed_host_headers(host, port)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            if self.reject_bad_host():
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self.send_bytes(200, "text/html; charset=utf-8", page)
                return
            if parsed.path == "/neighbors":
                self.send_guarded(_neighbors, parsed.query)
                return
            self.send_bytes(404, "text/plain", b"not found")

    return ThreadingHTTPServer((host, port), Handler)


def serve_graph(store: Store, initial_payload: dict, *, host: str = "127.0.0.1",
                port: int = 8765, cdn: bool = False, layout: str = "cose",
                max_fanout: int = 50) -> None:
    """Serve the visualizer (blocking until Ctrl-C)."""
    from .. import style

    srv = build_graph_server(store, initial_payload, host=host, port=port, cdn=cdn,
                             layout=layout, max_fanout=max_fanout)
    log(style.ok(f"Graph server on http://{host}:{port}  (Ctrl-C to stop)"))
    hint = host_pinning_hint(host, port)
    if hint:
        log(style.dim("  " + hint))
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

    Same shared request policy as ``build_graph_server`` above (Host pinning,
    guarded handlers) via :class:`kb.http_base.LocalHttpHandler`.
    """
    import urllib.parse
    from http.server import ThreadingHTTPServer

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

    def _repo_page(slug: str) -> tuple[int, str, bytes]:
        repo = slug_to_repo.get(slug)
        if not repo:
            return 404, "text/plain", b"unknown repo"
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
        return 200, "text/html; charset=utf-8", body

    def _neighbors(query: str) -> tuple[int, str, bytes]:
        q = urllib.parse.parse_qs(query)
        nid = (q.get("id") or [None])[0]
        if not nid:
            return 400, "application/json", b'{"error":"id required"}'
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
        return 200, "application/json", body.encode("utf-8")

    class Handler(LocalHttpHandler):
        allowed_hosts = allowed_host_headers(host, port)

        def do_GET(self):  # noqa: N802 - stdlib handler name
            if self.reject_bad_host():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.lstrip("/")
            if path in ("", "overview.html"):
                self.send_bytes(200, "text/html; charset=utf-8", overview_html)
                return
            if path == "index.html":
                self.send_bytes(200, "text/html; charset=utf-8", index_html)
                return
            if path in assets:
                text, ctype = assets[path]
                self.send_bytes(200, ctype + "; charset=utf-8", text.encode("utf-8"))
                return
            if path.startswith("repo-") and path.endswith(".html"):
                self.send_guarded(_repo_page, path[len("repo-"):-len(".html")])
                return
            if parsed.path == "/neighbors":
                self.send_guarded(_neighbors, parsed.query)
                return
            self.send_bytes(404, "text/plain", b"not found")

    return ThreadingHTTPServer((host, port), Handler)


def serve_site(store: Store, *, host: str = "127.0.0.1", port: int = 8765,
               **kwargs) -> None:
    """Serve the lazy cross-linked site (blocking until Ctrl-C)."""
    from .. import style

    srv = build_site_server(store, host=host, port=port, **kwargs)
    log(style.ok(f"Graph site on http://{host}:{port}  (Ctrl-C to stop)"))
    hint = host_pinning_hint(host, port)
    if hint:
        log(style.dim("  " + hint))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Stopping graph site server")
    finally:
        srv.shutdown()

