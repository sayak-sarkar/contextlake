import subprocess
from types import SimpleNamespace

import pytest

from contextlake.cli import main
from contextlake.kb.config import KbConfig, LlmCfg, apply_llm_overrides
from contextlake.kb.llm import anthropic as anth_mod
from contextlake.kb.llm import cli as cli_mod
from contextlake.kb.llm.anthropic import AnthropicLlm
from contextlake.kb.llm.base import build_llm
from contextlake.kb.llm.cli import CliLlm


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


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

    def fake_run(argv, input=None, capture_output=None, text=None, timeout=None, env=None):
        seen["argv"] = argv
        seen["input"] = input
        seen["timeout"] = timeout
        return _FakeCompleted(returncode=0, stdout="the answer\n")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    llm = CliLlm(command="claude", timeout=99)
    out = llm.generate("Question?", system="Be brief.")

    assert out == "the answer"
    assert seen["argv"] == ["claude", "-p", "--safe-mode"]   # preset for `claude`
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


def test_cli_args_override_populated_preset(monkeypatch):
    seen = {}

    def fake_run(argv, **k):
        seen["argv"] = argv
        return _FakeCompleted(stdout="x")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    # `claude` HAS a preset (["-p"]); explicit args must win over it
    CliLlm(command="claude", args=["--model", "opus"]).generate("hi")
    assert seen["argv"] == ["claude", "--model", "opus"]


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


def test_cli_strips_its_own_api_key_from_the_child_env(monkeypatch):
    """The whole point of provider=cli is reusing a subscription instead of an API
    key -- but the CLI itself (e.g. `claude -p`) treats its own API key env var as
    an auth override that takes precedence over the subscription login. A key set
    for an unrelated reason (testing a different [llm] provider, another tool)
    must not silently flip this provider onto pay-per-token auth."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-the-child")
    seen = {}

    def fake_run(argv, **k):
        seen["env"] = k.get("env")
        return _FakeCompleted(stdout="ok")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    CliLlm(command="claude").generate("hi")
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_cli_leaves_unrelated_env_vars_and_unknown_commands_alone(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-...")
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    seen = {}

    def fake_run(argv, **k):
        seen["env"] = k.get("env")
        return _FakeCompleted(stdout="ok")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    # An unrecognised command's auth model is unknown -- nothing is stripped.
    CliLlm(command="my-own-cli").generate("hi")
    assert seen["env"]["ANTHROPIC_API_KEY"] == "sk-..."
    assert seen["env"]["SOME_OTHER_VAR"] == "keep-me"


def test_cli_matches_preset_and_auth_vars_by_basename(monkeypatch):
    """`command` can be path-qualified (e.g. a shim or a non-PATH install) --
    the preset args and the auth-var strip must still key off `claude`, not
    fail closed on the full path."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-the-child")
    seen = {}

    def fake_run(argv, **k):
        seen["argv"] = argv
        seen["env"] = k.get("env")
        return _FakeCompleted(stdout="ok")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    CliLlm(command="/usr/local/bin/claude").generate("hi")
    assert seen["argv"] == ["/usr/local/bin/claude", "-p", "--safe-mode"]
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_build_llm_returns_cli():
    cfg = SimpleNamespace(enabled=True, provider="cli", command="gemini", args=None)
    llm = build_llm(cfg)
    assert isinstance(llm, CliLlm)
    assert llm.command == "gemini"
    assert llm.args == ["-p", "{PROMPT}"]


def test_cli_gemini_preset_puts_prompt_on_argv_not_stdin(monkeypatch):
    """`gemini -p` is a required string value, not a boolean headless-mode
    switch -- confirmed live: `gemini -p` with the prompt only on stdin produced
    a yargs usage dump, while `gemini -p "<text>"` reached the API. The preset's
    `{PROMPT}` placeholder must be substituted with the real prompt on argv, and
    stdin must be left empty (not double-fed)."""
    seen = {}

    def fake_run(argv, input=None, **k):
        seen["argv"] = argv
        seen["input"] = input
        return _FakeCompleted(stdout="ok")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    CliLlm(command="gemini").generate("Question?", system="Be brief.")
    assert seen["argv"] == ["gemini", "-p", "Be brief.\n\nQuestion?"]
    assert seen["input"] is None


def test_cli_claude_and_codex_still_feed_prompt_on_stdin(monkeypatch):
    """Only gemini's preset uses the {PROMPT} placeholder -- claude/codex (and
    any custom `args` without the placeholder) keep the prompt off argv."""
    seen = {}

    def fake_run(argv, input=None, **k):
        seen["argv"] = argv
        seen["input"] = input
        return _FakeCompleted(stdout="ok")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    CliLlm(command="claude").generate("hi")
    assert "{PROMPT}" not in seen["argv"]
    assert seen["input"] == "hi"


def test_llmcfg_new_fields_defaults_and_parse():
    cfg = LlmCfg()
    assert cfg.max_tokens == 4096
    assert cfg.command is None
    assert cfg.args is None
    cfg2 = LlmCfg(provider="cli", command="gemini", args=["-p"], max_tokens=1024)
    assert cfg2.command == "gemini" and cfg2.args == ["-p"] and cfg2.max_tokens == 1024


def test_llmcfg_api_key_env_defaults_to_none_until_resolved():
    # api_key_env is unset (None) at construction time — pydantic v2 doesn't
    # re-run validators on plain attribute assignment (e.g. `--llm anthropic`
    # via apply_llm_overrides), so per-provider defaulting can't safely live in a
    # model_validator. It's resolved at read-time instead, in build_llm().
    assert LlmCfg(provider="anthropic").api_key_env is None
    assert LlmCfg(provider="anthropic", api_key_env="CUSTOM_KEY").api_key_env == "CUSTOM_KEY"


def test_apply_llm_overrides_anthropic_resolves_key_env():
    # Regression test: `contextlake wiki --llm anthropic` goes through
    # apply_llm_overrides(), which sets cfg.llm.provider by plain attribute
    # assignment on an already-constructed LlmCfg. A model_validator would not
    # re-run on that assignment, so api_key_env must be resolved at build_llm()
    # read-time, not at LlmCfg construction time.
    cfg = KbConfig()
    apply_llm_overrides(cfg, provider="anthropic", model=None)
    llm = build_llm(cfg.llm)
    assert isinstance(llm, AnthropicLlm)
    assert llm.api_key_env == "ANTHROPIC_API_KEY"


def test_doctor_reports_anthropic_key_status(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        '[llm]\nenabled = true\nprovider = "anthropic"\n'
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "wiki LLM" in out
    assert "anthropic" in out
    assert "ANTHROPIC_API_KEY" in out  # names the env var the user must set


def test_doctor_reports_cli_provider_path_status(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        '[llm]\nenabled = true\nprovider = "cli"\ncommand = "definitely-not-a-real-binary-xyz"\n'
    )
    _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "wiki LLM" in out
    assert "cli" in out
    assert "not on PATH" in out
