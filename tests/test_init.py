"""Tests for `contextlake init` — the guided config generator."""

from argparse import Namespace

import pytest

from contextlake import init_cmd
from contextlake.config import load_config


def _args(**over):
    base = dict(platform=None, group=None, work_dir=None, kb=None,
                embeddings=False, yes=True, force=False)
    base.update(over)
    return Namespace(**base)


def _run(tmp_path, monkeypatch, **over):
    # Point the writer at an isolated HOME; stdin is not a TTY under pytest, so
    # cmd_init runs non-interactively regardless.
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    return init_cmd.cmd_init(_args(**over))


def test_init_writes_both_configs(tmp_path, monkeypatch):
    rc = _run(tmp_path, monkeypatch, platform="github", group="acme")
    assert rc == 0
    ini = (tmp_path / ".contextlake.ini").read_text()
    assert "platform = github" in ini
    assert "gitlab_group = acme" in ini
    kb = (tmp_path / ".contextlake/kb.toml").read_text()
    assert 'store_dir = "~/.contextlake/kb"' in kb
    assert "enabled = false" in kb  # embeddings off unless asked


def test_init_gitlab_omits_platform_key(tmp_path, monkeypatch):
    # gitlab is the default, so the key is left out (cleaner config)
    _run(tmp_path, monkeypatch, platform="gitlab", group="acme")
    assert "platform =" not in (tmp_path / ".contextlake.ini").read_text()


def test_init_embeddings_flag_enables_semantic(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, group="acme", embeddings=True)
    assert "enabled = true" in (tmp_path / ".contextlake/kb.toml").read_text()


def test_init_no_kb_writes_only_mirror(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, group="acme", kb=False)
    assert (tmp_path / ".contextlake.ini").exists()
    assert not (tmp_path / ".contextlake/kb.toml").exists()


def test_init_does_not_clobber_without_force(tmp_path, monkeypatch):
    cfg = tmp_path / ".contextlake.ini"
    cfg.write_text("[contextlake]\nwork_dir = /keep/me\n")
    _run(tmp_path, monkeypatch, platform="github", group="acme")
    assert "/keep/me" in cfg.read_text()          # untouched
    assert "platform = github" not in cfg.read_text()


def test_init_force_overwrites(tmp_path, monkeypatch):
    cfg = tmp_path / ".contextlake.ini"
    cfg.write_text("[contextlake]\nwork_dir = /old\n")
    _run(tmp_path, monkeypatch, platform="github", group="acme", force=True)
    assert "gitlab_group = acme" in cfg.read_text()


def test_init_rejects_unknown_platform(tmp_path, monkeypatch):
    rc = _run(tmp_path, monkeypatch, platform="sourceforge", group="acme")
    assert rc == 2
    assert not (tmp_path / ".contextlake.ini").exists()


def test_init_never_writes_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret-value")
    _run(tmp_path, monkeypatch, platform="github", group="acme")
    text = (tmp_path / ".contextlake.ini").read_text()
    assert "ghp-secret-value" not in text  # auth is by env-var reference only


def test_generated_config_loads_and_drives_the_tool(tmp_path, monkeypatch):
    # The whole point: what init writes must be valid config the tool reads back.
    ini = tmp_path / ".contextlake.ini"
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(ini))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    init_cmd.cmd_init(_args(platform="bitbucket", group="acme", work_dir=str(tmp_path / "w")))

    cfg = load_config(str(ini))
    assert cfg.get("platform") == "bitbucket"
    assert cfg.get("gitlab_group") == "acme"


@pytest.mark.parametrize("platform,env", [
    ("gitlab", "GITLAB_TOKEN"), ("github", "GITHUB_TOKEN"),
    ("bitbucket", "BITBUCKET_TOKEN"), ("codeberg", "GITEA_TOKEN"),
])
def test_init_reports_the_right_token_env(tmp_path, monkeypatch, gls_logs, platform, env):
    monkeypatch.delenv(env, raising=False)
    _run(tmp_path, monkeypatch, platform=platform, group="acme")
    # the auth hint names the platform's token env var
    assert env in gls_logs.text


def test_init_next_hint_matches_semantic_choice(tmp_path, monkeypatch, gls_logs):
    # Enabling semantic search must recommend [kb-full] (which ships the embedder),
    # not [kb] — otherwise the very next `bootstrap` embed step fails for every repo.
    _run(tmp_path, monkeypatch, group="acme", embeddings=True)
    assert 'contextlake[kb-full]' in gls_logs.text
    assert 'contextlake[kb]"' not in gls_logs.text  # the bare-kb hint must not appear


def test_init_next_hint_plain_kb_without_semantic(tmp_path, monkeypatch, gls_logs):
    _run(tmp_path, monkeypatch, group="acme", embeddings=False)
    assert 'contextlake[kb]' in gls_logs.text
    assert 'kb-full' not in gls_logs.text


# --- optional connector prompt ------------------------------------------------

def test_init_non_interactive_skips_connector_prompt_and_writes_no_sources(
        tmp_path, monkeypatch):
    # --yes (the default in _args) means non-interactive: the prompt is never
    # reached at all.
    _run(tmp_path, monkeypatch, group="acme")
    kb = (tmp_path / ".contextlake/kb.toml").read_text()
    assert "[[sources]]" not in kb


def test_init_connector_prompt_declined_writes_no_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)
    # accept every prompt's own default -- including "Connect a data source?"
    # whose default is False -- exactly like a user who just hits enter throughout.
    monkeypatch.setattr(init_cmd, "_ask_yn", lambda prompt, default: default)
    monkeypatch.setattr(init_cmd, "_ask", lambda prompt, default: default)

    rc = init_cmd.cmd_init(_args(yes=False, group="acme"))
    assert rc == 0
    kb = (tmp_path / ".contextlake/kb.toml").read_text()
    assert "[[sources]]" not in kb


def test_init_connector_prompt_accepted_adds_source(tmp_path, monkeypatch):
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)

    def fake_ask_yn(prompt, default):
        return True if "Connect a data source" in prompt else default

    def fake_ask(prompt, default):
        if "Source type" in prompt:
            return "atlassian"
        if "Source name" in prompt:
            return "jira"
        if "MCP server URL" in prompt:
            return "https://mcp.example.com"
        return default

    monkeypatch.setattr(init_cmd, "_ask_yn", fake_ask_yn)
    monkeypatch.setattr(init_cmd, "_ask", fake_ask)

    rc = init_cmd.cmd_init(_args(yes=False, group="acme"))
    assert rc == 0
    kb = (tmp_path / ".contextlake/kb.toml").read_text()
    assert 'name = "jira"' in kb
    assert 'type = "atlassian"' in kb
    assert 'mcp = "https://mcp.example.com"' in kb


def test_init_connector_prompt_never_asks_for_a_secret_value(tmp_path, monkeypatch):
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)
    seen_prompts = []

    def fake_ask_yn(prompt, default):
        return True if "Connect a data source" in prompt else default

    def fake_ask(prompt, default):
        seen_prompts.append(prompt)
        if "Source type" in prompt:
            return "atlassian"
        if "Source name" in prompt:
            return "jira"
        return default

    monkeypatch.setattr(init_cmd, "_ask_yn", fake_ask_yn)
    monkeypatch.setattr(init_cmd, "_ask", fake_ask)

    init_cmd.cmd_init(_args(yes=False, group="acme"))
    assert not any(
        "token" in p.lower() or "secret" in p.lower() or "password" in p.lower()
        for p in seen_prompts
    )
