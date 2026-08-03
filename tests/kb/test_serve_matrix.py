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
    else:
        path = {"streamable-http": "/mcp", "sse": "/sse"}[resolved_transport]
        assert f"http://127.0.0.1:8765{path}" in msgs
