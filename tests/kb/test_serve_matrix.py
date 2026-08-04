"""Combinatorial coverage for `contextlake kb serve`'s tool-wiring matrix
(RC-P1-7 / T-2, T-6, T-7).

``cmd_serve`` (kb/cmds/serve.py) decides three independent things per
invocation: which transport to bind (stdio | http | sse), whether an embedder
resolves at all, and whether a vector store already exists on disk. The last
two together gate whether ``semantic_search``/``hybrid_search`` get registered
on the MCP server at all (see server.py's ``build_server``:
``if embedder is not None and vector_store is not None:``). Existing tests
(test_kb_server.py, test_kb_commands.py) each exercise one or two cells of this
by hand; this file sweeps transport x embedder-present x vector-store-present
as a full product and checks, for every cell:

  1. the resolved (embedder, vector_store) None-ness matches what was configured;
  2. the *actual* MCP tool set registered (via a real ``build_server`` + an
     in-memory MCP client, not just inspecting the None-ness) matches;
  3. the codebase's "say why a capability is missing" logging contract holds --
     this project treats a silently-vanished tool as a bug, not a UX nicety
     (see serve.py's own comment: "these two tools silently vanishing... reads
     as a broken server, not an unconfigured tier").

The second half of the file (RC-P1-1 / F-5) covers the *other* axis the
transport choice decides: whether the server is reachable over a socket, and so
whether it must authenticate. Those tests drive the real ASGI app
(``build_http_app``) through starlette's TestClient rather than the CLI, because
the SDK's ``run_streamable_http_async``/``run_sse_async`` go straight into
``uvicorn.Server.serve()`` -- no test can reach the app they build.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client

from contextlake.kb import commands as commands_mod
from contextlake.kb.server import build_server

# The three CLI-visible transports cmd_serve maps `--transport` onto (see its
# `cli_transport` branch); None is the CLI default (no --transport given),
# kept as a distinct case from the literal "stdio" string since both must
# resolve to the same place.
TRANSPORTS = [None, "stdio", "http", "sse"]
EMBEDDER_PRESENT = [True, False]
VECTOR_STORE_PRESENT = [True, False]

_SEMANTIC_TOOLS = {"semantic_search", "hybrid_search"}
_ALWAYS_ON_TOOLS = {"graph_stats", "get_node", "find_definition", "ask"}


def _kb_config(tmp_path, *, embeddings_enabled: bool) -> tuple[Path, Path]:
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    text = f'[kb]\nstore_dir = "{store_dir}"\n'
    if embeddings_enabled:
        # provider="builtin" resolves to a real, working Embedder with no
        # network call and no download at construction time (the model loads
        # lazily, only on first embed() -- see BuiltinEmbedder's docstring) --
        # so "embedder present" below is a genuine working client, not a stub.
        text += '\n[embeddings]\nenabled = true\nprovider = "builtin"\n'
    cfg.write_text(text)
    return cfg, store_dir


def _serve_args(cfg, transport):
    return SimpleNamespace(config=str(cfg), transport=transport, host=None, port=None)


async def _tool_names(server) -> set[str]:
    async with Client(server) as client:
        tools = await client.list_tools()
        return {t.name for t in tools.tools}


_CASES = list(itertools.product(TRANSPORTS, EMBEDDER_PRESENT, VECTOR_STORE_PRESENT))


@pytest.mark.parametrize("transport,embedder_present,vector_store_present", _CASES)
def test_serve_matrix_registers_expected_tools_and_logs_why_not(
    tmp_path, gls_logs, monkeypatch, transport, embedder_present, vector_store_present,
):
    cfg, store_dir = _kb_config(tmp_path, embeddings_enabled=embedder_present)
    if vector_store_present:
        # Any existing file satisfies serve.py's own gate (`vec_path.exists()`);
        # sqlite3 treats a fresh/empty file as a valid empty database, so this
        # need not be a fully-formed vector store to exercise the real path.
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "embeddings.sqlite").touch()

    captured: dict = {}

    def _fake_run_server(store, **kw):
        captured["store"] = store
        captured.update(kw)

    monkeypatch.setattr("contextlake.kb.server.run_server", _fake_run_server)
    # This file's axes are transport x embedder x vector-store, not embedder
    # *readiness*: the banner now reports whether the engine can actually load
    # here, which depends on whether the optional `kb-local` extra is installed
    # in the running environment (CI installs `.[dev,kb]`, which excludes it).
    # Pinning the ready state keeps every cell asserting the thing this file is
    # about; the readiness branches have their own tests below.
    monkeypatch.setattr("contextlake.kb.embeddings.embedder_runtime_state",
                        lambda embedder: (True, ""))

    rc = commands_mod.cmd_serve(_serve_args(cfg, transport))

    assert rc == 0
    semantic_expected = embedder_present and vector_store_present
    # serve.py only forwards a non-None embedder/vector_store into run_server
    # when BOTH conditions hold (see its `if candidate is not None and
    # vec_path.exists():` gate) -- an embedder that resolved fine but has no
    # store yet (or vice versa) is deliberately dropped on the floor, not
    # half-wired in. So both booleans track `semantic_expected`, not their own
    # individual "present" flag.
    assert (captured.get("embedder") is not None) == semantic_expected
    assert (captured.get("vector_store") is not None) == semantic_expected

    # Build the real server with exactly what cmd_serve resolved and check the
    # actual registered MCP tool set -- not just the None-ness of the two args.
    # (cmd_serve's own `finally` already closed `store`/`vector_store` by now,
    # but list_tools() only inspects the build-time tool registry -- it never
    # touches the store/vector-store connection, so that's safe here.)
    srv = build_server(
        captured["store"], embedder=captured.get("embedder"),
        vector_store=captured.get("vector_store"),
    )
    names = asyncio.run(_tool_names(srv))
    assert _ALWAYS_ON_TOOLS <= names  # every cell keeps the core toolset
    if semantic_expected:
        assert _SEMANTIC_TOOLS <= names
    else:
        assert not (_SEMANTIC_TOOLS & names)

    msgs = "\n".join(r.getMessage() for r in gls_logs.records)
    if semantic_expected:
        assert "Semantic search enabled" in msgs
    elif not embedder_present:
        # candidate is None -> this reason wins even when a store file exists,
        # per serve.py's own `why = (... if candidate is None else ...)`.
        assert "no [embeddings] config" in msgs
    else:
        assert "no vector store yet" in msgs

    # Transport dispatch itself (already regression-guarded in test_kb_commands.py
    # for the common cases) -- re-asserted per cell here since it's one of this
    # file's three declared axes, not just incidental plumbing.
    resolved_transport = {
        None: "stdio", "stdio": "stdio", "http": "streamable-http", "sse": "sse",
    }[transport]
    assert captured.get("transport") == resolved_transport
    if resolved_transport == "stdio":
        assert "http://" not in msgs
        # stdio is a pipe the editor owns: no socket, so no token (RC-P1-1).
        assert captured.get("token") is None
    else:
        path = {"streamable-http": "/mcp", "sse": "/sse"}[resolved_transport]
        assert f"http://127.0.0.1:8765{path}" in msgs
        # ...and every socket transport gets one, in every cell of the matrix.
        assert captured.get("token")


# --------------------------------------------------------------------------
# RC-P1-1 / F-5: authentication + Origin validation on the HTTP transports.
# --------------------------------------------------------------------------

TOKEN = "test-token-not-a-real-secret"  # synthetic; never a minted value

# The Host the SDK's own rebinding check expects for the default bind. Without
# this base_url, TestClient sends `Host: testserver` and every request 421s
# before it ever reaches an assertion about auth.
LOOPBACK_BASE = "http://127.0.0.1:8765"

_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "1"}},
}


@pytest.fixture
def http_app(tmp_path):
    """The real streamable-http ASGI app over an empty store, token-gated."""
    from starlette.testclient import TestClient

    from contextlake.kb.server import build_http_app
    from contextlake.kb.store.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "index.sqlite")
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=TOKEN)
    try:
        with TestClient(app, base_url=LOOPBACK_BASE) as client:
            yield client
    finally:
        store.close()


@pytest.mark.parametrize("headers", [
    pytest.param({}, id="no-authorization-header"),
    pytest.param({"Authorization": f"Bearer {TOKEN}x"}, id="wrong-token"),
    pytest.param({"Authorization": TOKEN}, id="token-without-the-bearer-scheme"),
    pytest.param({"Authorization": "Basic dXNlcjpwYXNz"}, id="wrong-scheme"),
    # Sent pre-encoded because httpx refuses to encode a non-ASCII str header.
    # On the wire this is exactly what a hostile client can send, and it must
    # come back 401: comparing on str would raise TypeError inside
    # hmac.compare_digest and turn the attempt into a 500.
    pytest.param({"Authorization": "Bearer tökén".encode("latin-1")},
                 id="non-ascii-token"),
])
def test_http_transport_rejects_anything_but_the_right_bearer_token(http_app, headers):
    r = http_app.post("/mcp", json=_INITIALIZE, headers={**_JSON_HEADERS, **headers})
    assert r.status_code == 401
    # RFC 6750: the client is told which scheme to retry with.
    assert r.headers.get("www-authenticate") == "Bearer"


def test_http_transport_serves_tools_with_the_right_bearer_token(http_app):
    auth = {**_JSON_HEADERS, "Authorization": f"Bearer {TOKEN}"}

    init = http_app.post("/mcp", json=_INITIALIZE, headers=auth)
    assert init.status_code == 200

    listed = http_app.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth)
    assert listed.status_code == 200
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert _ALWAYS_ON_TOOLS <= names


def test_bearer_scheme_match_is_case_insensitive(http_app):
    """RFC 7235 makes the auth scheme case-insensitive; clients do send "bearer"."""
    r = http_app.post("/mcp", json=_INITIALIZE,
                      headers={**_JSON_HEADERS, "Authorization": f"bearer {TOKEN}"})
    assert r.status_code == 200


@pytest.mark.parametrize("header,value,expected", [
    # Origin validation is the MCP spec's explicit requirement for HTTP
    # transports; 403 and 421 are the SDK's own codes for the two checks.
    ("Origin", "http://evil.example", 403),
    ("Host", "evil.example", 421),
])
def test_default_loopback_bind_still_validates_origin_and_host(
    http_app, header, value, expected,
):
    """Passing our own TransportSecuritySettings *replaces* the SDK's loopback
    auto-enable (it only builds its own when transport_security is None), so the
    default --host 127.0.0.1 path is exactly where a careless allow-list would
    have regressed. Asserted with a valid token, so only the header is on trial.
    """
    r = http_app.post("/mcp", json=_INITIALIZE, headers={
        **_JSON_HEADERS, "Authorization": f"Bearer {TOKEN}", header: value})
    assert r.status_code == expected


def test_a_browser_origin_on_the_bound_host_is_accepted(http_app):
    r = http_app.post("/mcp", json=_INITIALIZE, headers={
        **_JSON_HEADERS, "Authorization": f"Bearer {TOKEN}",
        "Origin": "http://localhost:3000"})
    assert r.status_code == 200


def test_sse_transport_is_gated_by_the_same_token(tmp_path):
    """The legacy transport is a socket too -- and its /messages/ POST endpoint
    is as sensitive as /sse itself, so the gate wraps the whole app."""
    from starlette.testclient import TestClient

    from contextlake.kb.server import build_http_app
    from contextlake.kb.store.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "index.sqlite")
    try:
        app = build_http_app(store, transport="sse", host="127.0.0.1", token=TOKEN)
        with TestClient(app, base_url=LOOPBACK_BASE) as client:
            for path in ("/sse", "/messages/"):
                assert client.get(path).status_code == 401
                assert client.post(path, json={}).status_code == 401
    finally:
        store.close()


def test_stdio_takes_the_sdk_run_path_untouched(tmp_path, monkeypatch):
    """The default, most-used transport must be byte-for-byte the old behaviour:
    MCPServer.run(transport="stdio"), no host/port, no middleware, no token."""
    from contextlake.kb import server as server_mod
    from contextlake.kb.store.sqlite_store import SqliteStore

    calls: list[dict] = []
    monkeypatch.setattr(server_mod.MCPServer, "run",
                        lambda self, **kw: calls.append(kw))
    monkeypatch.setattr(
        server_mod, "build_http_app",
        lambda *a, **k: pytest.fail("stdio must not build an HTTP app"))

    store = SqliteStore(tmp_path / "index.sqlite")
    try:
        server_mod.run_server(store, transport="stdio")
    finally:
        store.close()
    assert calls == [{"transport": "stdio"}]


def test_env_token_is_reused_and_blank_env_fails_closed(monkeypatch):
    """A pinned CONTEXTLAKE_MCP_TOKEN survives restarts; a blank one (a shell
    expanding an unset var) must mint a fresh token, never disable auth."""
    from contextlake.kb.server import TOKEN_ENV, resolve_token

    monkeypatch.setenv(TOKEN_ENV, "  pinned-token-synthetic  ")
    assert resolve_token() == ("pinned-token-synthetic", True)

    for blank in ("", "   "):
        monkeypatch.setenv(TOKEN_ENV, blank)
        token, from_env = resolve_token()
        assert from_env is False
        assert len(token) >= 32

    monkeypatch.delenv(TOKEN_ENV)
    first, from_env = resolve_token()
    assert from_env is False
    assert first != resolve_token()[0]  # freshly minted per launch


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_non_loopback_host_is_refused_without_allow_remote(
    tmp_path, gls_logs, monkeypatch, transport,
):
    cfg, _ = _kb_config(tmp_path, embeddings_enabled=False)
    monkeypatch.setattr("contextlake.kb.server.run_server",
                        lambda *a, **k: pytest.fail("must not start a server"))

    args = _serve_args(cfg, transport)
    args.host = "0.0.0.0"
    assert commands_mod.cmd_serve(args) == 1

    msgs = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "--allow-remote" in msgs


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_binds_need_no_opt_in(tmp_path, monkeypatch, host):
    """Including both IPv6 spellings in LOOPBACK_HOSTS is the point: `--host ::1`
    is a loopback bind and must not be mistaken for a network one."""
    cfg, _ = _kb_config(tmp_path, embeddings_enabled=False)
    monkeypatch.setattr("contextlake.kb.server.run_server", lambda *a, **k: None)

    args = _serve_args(cfg, "http")
    args.host = host
    assert commands_mod.cmd_serve(args) == 0


def test_allow_remote_starts_the_server_and_warns(tmp_path, gls_logs, monkeypatch, capsys):
    cfg, _ = _kb_config(tmp_path, embeddings_enabled=False)
    captured: dict = {}
    monkeypatch.setattr("contextlake.kb.server.run_server",
                        lambda store, **kw: captured.update(kw))

    args = _serve_args(cfg, "http")
    args.host, args.allow_remote = "0.0.0.0", True
    assert commands_mod.cmd_serve(args) == 0

    msgs = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "--allow-remote" in msgs and "0.0.0.0" in msgs
    # The token reaches the server but never the logger: log() feeds a rotating
    # file handler, and a credential outliving the process in a log file would
    # be a worse leak than the unauthenticated socket this replaced.
    token = captured["token"]
    assert token and token not in msgs
    assert token in capsys.readouterr().err


# --- Embedder readiness: the banner must report runtime state, not config ---
#
# `build_embedder` returns a configured *candidate* without loading anything --
# the built-in engines import their library and fetch their model on the first
# embed(). So "the config named a provider" and "semantic search works here"
# are different questions, and the startup banner used to answer the first one
# while claiming to answer the second.


def _semantic_serve_args(tmp_path, monkeypatch):
    """A serve invocation that reaches the semantic banner: embeddings enabled
    and a vector-store file on disk (serve.py gates the banner on both).

    ``cache_dir`` is pinned inside tmp_path so the built-in model is definitely
    absent from it -- otherwise the answer depends on whatever the developer
    running the suite happens to have downloaded.
    """
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{store_dir}"\n'
        '\n[embeddings]\nenabled = true\nprovider = "builtin"\n'
        f'cache_dir = "{tmp_path / "models"}"\n'
    )
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "embeddings.sqlite").touch()
    monkeypatch.setattr("contextlake.kb.server.run_server", lambda *a, **k: None)
    return _serve_args(cfg, "stdio")


def test_serve_banner_does_not_claim_semantic_search_when_the_engine_cannot_load(
    tmp_path, gls_logs, monkeypatch,
):
    """The reported defect: with the engine absent, the banner said "Semantic
    search enabled" and every semantic/hybrid query then failed at call time.

    Driven through the real condition (the engine module not being importable)
    rather than by stubbing the readiness helper, so this fails on the old
    banner's *behaviour* and not merely on a missing symbol.
    """
    import importlib.util as _u

    args = _semantic_serve_args(tmp_path, monkeypatch)
    real = _u.find_spec
    monkeypatch.setattr(_u, "find_spec",
                        lambda name, *a, **k: None if name == "model2vec" else real(name, *a, **k))

    assert commands_mod.cmd_serve(args) == 0

    msgs = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "Semantic search enabled" not in msgs
    assert "cannot load on this machine" in msgs
    assert "kb-local" in msgs  # the actionable install name


def test_serve_banner_flags_a_model_that_still_has_to_be_downloaded(
    tmp_path, gls_logs, monkeypatch,
):
    """Engine installed but model not cached: it works online and fails offline,
    so the capability is announced *and* the caveat is stated."""
    import importlib.util as _u

    args = _semantic_serve_args(tmp_path, monkeypatch)
    real = _u.find_spec
    # Engine importable, model cache empty (cache_dir is under tmp_path).
    monkeypatch.setattr(_u, "find_spec",
                        lambda name, *a, **k: object() if name == "model2vec"
                        else real(name, *a, **k))

    assert commands_mod.cmd_serve(args) == 0

    msgs = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "Semantic search enabled" in msgs  # it does work, given network
    assert "not downloaded" in msgs and "offline" in msgs


def test_serve_banner_stays_plain_when_the_embedder_is_ready(
    tmp_path, gls_logs, monkeypatch,
):
    args = _semantic_serve_args(tmp_path, monkeypatch)
    monkeypatch.setattr("contextlake.kb.embeddings.embedder_runtime_state",
                        lambda embedder: (True, ""))

    assert commands_mod.cmd_serve(args) == 0

    msgs = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "Semantic search enabled" in msgs
    assert "cannot load" not in msgs


def test_embedder_runtime_state_reports_a_missing_engine_as_unusable(tmp_path, monkeypatch):
    import importlib.util as _u

    from contextlake.kb.embeddings import embedder_runtime_state
    from contextlake.kb.embeddings.builtin import BuiltinEmbedder

    emb = BuiltinEmbedder(engine="model2vec", cache_dir=str(tmp_path))
    real = _u.find_spec
    monkeypatch.setattr(_u, "find_spec",
                        lambda name, *a, **k: None if name == "model2vec" else real(name, *a, **k))

    ready, why = embedder_runtime_state(emb)
    assert ready is False
    # The extra's install name is the actionable part -- a bare "not installed"
    # leaves the reader to guess which package fixes it.
    assert "kb-local" in why


def test_embedder_runtime_state_separates_not_downloaded_from_not_installed(
    tmp_path, monkeypatch,
):
    import importlib.util as _u

    from contextlake.kb.embeddings import embedder_runtime_state
    from contextlake.kb.embeddings.builtin import BuiltinEmbedder

    emb = BuiltinEmbedder(engine="model2vec", cache_dir=str(tmp_path))
    monkeypatch.setattr(_u, "find_spec", lambda name, *a, **k: object())

    ready, why = embedder_runtime_state(emb)
    assert ready is None  # usable, but only with network
    assert "offline" in why

    cached = tmp_path / "hub" / ("models--" + emb.model_id.replace("/", "--"))
    cached.mkdir(parents=True)
    assert embedder_runtime_state(emb) == (True, "")


def test_embedder_runtime_state_leaves_remote_providers_alone(tmp_path):
    """Ollama/OpenAI hold no local model -- there is nothing to check without a
    request, and probing would spend circuit-breaker budget at startup."""
    from contextlake.kb.embeddings import embedder_runtime_state
    from contextlake.kb.embeddings.ollama import OllamaEmbedder

    assert embedder_runtime_state(OllamaEmbedder(model="nomic-embed-text")) == (True, "")
    assert embedder_runtime_state(None)[0] is False
