"""`contextlake kb serve` -- run the MCP server."""

from __future__ import annotations

from ... import style
from ...logging_setup import log
from ..config import load_kb_config
from ._common import (
    _open_store,
)


def cmd_serve(args) -> int:
    from ..server import run_server  # imported here so `query`/`index` don't load it

    store, store_dir = _open_store(args)
    vector_store = None
    interrupted = False
    try:
        # CLI exposes "http"; the MCP SDK calls it "streamable-http".
        transport = "streamable-http" if getattr(args, "transport", None) == "http" else "stdio"
        host = getattr(args, "host", None) or "127.0.0.1"
        port = getattr(args, "port", None) or 8765

        # On stdio, stdout carries the MCP JSON-RPC stream — keep our logs off it.
        if transport == "stdio":
            from ...logging_setup import use_stderr

            use_stderr()

        # Expose semantic_search only when embeddings are enabled and a vector store exists.
        cfg = load_kb_config(getattr(args, "config", None))
        embedder = None
        from ..embeddings import build_embedder

        candidate = build_embedder(cfg.embeddings)
        vec_path = store_dir / "embeddings.sqlite"
        if candidate is not None and vec_path.exists():
            from ..embeddings.store import build_vector_store

            embedder = candidate
            vector_store = build_vector_store(vec_path, backend=cfg.embeddings.vector_backend)
            log(f"Semantic search enabled ({vector_store.name} store, "
                f"{vector_store.count()} vectors)")
        else:
            # Say so out loud: these two tools silently vanishing from the MCP tool
            # list otherwise reads as a broken server, not an unconfigured tier.
            why = ("no [embeddings] config" if candidate is None
                   else "no vector store yet — run: contextlake kb embed")
            log(style.dim(f"semantic_search / hybrid_search not registered ({why}); "
                          "graph search and every other tool work without them"))

        log(f"Serving knowledge graph over MCP ({transport})")
        if transport == "streamable-http":
            # stdio has no bind address to report; http does, and a blocking
            # server that never says where it listens reads as broken, not busy.
            log(style.ok(f"MCP server on http://{host}:{port}  (Ctrl-C to stop)"))
        run_server(store, transport=transport, host=host, port=port,
                   embedder=embedder, vector_store=vector_store)
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

