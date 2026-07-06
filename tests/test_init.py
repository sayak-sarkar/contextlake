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
def test_init_reports_the_right_token_env(tmp_path, monkeypatch, caplog, platform, env):
    import logging
    monkeypatch.delenv(env, raising=False)
    with caplog.at_level(logging.INFO, logger="contextlake"):
        _run(tmp_path, monkeypatch, platform=platform, group="acme")
    # the auth hint names the platform's token env var
    assert env in caplog.text


def test_init_next_hint_matches_semantic_choice(tmp_path, monkeypatch, caplog):
    # Enabling semantic search must recommend [kb-full] (which ships the embedder),
    # not [kb] — otherwise the very next `bootstrap` embed step fails for every repo.
    import logging
    with caplog.at_level(logging.INFO, logger="contextlake"):
        _run(tmp_path, monkeypatch, group="acme", embeddings=True)
    assert 'contextlake[kb-full]' in caplog.text
    assert 'contextlake[kb]"' not in caplog.text  # the bare-kb hint must not appear


def test_init_next_hint_plain_kb_without_semantic(tmp_path, monkeypatch, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="contextlake"):
        _run(tmp_path, monkeypatch, group="acme", embeddings=False)
    assert 'contextlake[kb]' in caplog.text
    assert 'kb-full' not in caplog.text
