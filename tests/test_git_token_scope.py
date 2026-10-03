"""The forge token header reaches the forge's own origin and no other.

The incident: the token travelled as a bare `http.extraHeader`, which git sends on
every HTTPS request. `mirror update` and `mirror branches` run over every clone in
the workspace, so a third-party clone's origin, or any second remote that
`fetch --all` reaches, received the forge token. Reproduced before the fix with
git's own matcher: `git config --get-urlmatch http.extraHeader <third-party URL>`
returned the header.

These tests ask the same question of git itself. Each one captures the child env
the real code path hands to git (clone through `clone_repository`, refresh
through `update_repository`), then runs `git config --get-urlmatch` under that env
against the clone-URL shapes each forge's API actually returns. No network: the
command only reads config. It runs in tmp_path, which is not a repository, with
the system config off and HOME already isolated by conftest.
"""

import os
import subprocess

import pytest

from conftest import FakeCompleted, make_local_repo
from contextlake import core
from contextlake.core import clone_repository, update_repository

# (config, URLs that must receive the header, URLs that must not).
# Positive URLs are the shapes each enumerator stores as http_url_to_repo: GitLab's
# http_url_to_repo, GitHub's and Gitea's clone_url, Bitbucket's https clone href
# (which carries a username).
CASES = {
    "gitlab default": (
        {"platform": "gitlab"},
        ["https://gitlab.com/grp/team/api.git"],
        ["https://github.com/someone-else/lib.git",
         "https://gitlab.com.evil.net/grp/p.git",
         "http://gitlab.com/grp/p.git"]),
    "gitlab via gitlab_host": (
        {"platform": "gitlab", "gitlab_host": "gitlab.example.com"},
        ["https://gitlab.example.com/grp/team/api.git"],
        ["https://gitlab.com/grp/p.git",
         "https://gitlab.example.com.evil.net/grp/p.git",
         "https://evil.net/gitlab.example.com/grp/p.git"]),
    "github default": (
        {"platform": "github"},
        ["https://github.com/acme/api.git"],
        ["https://gitlab.com/acme/api.git", "https://github.com.evil.net/acme/api.git"]),
    "github enterprise": (
        {"platform": "github", "api_base": "https://ghe.example.com/api/v3"},
        ["https://ghe.example.com/acme/api.git"],
        ["https://github.com/acme/api.git"]),
    "bitbucket default": (
        {"platform": "bitbucket"},
        ["https://someuser@bitbucket.org/acme/api.git"],
        ["https://github.com/acme/api.git"]),
    "gitea default": (
        {"platform": "gitea"},
        ["https://gitea.com/acme/api.git"],
        ["https://codeberg.org/acme/api.git"]),
    "codeberg": (
        {"platform": "codeberg"},
        ["https://codeberg.org/acme/api.git"],
        ["https://gitea.com/acme/api.git"]),
}


def _header_for(url, env, cwd):
    """What git would send as http.extraHeader for ``url`` under ``env``, or None."""
    run_env = {**env, "GIT_CONFIG_NOSYSTEM": "1"}
    res = subprocess.run(["git", "config", "--get-urlmatch", "http.extraHeader", url],
                         cwd=cwd, env=run_env, capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None


@pytest.fixture
def tokens(monkeypatch):
    for name in ("GITLAB_TOKEN", "GITHUB_TOKEN", "BITBUCKET_TOKEN", "GITEA_TOKEN"):
        monkeypatch.setenv(name, "dummy-token")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)


def _clone_env(tmp_path, config, fake_subprocess, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda _: None)
    seen = {}

    def handler(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, _ = clone_repository("acme/api", "acme/api", "https://unused/x.git", "",
                                    str(tmp_path / "work"), config)
    assert status == "ok"
    return seen["env"]


def _update_env(tmp_path, config, fake_subprocess, monkeypatch):
    monkeypatch.setattr(core, "check_repository_safety", lambda *a, **k: (True, []))
    make_local_repo(tmp_path / "work", "acme/api")
    seen = {}

    def handler(cmd, **kwargs):
        if "fetch" in cmd:
            seen["env"] = kwargs.get("env")
        if "rev-parse" in cmd and "--abbrev-ref" in cmd:
            return FakeCompleted(stdout="main\n")
        return FakeCompleted()

    fake_subprocess.handler = handler
    update_repository("acme/api", str(tmp_path / "work"), config)
    return seen["env"]


@pytest.mark.parametrize("path", ["clone", "update"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_header_matches_the_forge_and_nothing_else(
        case, path, tmp_path, base_config, fake_subprocess, monkeypatch, tokens):
    over, forge_urls, other_urls = CASES[case]
    config = {**base_config, **over, "clone_method": "git"}
    grab = _clone_env if path == "clone" else _update_env
    env = grab(tmp_path, config, fake_subprocess, monkeypatch)
    assert env is not None

    for url in forge_urls:
        assert (_header_for(url, env, tmp_path) or "").startswith("Authorization: Basic "), url
    for url in other_urls:
        assert _header_for(url, env, tmp_path) is None, url


def test_gitlab_host_env_var_sets_the_scope_port_included(
        tmp_path, base_config, fake_subprocess, monkeypatch, tokens):
    monkeypatch.setenv("GITLAB_HOST", "https://git.example.com:8443")
    env = _update_env(tmp_path, {**base_config, "platform": "gitlab"},
                      fake_subprocess, monkeypatch)
    assert _header_for("https://git.example.com:8443/grp/p.git", env, tmp_path)
    assert _header_for("https://git.example.com/grp/p.git", env, tmp_path) is None


def test_a_third_party_clone_in_the_workspace_gets_no_header(
        tmp_path, base_config, fake_subprocess, monkeypatch, tokens):
    """The audit's scenario end to end in config terms: a GitLab workspace holding a
    clone whose origin is on github.com."""
    env = _update_env(tmp_path, {**base_config, "platform": "gitlab"},
                      fake_subprocess, monkeypatch)
    assert _header_for("https://github.com/someone-else/vendored-lib.git", env,
                       tmp_path) is None


def test_scope_is_required():
    with pytest.raises(ValueError):
        core._git_token_env("tok", "oauth2", "")
    with pytest.raises(TypeError):
        core._git_token_env("tok", "oauth2")  # no default to fall back on


def test_the_secret_stays_off_argv_and_out_of_the_key(
        tmp_path, base_config, fake_subprocess, monkeypatch, tokens):
    env = _clone_env(tmp_path, {**base_config, "clone_method": "git"},
                     fake_subprocess, monkeypatch)
    count = int(env["GIT_CONFIG_COUNT"])
    keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(count)]
    assert keys[-1] == "http.https://gitlab.com/.extraHeader"
    assert all("dummy-token" not in k for k in keys)
    assert all("dummy-token" not in " ".join(c) for c in fake_subprocess.calls)
    assert os.environ.get("GIT_CONFIG_COUNT") is None  # the parent env is untouched
