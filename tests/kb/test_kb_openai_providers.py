"""Tests for the OpenAI-compatible embedder and LLM providers (no network)."""

import contextlake.kb.embeddings.openai as emb_openai
import contextlake.kb.llm.openai as llm_openai
from contextlake.kb.config import EmbeddingsCfg, LlmCfg
from contextlake.kb.embeddings import build_embedder
from contextlake.kb.embeddings.openai import OpenAIEmbedder
from contextlake.kb.llm import build_llm
from contextlake.kb.llm.openai import OpenAILlm

# --- factory wiring --------------------------------------------------------

def test_build_embedder_openai():
    emb = build_embedder(EmbeddingsCfg(enabled=True, provider="openai", model="m",
                                       base_url="http://local/v1"))
    assert isinstance(emb, OpenAIEmbedder) and emb.model == "m" and emb.base_url == "http://local/v1"


def test_build_llm_openai():
    llm = build_llm(LlmCfg(enabled=True, provider="openai", model="g", base_url="http://local/v1"))
    assert isinstance(llm, OpenAILlm) and llm.model == "g"


# --- embedder --------------------------------------------------------------

def test_openai_embed_batches_and_orders(monkeypatch):
    seen = {}

    def fake_post(url, payload, headers, timeout):
        seen["url"] = url
        seen["headers"] = headers
        # return rows out of order to prove we sort by index
        return {"data": [
            {"embedding": [0.2], "index": 1},
            {"embedding": [0.1], "index": 0},
        ]}

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(emb_openai, "_post_json", fake_post)
    vecs = OpenAIEmbedder(model="m", base_url="http://x/v1/").embed(["a", "b"])

    assert vecs == [[0.1], [0.2]]  # reordered by index
    assert seen["url"] == "http://x/v1/embeddings"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"


def test_openai_embed_no_key_omits_auth(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["headers"] = headers
        return {"data": [{"embedding": [1.0], "index": 0}]}

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(emb_openai, "_post_json", fake_post)
    OpenAIEmbedder(api_key_env="OPENAI_API_KEY").embed(["x"])
    assert "Authorization" not in captured["headers"]  # local server, no key needed


# --- llm -------------------------------------------------------------------

def test_openai_generate(monkeypatch):
    seen = {}

    def fake_post(url, payload, headers, timeout):
        seen["url"] = url
        seen["messages"] = payload["messages"]
        return {"choices": [{"message": {"content": "  a page  "}}]}

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm_openai, "_post_json", fake_post)
    out = OpenAILlm(model="g", base_url="http://x/v1").generate("hi", system="be precise")

    assert out == "a page"  # stripped
    assert seen["url"] == "http://x/v1/chat/completions"
    assert seen["messages"] == [
        {"role": "system", "content": "be precise"},
        {"role": "user", "content": "hi"},
    ]


def test_openai_generate_empty_choices(monkeypatch):
    monkeypatch.setattr(llm_openai, "_post_json", lambda *a, **k: {"choices": []})
    assert OpenAILlm().generate("hi") == ""
