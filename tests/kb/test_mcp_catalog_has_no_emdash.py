"""The MCP tool catalog a client reads holds no em-dash (U+2014).

The source scan in ``tests/test_no_emdash_in_output.py`` checks the docstrings of the tool
functions. This file checks what a client is actually sent: the description, the input
schema and the output schema of every tool. That reaches pydantic class docstrings and
``Field`` descriptions, which a scan of function docstrings does not.

It lives under ``tests/kb/`` because building a server needs the ``[kb]`` extra, which the
core CI job does not install.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
from pathlib import Path

_GUARD = Path(__file__).resolve().parents[1] / "test_no_emdash_in_output.py"
_spec = importlib.util.spec_from_file_location("_emdash_output_guard", _GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

EM = guard.EM


def _live_catalog() -> list[dict]:
    """Every tool a server offers, with embeddings on (stubs stand in for a real embedder,
    because registration only checks that both objects exist)."""
    from contextlake.kb.embeddings.store import VectorStore
    from contextlake.kb.server import build_server
    from contextlake.kb.store.sqlite_store import SqliteStore

    class _StubEmbedder:
        name = "stub"

        def embed(self, texts):
            return [[0.0, 0.0] for _ in texts]

    with tempfile.TemporaryDirectory() as d:
        store = SqliteStore(Path(d) / "k.sqlite")
        vs = VectorStore(Path(d) / "v.sqlite")
        try:
            server = build_server(store, embedder=_StubEmbedder(), vector_store=vs)
            tools = asyncio.run(server.list_tools())
            return [t.model_dump(mode="json") for t in tools]
        finally:
            vs.close()
            store.close()


def test_the_source_scan_sees_every_tool_a_server_offers():
    """A renamed decorator would leave the docstring check with nothing to check."""
    _findings, tools = guard.scan_python(guard.SRC)
    live = {t["name"] for t in _live_catalog()}
    assert len(tools) >= 25
    assert set(tools) == live


def test_no_mcp_tool_description_or_schema_holds_an_em_dash():
    bad = []
    for tool in _live_catalog():
        blob = json.dumps(tool, ensure_ascii=False)
        if EM in blob or "\\u2014" in blob:
            at = blob.find(EM)
            bad.append(f"{tool['name']}: ...{blob[max(0, at - 50):at + 30]}...")
    assert not bad, "an em-dash in the tool catalog a client reads:\n" + "\n".join(bad)
