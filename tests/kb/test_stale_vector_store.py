"""A stale embedding store must not answer silently (blocks E4).

A vector row is keyed by node id. If the id scheme changes and the store is not
re-embedded, every stored key stops matching a real node -- and the retrieval paths
dropped those hits with a bare `if n:`, returning a shorter, plausible, non-empty
answer. `doctor` still reports a healthy row count, and the half-migrated case is
worst: the surviving hits are silently biased toward whichever repos were re-embedded.

`ask` was the sharpest case, because its existing disclosure reports the QUESTION's
unmatched terms, so it would vouch that everything asked about is indexed while
quietly discarding most of what the search found.
"""

import asyncio

import pytest
from mcp import Client

from contextlake.kb.embeddings.index import EMBED_CONTENT_VERSION
from contextlake.kb.model import Node
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore

_QUESTION = "cache and retry"          # terms anchor to seeded content, routes to search


class _Emb:
    dim = 3

    def embed(self, xs):
        return [[0.1, 0.2, 0.3] for _ in xs]


class _Stale:
    """One live id and two that name nodes the graph no longer holds."""

    def search(self, vec, k=10, repo=None):
        return [("real", 0.9), ("gone_1", 0.8), ("gone_2", 0.7)]


class _Healthy:
    def search(self, vec, k=10, repo=None):
        return [("real", 0.9), ("r2", 0.8)]


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_nodes("app", [
        Node(id="real", repo="app", kind="function", name="cache", file="c.py"),
        Node(id="r2", repo="app", kind="function", name="retry", file="r.py"),
    ])
    yield s
    s.close()


def _ask(store, vs, question=_QUESTION):
    srv = build_server(store, embedder=_Emb(), vector_store=vs)

    async def go():
        async with Client(srv) as c:
            return await c.call_tool("ask", {"question": question})
    return asyncio.run(go()).structured_content


def test_a_stale_store_is_disclosed_not_silently_trimmed(store):
    res = _ask(store, _Stale())
    assert len(res["nodes"]) == 1                 # 2 of 3 hits were unresolvable
    assert "INCOMPLETE" in res["note"]
    assert "kb embed" in res["note"]              # and it says how to repair it


def test_the_disclosure_counts_the_drops(store):
    assert "2 vector hit(s)" in _ask(store, _Stale())["note"]


def test_a_healthy_store_produces_no_warning(store):
    """The control. A warning that always fires is the defect, not the fix."""
    res = _ask(store, _Healthy())
    assert len(res["nodes"]) == 2
    assert "INCOMPLETE" not in (res["note"] or "")


def test_the_embed_content_version_contract_covers_ids():
    """The marker is the only signal that reaches `kb embed`'s incremental path, so an
    id-scheme change has to bump it. This asserts the contract is documented where the
    next person will look, since the value itself cannot express intent."""
    import inspect

    import contextlake.kb.embeddings.index as mod
    src = inspect.getsource(mod)
    head = src[:src.index("EMBED_CONTENT_VERSION =")]
    assert "node ids" in head.lower()
    assert "MUST bump" in head
    assert isinstance(EMBED_CONTENT_VERSION, int)


class TestSemanticAndHybridDiscloseStaleness:
    """These two used to return a BARE LIST, so a dropped hit had nowhere to be reported
    and the caller saw a shorter, entirely plausible result. They now return the same
    envelope every other node-returning verb uses, which is what makes the disclosure
    possible at all."""

    def _call(self, store, vs, tool):
        srv = build_server(store, embedder=_Emb(), vector_store=vs)

        async def go():
            async with Client(srv) as c:
                return await c.call_tool(tool, {"query": "cache retry"})
        return asyncio.run(go()).structured_content

    @pytest.mark.parametrize("tool", ["semantic_search", "hybrid_search"])
    def test_a_stale_store_is_disclosed(self, store, tool):
        res = self._call(store, _Stale(), tool)
        assert len(res["nodes"]) == 1
        assert "INCOMPLETE" in res["note"]
        assert "kb embed" in res["note"]

    @pytest.mark.parametrize("tool", ["semantic_search", "hybrid_search"])
    def test_a_healthy_store_says_nothing(self, store, tool):
        res = self._call(store, _Healthy(), tool)
        assert res["note"] is None

    @pytest.mark.parametrize("tool", ["semantic_search", "hybrid_search"])
    def test_the_envelope_shape_matches_the_other_verbs(self, store, tool):
        res = self._call(store, _Healthy(), tool)
        assert set(res) >= {"nodes", "total", "truncated"}
        assert res["total"] == len(res["nodes"])
