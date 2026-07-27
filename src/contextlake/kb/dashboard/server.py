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

Read-only by default. ``allow_mutations=True`` (``--allow-mutations`` on the CLI,
loopback-only) additionally exposes ``POST /api/repo/<id>/sync``, ``POST
/api/repo/add``, and ``POST /api/mcp/serve`` (start/stop/restart the HTTP-transport
MCP server) -- see :mod:`.mutations`. A ``BaseHTTPRequestHandler`` answering POST on
localhost is a classic CSRF-to-RCE shape (any page a browser has open can fire a
form-encoded POST at it, no preflight required), so every mutating request must
carry the per-process token minted at server-build time in a custom header --
requiring a custom header is what forces the preflight a cross-origin page can't
complete -- and the ``Host`` header must name this exact host:port, which blocks DNS
rebinding around the loopback bind.
"""

from __future__ import annotations

import hmac
import json
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..lock import StoreBusy, StoreLock
from ..store.sqlite_store import SqliteStore
from . import data as kbdata
from . import mutations as kbmut

TOKEN_HEADER = "X-Contextlake-Token"


def _static(name: str) -> str:
    from importlib.resources import files
    return (files("contextlake.kb.dashboard") / "static" / name).read_text(encoding="utf-8")


def _json_bytes(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def build_dashboard_server(store, store_dir, *, host: str = "127.0.0.1", port: int = 8765,
                           config_path: str | None = None, sample: bool = False,
                           allow_mutations: bool = False, workspace: str | None = None):
    """Build (but do not start) the dashboard HTTP server.

    Returned non-blocking so the CLI loop and tests drive ``serve_forever`` /
    ``shutdown`` themselves. ``store`` is used only to render the one-time graph
    overview at build time (main thread); every request opens its own store.

    ``config_path`` is the ``--config`` the dashboard itself was started with,
    threaded through to the MCP console / settings routes so they describe the
    config actually in effect. ``sample=True`` (the ``--sample`` demo fleet)
    makes those same routes use bare config defaults instead of resolving the
    real precedence chain -- ``load_kb_config(None)`` still merges the user's
    real ``~/.contextlake/kb.toml`` regardless of ``config_path``, which would
    leak real config into a surface billed as "nothing local is read".

    ``allow_mutations`` additionally wires ``POST /api/repo/<id>/sync``, ``POST
    /api/repo/add`` and ``POST /api/mcp/serve`` (see module docstring for the
    auth model). The caller (``cmd_dashboard``) refuses this combined with a
    non-loopback ``host`` or with ``sample`` -- there is no real fleet to mutate
    behind the bundled demo data. ``workspace`` is where ``add_repo`` clones new
    repos; defaults to ``store_dir.parent`` when not given.
    """
    from .. import visualize as viz

    store_dir = Path(store_dir)
    store_factory, store_path = type(store), getattr(store, "path", None)
    ws_dir = Path(workspace) if workspace else store_dir.parent
    token = secrets.token_urlsafe(32) if allow_mutations else None
    host_header_ok = {f"{host}:{port}", f"localhost:{port}"}

    shell = _static("dashboard.html")
    js = _static("dashboard.js")
    if allow_mutations:
        js = f'window.__CL_TOKEN__={json.dumps(token)};\nwindow.__CL_MUTATIONS__=true;\n' + js
    else:
        js = "window.__CL_MUTATIONS__=false;\n" + js
    assets = {
        "dashboard.js": (js, "application/javascript"),
        "dashboard.css": (_static("dashboard.css"), "text/css"),
        # Lazy-loaded by dashboard.js only when the Diagrams tab is first opened
        # (it's ~3.5MB, unlike dashboard.js/css above) -- still read eagerly here
        # at server-build time like the others, that cost is server-side memory,
        # not a browser fetch.
        "mermaid.min.js": (_static("mermaid.min.js"), "application/javascript"),
    }

    # The cross-linked graph pages reuse the cytoscape visualizer. Build the fleet
    # overview once (self-contained, live=True for click-to-expand); repo pages are
    # rendered on demand. Repo nodes link to ``repo-<slug>.html`` (resolved under
    # /graph/ in the iframe).
    sizes = viz.repo_node_sizes(store)
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
                repo = (q.get("repo") or [None])[0]
                return 200, _json_bytes(
                    kbdata.impact(req, nid, hops=hops, limit=limit, repo=repo))
            if path == "/api/impact/diagram":
                nid = (q.get("node") or q.get("id") or [None])[0]
                if not nid:
                    return 400, b'{"error":"node required"}'
                hops = int((q.get("hops") or [2])[0])
                return 200, _json_bytes(kbdata.sequence_diagram(req, nid, hops=hops))
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
                if rest.endswith("/data-flow"):
                    repo_id = urllib.parse.unquote(rest[:-len("/data-flow")])
                    return 200, _json_bytes(kbdata.data_flow(req, repo_id))
                if rest.endswith("/diagram"):
                    repo_id = urllib.parse.unquote(rest[:-len("/diagram")])
                    fmt = (q.get("format") or ["mermaid"])[0]
                    return 200, _json_bytes(kbdata.diagram(req, repo_id, fmt))
                repo_id = urllib.parse.unquote(rest)
                return 200, _json_bytes(kbdata.repo_detail(req, sd, repo_id))
            if path == "/api/mcp":
                out = kbdata.mcp_console(req, sd, config_path=config_path, sample=sample)
                mcp_status = kbmut.mcp_status(sd) if allow_mutations else {"running": False}
                out["mutations"] = allow_mutations
                out["http_server"] = mcp_status
                return 200, _json_bytes(out)
            if path == "/api/settings":
                out = kbdata.settings(req, sd, config_path=config_path, sample=sample)
                out["mutations"] = allow_mutations
                return 200, _json_bytes(out)
            if path == "/api/capabilities":
                return 200, _json_bytes({"mutations": allow_mutations})
            return 404, b'{"error":"not found"}'
        finally:
            if req is not store:
                req.close()

    def _mutate(path: str, body: bytes) -> tuple[int, bytes]:
        """POST /api/* mutating routes. Caller has already checked the token and
        Host header; this only dispatches and holds the store's single-writer
        lock for the duration of the one mutation (never for the server's whole
        lifetime -- a CLI command run alongside the dashboard must still work)."""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return 400, b'{"error":"invalid JSON body"}'
        lock = StoreLock(store_dir, f"dashboard-mutate {path}")
        try:
            lock.acquire()
        except StoreBusy as e:
            h = e.holder
            return 409, _json_bytes({
                "error": "store is busy",
                "holder": {"pid": h.get("pid"), "command": h.get("command"),
                          "host": h.get("host")},
            })
        try:
            req = _open_store()
            try:
                if path.startswith("/api/repo/") and path.endswith("/sync"):
                    repo_id = urllib.parse.unquote(path[len("/api/repo/"):-len("/sync")])
                    return 200, _json_bytes(kbmut.sync_repo(req, store_dir, repo_id))
                if path == "/api/repo/add":
                    url = (payload.get("url") or "").strip()
                    if not url:
                        return 400, b'{"error":"url required"}'
                    return 200, _json_bytes(kbmut.add_repo(req, store_dir, ws_dir, url))
                if path == "/api/mcp/serve":
                    action = payload.get("action")
                    if action == "start":
                        return 200, _json_bytes(kbmut.mcp_start(
                            store_dir, host=payload.get("host") or "127.0.0.1",
                            port=int(payload.get("port") or 8766), config_path=config_path))
                    if action == "stop":
                        return 200, _json_bytes(kbmut.mcp_stop(store_dir))
                    if action == "restart":
                        return 200, _json_bytes(kbmut.mcp_restart(
                            store_dir, host=payload.get("host") or "127.0.0.1",
                            port=int(payload.get("port") or 8766), config_path=config_path))
                    return 400, b'{"error":"action must be start, stop, or restart"}'
                return 404, b'{"error":"not found"}'
            finally:
                if req is not store:
                    req.close()
        finally:
            lock.release()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # keep request logs off the console
            pass

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # the client (browser tab, curl, etc.) went away mid-write -- nothing
                # left to send it, and ThreadingHTTPServer already isolates this to its
                # own request thread, so there's nothing to do but not print a traceback.
                pass

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

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
            if not allow_mutations:
                self._send(404, "text/plain", b"not found")
                return
            # DNS rebinding: an attacker domain that resolves to 127.0.0.1 would
            # otherwise sail straight past the loopback bind, since the browser's
            # same-origin check is about the domain, not the resolved address.
            # Requiring the Host header to literally name this host:port closes
            # that gap without needing a real TLS cert on localhost.
            if (self.headers.get("Host") or "") not in host_header_ok:
                self._send(403, "text/plain", b"forbidden")
                return
            given = self.headers.get(TOKEN_HEADER) or ""
            if not token or not hmac.compare_digest(given, token):
                self._send(403, "text/plain", b"forbidden")
                return
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 1_000_000)) if length else b""
            code, resp = _mutate(parsed.path, body)
            self._send(code, "application/json", resp)

    return ThreadingHTTPServer((host, port), Handler)


def serve_dashboard(store_dir, *, host: str = "127.0.0.1", port: int = 8765,
                    open_browser: bool = False, config_path: str | None = None,
                    sample: bool = False, allow_mutations: bool = False,
                    workspace: str | None = None) -> None:
    """Serve the dashboard (blocking until Ctrl-C)."""
    from ... import style
    from ...logging_setup import log

    store_dir = Path(store_dir)
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        srv = build_dashboard_server(store, store_dir, host=host, port=port,
                                     config_path=config_path, sample=sample,
                                     allow_mutations=allow_mutations, workspace=workspace)
        log(style.ok(f"Dashboard on http://{host}:{port}  (Ctrl-C to stop)"))
        if allow_mutations:
            log(style.warn("Mutating routes enabled (--allow-mutations): sync/add-repo/"
                           "MCP-server actions can write to this store from the browser. "
                           "The per-launch auth token is wired into the served page only "
                           "-- it isn't printed here or logged anywhere."))
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
