"""`contextlake kb serve` -- run the MCP server."""

from __future__ import annotations

import sys

from ... import style
from ...logging_setup import log
from ..http_base import LOOPBACK_HOSTS
from ._common import (
    _open_store,
    kb_config,
)


def _announce_token(token: str, *, from_env: bool) -> None:
    """Tell the operator how to authenticate, exactly once, on stderr.

    Not through ``log()``: that reaches the configured rotating file handler
    (see logging_setup), and a credential that outlives the process in a log
    file is a worse leak than the unauthenticated socket this replaced. stdout
    is out for the same reason it is on stdio -- it is a protocol stream on one
    transport and pipeable output on the others, and a secret does not belong in
    either. A pinned ``CONTEXTLAKE_MCP_TOKEN`` is acknowledged but never echoed:
    the operator already has it, and printing it back only creates copies.
    """
    from ..server import TOKEN_ENV

    where = f"read from ${TOKEN_ENV}" if from_env else token
    print(f"  Bearer token: {where}", file=sys.stderr)
    print("  Clients must send: Authorization: Bearer <token>", file=sys.stderr)
    if not from_env:
        print(f"  Pin a stable one across restarts with ${TOKEN_ENV}.", file=sys.stderr)


def cmd_serve(args) -> int:
    # imported here so `query`/`index` don't load it
    from ..server import HTTP_TRANSPORTS, resolve_token, run_server

    # CLI exposes "http"; the MCP SDK calls it "streamable-http". "sse" (the
    # legacy HTTP+SSE transport) passes through unchanged -- it's a real,
    # separate transport in the SDK, not an alias for either of the other two.
    cli_transport = getattr(args, "transport", None)
    if cli_transport == "http":
        transport = "streamable-http"
    elif cli_transport == "sse":
        transport = "sse"
    else:
        transport = "stdio"
    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or 8765
    network = transport in HTTP_TRANSPORTS

    # Refused before the store is even opened: a bind this wide is a decision to
    # revisit, not a server to start and then reconsider. Every indexed file
    # path, symbol name, docstring and owner identity is readable through these
    # tools, so a non-loopback bind publishes the fleet's internals to whoever
    # can route to this machine. The bearer token below is the second layer, not
    # a substitute for meaning to do this.
    if network and host not in LOOPBACK_HOSTS and not getattr(args, "allow_remote", False):
        log(style.fail(f"--host {host!r} refused without --allow-remote: it would expose "
                       "the whole knowledge graph (file paths, symbol names, docstrings, "
                       "owner identities) to the network."))
        log("  Serve on 127.0.0.1 and tunnel to it, or pass --allow-remote if you "
            "genuinely mean to bind a network interface.")
        return 1

    store, store_dir = _open_store(args)
    vector_store = None
    interrupted = False
    try:
        # On stdio, stdout carries the MCP JSON-RPC stream — keep our logs off it.
        if transport == "stdio":
            from ...logging_setup import use_stderr

            use_stderr()

        # Expose semantic_search only when embeddings are enabled and a vector store exists.
        cfg = kb_config(args)
        embedder = None
        from ..embeddings import build_embedder

        candidate = build_embedder(cfg.embeddings)
        vec_path = store_dir / "embeddings.sqlite"
        if candidate is not None and vec_path.exists():
            from ..embeddings.store import build_vector_store

            embedder = candidate
            vector_store = build_vector_store(vec_path, backend=cfg.embeddings.vector_backend)
            # Report what the embedder can do *here*, not what the config asked
            # for. build_embedder hands back a candidate without loading
            # anything, so announcing the config's intent as a working capability
            # is how this banner came to read "Semantic search enabled" on a
            # machine where the engine was missing and every semantic/hybrid
            # query then failed at call time.
            from ..embeddings import embedder_runtime_state

            ready, why = embedder_runtime_state(embedder)
            if ready is False:
                log(style.warn(
                    f"semantic_search / hybrid_search are registered, but the embedder "
                    f"cannot load on this machine: {why}"))
                log(style.dim("  Every semantic or hybrid query will fail until that is "
                              "fixed; graph search and every other tool work without it"))
            else:
                log(f"Semantic search enabled ({vector_store.name} store, "
                    f"{vector_store.count()} vectors)")
                if ready is None:
                    log(style.dim(f"  {why}"))
        else:
            # Say so out loud: these two tools silently vanishing from the MCP tool
            # list otherwise reads as a broken server, not an unconfigured tier.
            why = ("no [embeddings] config" if candidate is None
                   else "no vector store yet — run: contextlake kb embed")
            log(style.dim(f"semantic_search / hybrid_search not registered ({why}); "
                          "graph search and every other tool work without them"))

        # An empty store starts, banners, and serves all twenty tools; every one
        # of them then answers confidently with nothing, and `graph_health`
        # reports zero stale and zero dangling -- the exact output of a perfectly
        # healthy fleet. Same reasoning as the semantic-search line above: a tier
        # that is missing rather than broken has to say so out loud, and "the
        # graph is empty" is the more consequential of the two. Deliberately
        # after use_stderr(): on stdio, stdout is the JSON-RPC stream.
        if store.stats().repos == 0:
            log(style.warn("This store holds no indexed repository, so every tool will "
                           "answer and every answer will be empty."))
            log(style.dim("  Index one first: contextlake kb index"))

        log(f"Serving knowledge graph over MCP ({transport})")
        # stdio has no bind address to report; the network transports do, and a
        # blocking server that never says where it listens reads as broken, not
        # busy. Neither network transport is mounted at the bare host:port --
        # each has its own path, and printing the bare root sends clients to a
        # 404. The paths are the SDK's own defaults, which contextlake does not
        # override: streamable_http_path="/mcp" and sse_path="/sse" (see
        # mcp.server.mcpserver.MCPServer.streamable_http_app / .sse_app).
        path = {"streamable-http": "/mcp", "sse": "/sse"}.get(transport)
        if path:
            log(style.ok(f"MCP server on http://{host}:{port}{path}  (Ctrl-C to stop)"))
        token = None
        if network:
            token, from_env = resolve_token()
            _announce_token(token, from_env=from_env)
            if host not in LOOPBACK_HOSTS:
                log(style.warn(
                    f"--allow-remote: bound to {host}, so anyone who can route here and "
                    "holds the token above can read the entire graph. Nothing is "
                    "encrypted in transit — put TLS in front of it, or tunnel instead."))
                # Same reasoning as http_base.host_pinning_hint (say it once at
                # startup, not per-request), but deliberately not that function:
                # its wording describes the *stdlib* servers' allow-set
                # ({host:port, localhost:port}), which is narrower than the MCP
                # transport's ({127.0.0.1, localhost, [::1], <bound host>} on any
                # port). Reusing the text would misreport what is accepted here.
                if host in ("0.0.0.0", "::", ""):  # noqa: S104 - this is the wildcard-bind check
                    log(style.warn(
                        f"A wildcard bind ({host}) does not accept this machine's own "
                        "address in a Host header, so a remote client naming it gets "
                        "421. Bind that address instead "
                        "(--host <the-address-clients-will-use>)."))
        run_server(store, transport=transport, host=host, port=port,
                   embedder=embedder, vector_store=vector_store, token=token,
                   tool_concurrency=getattr(args, "tool_concurrency", None))
        return 0
    except KeyboardInterrupt:
        # Ctrl-C is the documented way to stop this server (see the "Ctrl-C to
        # stop" log line above), same as graph --serve/--site and dashboard
        # --serve, each of which already reports its own stop message instead
        # of falling through to cli.py's generic "Operation cancelled" catch.
        log("Stopping MCP server")
        interrupted = True
        return 0
    finally:
        if vector_store is not None:
            vector_store.close()
        store.close()
        if interrupted:
            # The mcp SDK's stdio transport leaves a background thread blocked on
            # a stdin read; joining it during normal interpreter shutdown takes a
            # moment, and an impatient second/third Ctrl-C in that window lands
            # with no try/except left around it (we've already returned), surfacing
            # as a harmless but noisy "Exception ignored while joining a thread in
            # _thread._shutdown()" traceback fragment. Our own cleanup just ran
            # above; nothing meaningful is left to do, so skip Python's remaining
            # shutdown sequence (thread joins, atexit) rather than leave that
            # window open. Reproduced directly: 3 rapid SIGINTs before this fix
            # printed the traceback every time; after it, they never do.
            import os

            os._exit(0)
