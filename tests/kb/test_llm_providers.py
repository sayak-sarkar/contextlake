from types import SimpleNamespace

from contextlake.kb.llm import anthropic as anth_mod
from contextlake.kb.llm.anthropic import AnthropicLlm
from contextlake.kb.llm.base import build_llm


def test_anthropic_generate_builds_request_and_parses_text(monkeypatch):
    captured = {}

    def fake_post_json(url, payload, timeout, headers=None):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout"] = timeout
        return {"content": [{"type": "text", "text": "hello "},
                            {"type": "text", "text": "world"}]}

    monkeypatch.setattr(anth_mod, "post_json", fake_post_json)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = AnthropicLlm(model="claude-opus-4-8", max_tokens=2048, timeout=42)

    out = llm.generate("Summarize this repo.", system="You are terse.")

    assert out == "hello world"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["timeout"] == 42
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["payload"]["model"] == "claude-opus-4-8"
    assert captured["payload"]["max_tokens"] == 2048
    assert captured["payload"]["system"] == "You are terse."
    assert captured["payload"]["messages"] == [{"role": "user", "content": "Summarize this repo."}]


def test_anthropic_generate_omits_system_when_absent(monkeypatch):
    monkeypatch.setattr(anth_mod, "post_json",
                        lambda *a, **k: {"content": [{"type": "text", "text": "ok"}]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = AnthropicLlm()
    assert llm.generate("hi") == "ok"


def test_anthropic_generate_empty_content_returns_blank(monkeypatch):
    monkeypatch.setattr(anth_mod, "post_json", lambda *a, **k: {"content": []})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert AnthropicLlm().generate("hi") == ""


def test_build_llm_returns_anthropic():
    cfg = SimpleNamespace(enabled=True, provider="anthropic",
                          model="claude-haiku-4-5", api_key_env="ANTHROPIC_API_KEY")
    llm = build_llm(cfg)
    assert isinstance(llm, AnthropicLlm)
    assert llm.model == "claude-haiku-4-5"
