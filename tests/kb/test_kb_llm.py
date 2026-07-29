"""Tests for the LLM client abstraction (no live model)."""

import io
import json
import sys
import types
import urllib.error

import pytest

import contextlake.kb.llm.base as base_mod
import contextlake.kb.llm.ollama as ollama_mod
from contextlake.kb.config import LlmCfg
from contextlake.kb.llm import build_llm
from contextlake.kb.llm.builtin import BuiltinLlm
from contextlake.kb.llm.ollama import OllamaLlm


def _fake_llama_cpp(monkeypatch, *, with_class=True):
    """Install a fake `llama_cpp` module so generate() needs no model.

    with_class=False simulates the extra being absent (import of Llama fails)."""
    mod = types.ModuleType("llama_cpp")
    if with_class:
        class FakeLlama:
            @classmethod
            def from_pretrained(cls, **kw):
                return cls()

            def create_chat_completion(self, *, messages, **kw):
                user = [m for m in messages if m["role"] == "user"][0]["content"]
                return {"choices": [{"message": {"content": f"  echo:{user}  "}}]}

        mod.Llama = FakeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", mod)


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

    monkeypatch.setattr(ollama_mod, "post_json", fake_post)
    out = OllamaLlm(model="m", base_url="http://x:11434/").generate("hi", system="be precise")

    assert out == "hello world"  # stripped
    assert seen["url"] == "http://x:11434/api/generate"
    assert seen["payload"]["model"] == "m" and seen["payload"]["prompt"] == "hi"
    assert seen["payload"]["system"] == "be precise" and seen["payload"]["stream"] is False


def test_ollama_generate_404_names_the_real_reason_and_the_fix(monkeypatch):
    error = 'model "llama3.1" not found, try pulling it first'
    body = json.dumps({"error": error}).encode()

    def fake_post(url, payload, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(body))

    monkeypatch.setattr(ollama_mod, "post_json", fake_post)
    with pytest.raises(RuntimeError) as exc:
        OllamaLlm(model="llama3.1", base_url="http://x:11434").generate("hi")
    msg = str(exc.value)
    assert 'model "llama3.1" not found' in msg
    assert "ollama pull llama3.1" in msg


# --- built-in LLM -----------------------------------------------------------

def test_default_provider_is_auto():
    assert LlmCfg().provider == "auto"


def test_build_llm_builtin_constructs_lazily():
    llm = build_llm(LlmCfg(enabled=True, provider="builtin"))
    assert isinstance(llm, BuiltinLlm)
    assert llm.repo_id == "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
    assert llm.filename == "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    assert llm._llm is None  # nothing loaded/downloaded yet


def test_builtin_generate_missing_extra_raises_actionable(monkeypatch):
    _fake_llama_cpp(monkeypatch, with_class=False)
    with pytest.raises(ImportError, match=r"llm-local"):
        BuiltinLlm().generate("hi")


def test_builtin_generate_mocked(monkeypatch):
    _fake_llama_cpp(monkeypatch, with_class=True)
    out = BuiltinLlm().generate("write docs", system="be brief")
    assert out == "echo:write docs"  # stripped


def test_auto_prefers_reachable_ollama_that_has_the_model(monkeypatch):
    monkeypatch.setattr(base_mod, "ollama_reachable", lambda *a, **k: True)
    monkeypatch.setattr(base_mod, "ollama_has_model", lambda *a, **k: True)
    assert isinstance(build_llm(LlmCfg(enabled=True, provider="auto")), OllamaLlm)


def test_auto_falls_back_to_builtin_when_ollama_reachable_but_missing_the_model(monkeypatch):
    """Same real bug as embeddings: Ollama running for one chat model (not the
    "auto" default) used to still get picked, then fail on first generate()."""
    monkeypatch.setattr(base_mod, "ollama_reachable", lambda *a, **k: True)
    monkeypatch.setattr(base_mod, "ollama_has_model", lambda *a, **k: False)
    monkeypatch.setattr(
        base_mod.importlib.util, "find_spec",
        lambda name: object() if name == "llama_cpp" else None,
    )
    assert isinstance(build_llm(LlmCfg(enabled=True, provider="auto")), BuiltinLlm)


def test_auto_falls_back_to_builtin_when_ollama_unreachable(monkeypatch):
    monkeypatch.setattr(base_mod, "ollama_reachable", lambda *a, **k: False)
    monkeypatch.setattr(
        base_mod.importlib.util, "find_spec",
        lambda name: object() if name == "llama_cpp" else None,
    )
    assert isinstance(build_llm(LlmCfg(enabled=True, provider="auto")), BuiltinLlm)


def test_auto_returns_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(base_mod, "ollama_reachable", lambda *a, **k: False)
    monkeypatch.setattr(base_mod.importlib.util, "find_spec", lambda name: None)
    assert build_llm(LlmCfg(enabled=True, provider="auto")) is None  # no raise
