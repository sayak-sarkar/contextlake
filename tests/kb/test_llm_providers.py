import subprocess
from types import SimpleNamespace

from contextlake.kb.llm import anthropic as anth_mod
from contextlake.kb.llm import cli as cli_mod
from contextlake.kb.llm.anthropic import AnthropicLlm
from contextlake.kb.llm.base import build_llm
from contextlake.kb.llm.cli import CliLlm


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


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_cli_generate_feeds_stdin_and_returns_stdout(monkeypatch):
    seen = {}

    def fake_run(argv, input=None, capture_output=None, text=None, timeout=None):
        seen["argv"] = argv
        seen["input"] = input
        seen["timeout"] = timeout
        return _FakeCompleted(returncode=0, stdout="the answer\n")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    llm = CliLlm(command="claude", timeout=99)
    out = llm.generate("Question?", system="Be brief.")

    assert out == "the answer"
    assert seen["argv"] == ["claude", "-p"]            # preset for `claude`
    assert seen["input"] == "Be brief.\n\nQuestion?"    # system prepended
    assert seen["timeout"] == 99


def test_cli_generate_custom_args_override_preset(monkeypatch):
    seen = {}
    def fake_run(argv, **k):
        seen["argv"] = argv
        return _FakeCompleted(stdout="x")
    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    CliLlm(command="mycli", args=["--flag"]).generate("hi")
    assert seen["argv"] == ["mycli", "--flag"]


def test_cli_generate_nonzero_exit_returns_blank(monkeypatch):
    monkeypatch.setattr(cli_mod.subprocess, "run",
                        lambda argv, **k: _FakeCompleted(returncode=1, stderr="boom"))
    assert CliLlm(command="claude").generate("hi") == ""


def test_cli_generate_timeout_returns_blank(monkeypatch):
    def boom(argv, **k):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
    monkeypatch.setattr(cli_mod.subprocess, "run", boom)
    assert CliLlm(command="claude").generate("hi") == ""


def test_cli_generate_missing_binary_raises(monkeypatch):
    def boom(argv, **k):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(cli_mod.subprocess, "run", boom)
    import pytest
    with pytest.raises(RuntimeError):
        CliLlm(command="nope").generate("hi")


def test_build_llm_returns_cli():
    cfg = SimpleNamespace(enabled=True, provider="cli", command="gemini", args=None)
    llm = build_llm(cfg)
    assert isinstance(llm, CliLlm)
    assert llm.command == "gemini"
    assert llm.args == ["-p"]
