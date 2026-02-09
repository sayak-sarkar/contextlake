"""Tests for the LLM client abstraction (no live model)."""

import pytest

import gitlab_sync.kb.llm.ollama as ollama_mod
from gitlab_sync.kb.config import LlmCfg
from gitlab_sync.kb.llm import build_llm
from gitlab_sync.kb.llm.ollama import OllamaLlm


def test_build_llm_disabled_returns_none():
    assert build_llm(LlmCfg(enabled=False)) is None


def test_build_llm_ollama():
    llm = build_llm(LlmCfg(enabled=True, provider="ollama", model="m", base_url="http://h:1"))
    assert isinstance(llm, OllamaLlm) and llm.model == "m" and llm.base_url == "http://h:1"


def test_build_llm_unknown_raises():
    with pytest.raises(ValueError, match="unknown llm provider"):
        build_llm(LlmCfg(enabled=True, provider="nope"))


def test_ollama_generate(monkeypatch):
    seen = {}

    def fake_post(url, payload, timeout):
        seen["url"] = url
        seen["payload"] = payload
        return {"response": "  hello world  "}

    monkeypatch.setattr(ollama_mod, "_post_json", fake_post)
    out = OllamaLlm(model="m", base_url="http://x:11434/").generate("hi", system="be precise")

    assert out == "hello world"  # stripped
    assert seen["url"] == "http://x:11434/api/generate"
    assert seen["payload"]["model"] == "m" and seen["payload"]["prompt"] == "hi"
    assert seen["payload"]["system"] == "be precise" and seen["payload"]["stream"] is False
