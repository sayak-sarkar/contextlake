"""Tests for clone behaviour: dry-run, corruption cleanup, retry, command choice."""

from conftest import FakeCompleted
from contextlake import core
from contextlake.core import clone_repository


def _cfg(base_config, **over):
    cfg = base_config.copy()
    cfg.update(over)
    return cfg


def test_skip_existing_valid_repo(tmp_path, base_config, fake_subprocess):
    (tmp_path / "a" / ".git").mkdir(parents=True)
    status, _, _ = clone_repository("a", "grp/a", "http", "ssh", str(tmp_path), base_config)
    assert status == "skip"
    assert fake_subprocess.calls == []  # nothing executed


def test_dry_run_does_not_clone(tmp_path, base_config, fake_subprocess):
    status, _, msg = clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path), _cfg(base_config, dry_run="true")
    )
    assert status == "dry-run"
    assert fake_subprocess.calls == []


def test_clone_success(tmp_path, base_config, fake_subprocess, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda _: None)  # force git
    status, _, _ = clone_repository(
        "g/p", "grp/g/p", "http://x/g/p.git", "ssh", str(tmp_path),
        _cfg(base_config, clone_method="git"),
    )
    assert status == "ok"
    assert fake_subprocess.commands_matching("git", "clone")


def test_clone_prefers_glab_when_method_glab(tmp_path, base_config, fake_subprocess):
    clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path), _cfg(base_config, clone_method="glab")
    )
    # glab must receive the FULL group-qualified path, not the local dest path
    assert fake_subprocess.commands_matching("glab", "repo", "clone", "grp/g/p")


def test_clone_with_token_uses_native_git_and_keeps_the_secret_off_argv(
        tmp_path, base_config, fake_subprocess, monkeypatch):
    # A GITLAB_TOKEN makes glab unnecessary: auto must clone with plain git,
    # carrying the credential ONLY via the GIT_CONFIG_* child env — never in
    # the argv (visible in ps) and never in the URL (persisted to .git/config).
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-secret")
    monkeypatch.setattr(core.shutil, "which", lambda _: "/usr/bin/glab")  # glab present, still git
    seen = {}

    def handler(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs.get("env")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, _ = clone_repository(
        "g/p", "grp/g/p", "http://x/g/p.git", "ssh", str(tmp_path), base_config)

    assert status == "ok"
    assert seen["cmd"][:2] == ["git", "clone"]
    assert "glpat-secret" not in " ".join(seen["cmd"])
    env = seen["env"]
    assert env is not None and env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "Authorization: Basic" in env["GIT_CONFIG_VALUE_0"]
    assert env["GIT_CONFIG_COUNT"] == "1"


def test_clone_token_env_offsets_past_existing_git_config_entries(monkeypatch):
    # A user-set GIT_CONFIG_* entry must survive; ours appends after it.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Someone")
    env = core._git_token_env("tok")
    assert env["GIT_CONFIG_KEY_0"] == "user.name"          # untouched
    assert env["GIT_CONFIG_KEY_1"] == "http.extraHeader"   # appended
    assert env["GIT_CONFIG_COUNT"] == "2"


def test_clone_uses_each_platforms_basic_auth_username(monkeypatch):
    import base64
    # GitHub wants x-access-token, Bitbucket x-token-auth, GitLab/Gitea oauth2.
    for platform, user in (("gitlab", "oauth2"), ("github", "x-access-token"),
                           ("bitbucket", "x-token-auth"), ("gitea", "oauth2")):
        cmd, env = core._build_clone_cmd("g/p", "http://x/p.git", "/tmp/p", "auto",
                                         token="tok", platform=platform)
        assert cmd[:2] == ["git", "clone"]
        header = env["GIT_CONFIG_VALUE_0"].split("Basic ")[1]
        assert base64.b64decode(header).decode() == f"{user}:tok", platform


def test_clone_glab_fallback_is_gitlab_only(monkeypatch):
    # Without a token, auto uses glab only for GitLab; other platforms go
    # straight to plain git (glab cannot clone a GitHub repo).
    monkeypatch.setattr(core.shutil, "which", lambda _: "/usr/bin/glab")
    cmd, _ = core._build_clone_cmd("g/p", "http://x/p.git", "/tmp/p", "auto",
                                   token=None, platform="gitlab")
    assert cmd[0] == "glab"
    cmd, _ = core._build_clone_cmd("g/p", "http://x/p.git", "/tmp/p", "auto",
                                   token=None, platform="github")
    assert cmd[:2] == ["git", "clone"]


def test_corrupted_dir_cleaned_and_recloned(tmp_path, base_config, fake_subprocess):
    corrupt = tmp_path / "g" / "p"
    corrupt.mkdir(parents=True)
    (corrupt / "junk.txt").write_text("not a repo")
    status, _, _ = clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path), _cfg(base_config, clean_corrupted="true")
    )
    assert status == "ok"
    assert not (corrupt / "junk.txt").exists()  # cleaned


def test_corrupted_dir_errors_when_clean_disabled(tmp_path, base_config, fake_subprocess):
    corrupt = tmp_path / "g" / "p"
    corrupt.mkdir(parents=True)
    status, _, msg = clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path), _cfg(base_config, clean_corrupted="false")
    )
    assert status == "error"
    assert "not a git repo" in msg


def test_clone_retries_then_succeeds(tmp_path, base_config, fake_subprocess, no_sleep, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda _: None)
    state = {"n": 0}

    def handler(cmd, **kwargs):
        if "clone" in cmd:
            state["n"] += 1
            if state["n"] < 2:
                return FakeCompleted(returncode=1, stderr="connection reset")
            return FakeCompleted(returncode=0)
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, _ = clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path),
        _cfg(base_config, clone_method="git", max_retries="3"),
    )
    assert status == "ok"
    assert state["n"] == 2  # retried once


# --- clone_missing_repos loop: unified status_line rendering (H1) ----------

_LOOP_PROJECTS = {"g/p": {"archived": False, "http": "h", "ssh": "s", "default_branch": "main"}}


def test_clone_missing_repos_line_has_glyph_and_path(
    tmp_path, base_config, fake_subprocess, monkeypatch, gls_logs
):
    """H1: clone's per-repo line must carry a glyph, matching update/branches --
    no longer the bare '[i/total] path: message' form."""
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g: dict(_LOOP_PROJECTS))
    monkeypatch.setattr(core, "clone_repository", lambda *a, **k: ("ok", "g/p", "Cloned"))

    core.clone_missing_repos(str(tmp_path), base_config, "g")

    text = gls_logs.text
    # Pinned to the exact per-repo line (not just "a glyph appears somewhere in
    # the output"): the "Clone complete: ..." summary line also carries a "✓",
    # so a loose "'✓' in text" check would pass even against the old bare
    # "[1/1] g/p: Cloned" form. Anchoring on the full per-repo line shape is
    # the only assertion that actually distinguishes styled from bare.
    assert "[1/1] ✓ g/p: Cloned" in text


def test_clone_missing_repos_failure_line_has_fail_glyph(
    tmp_path, base_config, fake_subprocess, monkeypatch, gls_logs
):
    """H2: a failed clone renders with the fail glyph, not raw unstyled text."""
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g: dict(_LOOP_PROJECTS))
    monkeypatch.setattr(core, "clone_repository", lambda *a, **k: ("error", "g/p", "boom"))

    core.clone_missing_repos(str(tmp_path), base_config, "g")

    text = gls_logs.text
    assert "✗" in text
    assert "boom" in text
