"""Canonical repo identity: normalized-remote ids, independent of local path."""

import subprocess

from contextlake.kb.repo_identity import (
    canonical_repo_id,
    is_own_gitdir,
    normalize_remote_url,
    resolve_repo_id,
)


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True, text=True)


def _init_repo(path, *, remote=None, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "a@b.c")
    _git(path, "config", "user.name", "a")
    if commit:
        (path / "f.txt").write_text("x")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "init")
    if remote:
        _git(path, "remote", "add", "origin", remote)


def test_normalize_remote_url_https_and_ssh_agree():
    https = normalize_remote_url("https://gitlab.example.com/team/api.git")
    ssh = normalize_remote_url("git@gitlab.example.com:team/api.git")
    assert https == ssh == "gitlab.example.com/team/api"


def test_normalize_remote_url_strips_userinfo_and_trailing_slash():
    with_token = normalize_remote_url("https://x-token:secret@gitlab.example.com/team/api.git/")
    assert with_token == "gitlab.example.com/team/api"


def test_normalize_remote_url_lowercases():
    assert normalize_remote_url("https://GitLab.Example.com/Team/API.git") \
        == "gitlab.example.com/team/api"


def test_canonical_repo_id_from_real_git_remote(tmp_path):
    repo = tmp_path / "clone"
    _init_repo(repo, remote="https://example.com/acme/widgets.git")
    assert canonical_repo_id(str(repo)) == "example.com/acme/widgets"


def test_canonical_repo_id_none_without_remote(tmp_path):
    repo = tmp_path / "clone"
    _init_repo(repo)
    assert canonical_repo_id(str(repo)) is None


def test_resolve_repo_id_prefers_remote_over_fallback(tmp_path):
    repo = tmp_path / "clone"
    _init_repo(repo, remote="https://example.com/acme/widgets.git")
    assert resolve_repo_id(str(repo)) == "example.com/acme/widgets"


def test_resolve_repo_id_falls_back_to_name_plus_root_commit(tmp_path):
    repo = tmp_path / "my-local-repo"
    _init_repo(repo)
    rid = resolve_repo_id(str(repo))
    assert rid.startswith("my-local-repo@")
    assert len(rid.split("@", 1)[1]) == 12   # short root-commit hash


def test_resolve_repo_id_two_remoteless_repos_with_different_history_differ(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _init_repo(a)
    _init_repo(b)
    assert resolve_repo_id(str(a)) != resolve_repo_id(str(b))


def test_resolve_repo_id_no_remote_no_commits_is_just_the_dirname(tmp_path):
    repo = tmp_path / "empty-repo"
    _init_repo(repo, commit=False)
    assert resolve_repo_id(str(repo)) == "empty-repo"


def test_is_own_gitdir_true_for_a_real_repo_root(tmp_path):
    repo = tmp_path / "clone"
    _init_repo(repo, remote="https://example.com/acme/widgets.git")
    assert is_own_gitdir(str(repo)) is True


def test_is_own_gitdir_false_for_a_corrupted_git_that_walks_up_to_an_ancestor(tmp_path):
    """The exact bug scenario: a real repo at the workspace root, and a nested
    directory whose own .git is incomplete -- `git -C <nested>` silently walks
    up and resolves to the ancestor's real repo instead of failing. Trusting
    that resolution would misattribute the ancestor's identity/history to the
    nested (broken) directory; is_own_gitdir must catch it."""
    ancestor = tmp_path / "workspace"
    _init_repo(ancestor, remote="https://example.com/acme/ancestor.git")
    broken = ancestor / "nested" / "broken-checkout"
    broken.mkdir(parents=True)
    (broken / ".git").mkdir()
    (broken / ".git" / "HEAD").write_text("not a real gitdir\n")
    assert is_own_gitdir(str(broken)) is False
    # Sanity: git really does walk up and resolve the ancestor's remote here --
    # this is the trap is_own_gitdir exists to catch, not a hypothetical.
    assert canonical_repo_id(str(broken)) == "example.com/acme/ancestor"
