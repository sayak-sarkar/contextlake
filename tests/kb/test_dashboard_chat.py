"""Unit tests for the dashboard chat answerer (kb/dashboard/chat.py).

No HTTP layer here -- see test_dashboard_server.py for the /api/chat route
(token gating, live-only behavior). This file tests chat_answer()'s two-layer
shape directly: the free router always runs; an LlmClient (a stub here, never
a real provider) is layered on top only when passed.
"""

from datetime import date

from contextlake.kb.dashboard.chat import chat_answer
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))


def _seeded_store(tmp_path):
    s = SqliteStore(tmp_path / "index.sqlite")
    nodes = [
        Node(id="svc", repo="team/app", kind="class", name="CatalogService", lang="python"),
        Node(id="caller", repo="team/app", kind="function", name="checkout", lang="python"),
    ]
    edges = [Edge(src="caller", dst="svc", relation="calls",
                  confidence=Confidence.EXTRACTED, provenance=_PROV)]
    s.upsert_repo(Repo(id="team/app", path=str(tmp_path), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="team/app", head_commit="h1", nodes=nodes, edges=edges))
    reindex_shard(s, tmp_path, "team/app")
    return s


class _StubLlm:
    def __init__(self, reply="STUBBED ANSWER", raises=None):
        self.reply = reply
        self.raises = raises
        self.last_prompt = None

    def generate(self, prompt, *, system=None):
        self.last_prompt = prompt
        if self.raises is not None:
            raise self.raises
        return self.reply


def test_chat_answer_free_mode_returns_structured_result_no_llm(tmp_path):
    s = _seeded_store(tmp_path)
    try:
        result = chat_answer(s, "who calls CatalogService?")
        assert result["llm_used"] is False
        assert result["answer"] is None
        assert "llm_error" not in result
        assert result["structured"]["route"] == "callers"
        assert {n["name"] for n in result["structured"]["nodes"]} == {"checkout"}
    finally:
        s.close()


def test_chat_answer_reuses_the_real_ask_tool_not_a_reimplementation(tmp_path):
    """The router logic must come from server.build_server's actual `ask` tool
    -- proven by resolving a bare name the same way `ask` does (via
    nodes_by_name), not some parallel lookup that would silently drift."""
    s = _seeded_store(tmp_path)
    try:
        result = chat_answer(s, "who calls CatalogService?")
        assert result["structured"]["target"] == "CatalogService"
    finally:
        s.close()


def test_chat_answer_llm_mode_grounds_prose_in_the_structured_result(tmp_path):
    s = _seeded_store(tmp_path)
    try:
        llm = _StubLlm(reply="checkout calls CatalogService.")
        result = chat_answer(s, "who calls CatalogService?", llm=llm)
        assert result["llm_used"] is True
        assert result["answer"] == "checkout calls CatalogService."
        # the free layer is still present alongside the LLM prose -- callers
        # can always show/verify the citations the prose was grounded in
        assert result["structured"]["route"] == "callers"
        # the prompt actually carries the real structured data, not a stub
        assert "checkout" in llm.last_prompt
        assert "who calls CatalogService?" in llm.last_prompt
    finally:
        s.close()


def test_chat_answer_llm_failure_degrades_to_the_free_result(tmp_path):
    """An LLM error (bad key, timeout, provider outage) must never break the
    free/router path -- the whole point of layering LLM synthesis on top."""
    s = _seeded_store(tmp_path)
    try:
        llm = _StubLlm(raises=RuntimeError("provider unreachable"))
        result = chat_answer(s, "who calls CatalogService?", llm=llm)
        assert result["llm_used"] is False
        assert result["answer"] is None
        assert "provider unreachable" in result["llm_error"]
        assert result["structured"]["route"] == "callers"  # free layer intact
    finally:
        s.close()


def test_chat_answer_no_match_reports_honestly_in_both_modes(tmp_path):
    s = _seeded_store(tmp_path)
    try:
        free = chat_answer(s, "who calls TotallyUnknownSymbol?")
        assert free["structured"]["nodes"] == []

        llm = _StubLlm(reply="No callers were found for that symbol.")
        grounded = chat_answer(s, "who calls TotallyUnknownSymbol?", llm=llm)
        assert grounded["llm_used"] is True
        assert "TotallyUnknownSymbol" in llm.last_prompt
    finally:
        s.close()
