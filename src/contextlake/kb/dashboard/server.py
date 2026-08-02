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
                           allow_mutations: bool = False, workspace: str | None = None,
                           llm_chat: bool = False):
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

    ``POST /api/chat`` (natural-language Q&A over the graph, see :mod:`.chat`)
    is always available, at the same free/read-only risk level as the other
    ``/api/*`` GET routes -- it needs no flag and no token. ``llm_chat=True``
    (``--llm-chat`` on the CLI) additionally builds an ``LlmClient`` from the
    ambient ``[llm]`` config and turns the router's structured answer into
    prose; that path costs real time/tokens, so it's opt-in at server-start
    time (never per-request) and its own requests carry the same per-process
    token mutations use, to stop a page other than this dashboard from
    silently triggering paid calls.
    """
    from .. import visualize as viz

    store_dir = Path(store_dir)
    store_factory, store_path = type(store), getattr(store, "path", None)
    ws_dir = Path(workspace) if workspace else store_dir.parent
    token = secrets.token_urlsafe(32) if (allow_mutations or llm_chat) else None
    host_header_ok = {f"{host}:{port}", f"localhost:{port}"}

    chat_llm = None
    chat_embedder = chat_vector_store = None
    if llm_chat:
        from ..config import KbConfig, load_kb_config
        from ..embeddings import build_embedder
        from ..llm.base import build_llm

        cfg = KbConfig() if sample else load_kb_config(config_path)
        chat_llm = build_llm(cfg.llm)
        chat_embedder = build_embedder(cfg.embeddings)
        vec_path = store_dir / "embeddings.sqlite"
        if chat_embedder is not None and vec_path.exists():
            from ..embeddings.store import build_vector_store

            chat_vector_store = build_vector_store(vec_path, backend=cfg.embeddings.vector_backend)

    shell = _static("dashboard.html")
    js = _static("dashboard.js")
    js = f"window.__CL_LLM_CHAT__={json.dumps(chat_llm is not None)};\n" + js
    if allow_mutations or llm_chat:
        js = f'window.__CL_TOKEN__={json.dumps(token)};\n' + js
    js = f"window.__CL_MUTATIONS__={json.dumps(allow_mutations)};\n" + js
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
            if path == "/api/path":
                src = (q.get("from") or [None])[0]
                dst = (q.get("to") or [None])[0]
                if not src or not dst:
                    return 400, b'{"error":"from and to required"}'
                max_hops = int((q.get("max_hops") or [6])[0])
                repo = (q.get("repo") or [None])[0]
                return 200, _json_bytes(
                    kbdata.path(req, src, dst, max_hops=max_hops, repo=repo))
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
                    module = (q.get("module") or [None])[0]
                    return 200, _json_bytes(kbdata.diagram(req, repo_id, fmt, module=module))
                if rest.endswith("/modules"):
                    repo_id = urllib.parse.unquote(rest[:-len("/modules")])
                    within = (q.get("within") or [None])[0]
                    wiki_pages = (q.get("wiki") or ["false"])[0].lower() == "true"
                    return 200, _json_bytes(kbdata.repo_modules(
                        req, repo_id, within=within, store_dir=sd, wiki_pages=wiki_pages))
                if rest.endswith("/wiki"):
                    # A real repo id can itself end in "/wiki" (e.g. "team/wiki") --
                    # `rest` reaches here unquoted-but-still-slash-joined (encPath()
                    # only percent-encodes each segment, never the "/" separators), so
                    # naively stripping the trailing "/wiki" would silently hijack that
                    # repo's OWN /api/repo/<id> request into this sub-route, returning a
                    # wiki-content payload where a repo-detail payload was expected. Check
                    # whether the FULL rest string is itself a known repo id first, and
                    # fall through to the ordinary repo_detail handling below if so --
                    # only treat a trailing "/wiki" as this sub-route's marker when it
                    # ISN'T also a real repo id in its own right. Residual, deliberately
                    # left unresolved: if BOTH "team" and "team/wiki" are indexed, a
                    # module-scoped wiki fetch for "team" via this exact URL shape now
                    # loses to "team/wiki"'s repo-detail match -- the frontend degrades
                    # honestly (an unrecognized shape reads as "not found"), which is a
                    # correctness improvement over the prior silent-wrong-data bug, not
                    # a full disambiguation. The same inherent id-vs-sub-route collision
                    # is explicitly out of scope for /rel, /diagram, /modules, /data-flow
                    # too -- full-match-wins is the prescribed precedence, not a fix to
                    # chase further here.
                    full_candidate = urllib.parse.unquote(rest)
                    if req.get_repo(full_candidate) is None:
                        repo_id = urllib.parse.unquote(rest[:-len("/wiki")])
                        module = (q.get("module") or [None])[0]
                        return 200, _json_bytes(kbdata.repo_wiki(req, sd, repo_id, module=module))
                repo_id = urllib.parse.unquote(rest)
                return 200, _json_bytes(kbdata.repo_detail(req, sd, repo_id))
            if path == "/api/mcp":
                out = kbdata.mcp_console(req, sd, config_path=config_path, sample=sample)
                mcp_status = kbmut.mcp_status(sd) if allow_mutations else {"running": False}
                out["mutations"] = allow_mutations
                out["http_server"] = mcp_status
                return 200, _json_bytes(out)
            if path == "/api/wiki/status":
                out = kbmut.wiki_generate_status(sd) if allow_mutations else {"running": False}
                return 200, _json_bytes(out)
            if path == "/api/wiki/estimate":
                if not allow_mutations:
                    return 404, b'{"error":"not found"}'
                repo_id = (q.get("repo") or [None])[0]
                force = (q.get("force") or ["false"])[0].lower() == "true"
                return 200, _json_bytes(
                    kbmut.wiki_generate_estimate(req, sd, repo_id=repo_id, force=force))
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

    def _wiki_generate(body: bytes) -> tuple[int, bytes]:
        """POST /api/wiki/generate -- deliberately NOT dispatched through _mutate:
        that helper holds the store's single-writer lock for the dispatch's
        duration, but the spawned `contextlake wiki` child takes that SAME lock
        itself at startup (via _guard_store) and is a different pid, so it can
        lose the race against this request's own lock window and exit with
        "store is busy". wiki_generate_start() itself never touches the store
        (only a subprocess spawn + a pidfile write), so it needs no lock here at
        all -- the child manages its own."""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return 400, b'{"error":"invalid JSON body"}'
        repo_id = (payload.get("repo") or "").strip() or None
        force = bool(payload.get("force"))
        llm = (payload.get("llm") or "").strip() or None
        llm_model = (payload.get("llm_model") or "").strip() or None
        return 200, _json_bytes(kbmut.wiki_generate_start(
            store_dir, repo_id=repo_id, force=force, llm=llm,
            llm_model=llm_model, config_path=config_path))

    def _chat(body: bytes) -> tuple[int, bytes]:
        """POST /api/chat -- always available (see build_dashboard_server's
        docstring for why this needs no flag/token of its own; llm_chat's own
        token requirement is checked by the caller before this runs). No store
        lock: this never writes, so it can run alongside any other
        dashboard/CLI activity without contention."""
        from . import chat as kbchat

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return 400, b'{"error":"invalid JSON body"}'
        question = (payload.get("question") or "").strip()
        if not question:
            return 400, b'{"error":"question required"}'
        req = _open_store()
        try:
            result = kbchat.chat_answer(
                req, question, llm=chat_llm, embedder=chat_embedder,
                vector_store=chat_vector_store)
            return 200, _json_bytes(result)
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
            parsed = urllib.parse.urlparse(self.path)
            # DNS rebinding: an attacker domain that resolves to 127.0.0.1 would
            # otherwise sail straight past the loopback bind, since the browser's
            # same-origin check is about the domain, not the resolved address.
            # Requiring the Host header to literally name this host:port closes
            # that gap without needing a real TLS cert on localhost. Applies to
            # every POST below, chat included.
            if (self.headers.get("Host") or "") not in host_header_ok:
                self._send(403, "text/plain", b"forbidden")
                return

            if parsed.path == "/api/chat":
                # Free/read-only (router-only) chat needs no token, same risk
                # level as any other GET /api/* route. Only the LLM-synthesis
                # layer costs real time/tokens, so only *it* requires the same
                # per-process token mutations use -- checked here rather than
                # inside _chat so a bad/missing token is rejected before the
                # (free) router work even runs.
                if chat_llm is not None:
                    given = self.headers.get(TOKEN_HEADER) or ""
                    if not token or not hmac.compare_digest(given, token):
                        self._send(403, "text/plain", b"forbidden")
                        return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(length, 1_000_000)) if length else b""
                code, resp = _chat(body)
                self._send(code, "application/json", resp)
                return

            if not allow_mutations:
                self._send(404, "text/plain", b"not found")
                return
            given = self.headers.get(TOKEN_HEADER) or ""
            if not token or not hmac.compare_digest(given, token):
                self._send(403, "text/plain", b"forbidden")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 1_000_000)) if length else b""
            if parsed.path == "/api/wiki/generate":
                code, resp = _wiki_generate(body)
            else:
                code, resp = _mutate(parsed.path, body)
            self._send(code, "application/json", resp)

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.chat_llm_enabled = chat_llm is not None  # for serve_dashboard's startup log only
    return srv


def serve_dashboard(store_dir, *, host: str = "127.0.0.1", port: int = 8765,
                    open_browser: bool = False, config_path: str | None = None,
                    sample: bool = False, allow_mutations: bool = False,
                    workspace: str | None = None, llm_chat: bool = False) -> int:
    """Serve the dashboard (blocking until Ctrl-C). Returns a process exit code."""
    from ... import style
    from ...logging_setup import log

    store_dir = Path(store_dir)
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        try:
            srv = build_dashboard_server(store, store_dir, host=host, port=port,
                                         config_path=config_path, sample=sample,
                                         allow_mutations=allow_mutations, workspace=workspace,
                                         llm_chat=llm_chat)
        except OSError as e:
            # A raw traceback here (port already in use is the common case --
            # a previous `dashboard --serve` still running, most often) is a
            # bad first impression for an error this ordinary and this
            # actionable.
            log(style.warn(f"Could not start the dashboard on {host}:{port} — {e}"))
            log(style.dim("  Another `contextlake kb dashboard --serve` may already be "
                          "running on this port -- pick another with --port, or stop "
                          "the existing one."))
            return 1
        log(style.ok(f"Dashboard on http://{host}:{port}  (Ctrl-C to stop)"))
        log("Ask a question in the Chat tab -- free graph-router answers, always on.")
        if allow_mutations:
            log(style.warn("Mutating routes enabled (--allow-mutations): sync/add-repo/"
                           "MCP-server actions can write to this store from the browser. "
                           "The per-launch auth token is wired into the served page only "
                           "-- it isn't printed here or logged anywhere."))
        if llm_chat:
            if srv.chat_llm_enabled:  # type: ignore[attr-defined]
                log(style.warn("LLM chat synthesis enabled (--llm-chat): questions are "
                               "sent to the configured [llm] provider for prose answers. "
                               "The per-launch auth token gates this path the same way "
                               "--allow-mutations gates writes."))
            else:
                log(style.dim("--llm-chat was set, but [llm] isn't enabled in this "
                              "config -- chat stays free/router-only. Configure [llm] "
                              "in kb.toml (same setting `contextlake kb wiki` uses) to turn "
                              "synthesis on."))
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://{host}:{port}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log("Stopping dashboard server")
        finally:
            srv.shutdown()
        return 0
    finally:
        store.close()
