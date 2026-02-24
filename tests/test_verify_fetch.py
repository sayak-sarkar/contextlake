"""Tests for verify (nested-repo detection), cache loading and fetch."""

import json

from conftest import FakeCompleted
from contextlake.core import (
    fetch_gitlab_projects,
    find_nested_repos,
    load_gitlab_projects,
    to_local_path,
    verify_repository,
)


def test_to_local_path_strips_group_prefix():
    # Local clones mirror the tree below the group, so the group prefix is dropped.
    assert to_local_path("acme/team/api", "acme") == "team/api"
    assert to_local_path("acme/sub/team/api", "acme/sub") == "team/api"
    # paths outside the configured group are left untouched
    assert to_local_path("other/repo", "acme") == "other/repo"


def test_find_nested_repos():
    # Bug #7 recovery: a repo inside another repo is a corruption signal.
    repos = ["group/a", "group/a/inner", "group/b"]
    assert find_nested_repos(repos) == ["group/a/inner"]


def test_find_nested_repos_none_when_flat():
    assert find_nested_repos(["a", "b", "c"]) == []


def test_verify_repository_classifications(tmp_path):
    projects = {"a": {"archived": False}}
    (tmp_path / "a" / ".git").mkdir(parents=True)
    assert verify_repository("a", projects, str(tmp_path), {})[0] == "ok"
    assert verify_repository("ghost", projects, str(tmp_path), {})[0] == "extra"
    assert verify_repository("a-missing", {"a-missing": {}}, str(tmp_path), {})[0] == "missing"
    (tmp_path / "b").mkdir()
    assert verify_repository("b", {"b": {}}, str(tmp_path), {})[0] == "invalid"


def test_load_normalizes_list_cache(tmp_path, base_config):
    # Bug #6: a list-shaped JSON cache used to be silently discarded.
    cache = tmp_path / "p.json"
    cache.write_text(json.dumps([
        {"path_with_namespace": "g/p", "http_url_to_repo": "h", "ssh_url_to_repo": "s",
         "archived": False, "default_branch": "main"},
    ]))
    cfg = base_config.copy()
    cfg.update(cache_dir=str(tmp_path), cache_json="p.json", cache_file="p.txt")
    data = load_gitlab_projects(cfg, "g")
    # keyed by local path (group stripped), with the full path retained
    assert "p" in data and data["p"]["http"] == "h"
    assert data["p"]["full_path"] == "g/p"


def test_fetch_paginates_and_writes_both_caches(tmp_path, base_config, fake_subprocess):
    page1 = [{"path_with_namespace": f"g/r{i}", "http_url_to_repo": f"h{i}",
              "ssh_url_to_repo": f"s{i}", "archived": False, "default_branch": "main"}
             for i in range(100)]
    page2 = [{"path_with_namespace": "g/last", "http_url_to_repo": "hl",
              "ssh_url_to_repo": "sl", "archived": False, "default_branch": "dev"}]

    def handler(cmd, **kwargs):
        endpoint = cmd[-1]
        # Use the &-prefixed param so "per_page=100" doesn't match "page=1".
        if "&page=1" in endpoint:
            return FakeCompleted(stdout=json.dumps(page1))
        if "&page=2" in endpoint:
            return FakeCompleted(stdout=json.dumps(page2))
        return FakeCompleted(stdout="[]")

    fake_subprocess.handler = handler
    cfg = base_config.copy()
    cfg.update(cache_dir=str(tmp_path), cache_json="p.json", cache_file="p.txt")

    result = fetch_gitlab_projects("g", cfg)
    assert len(result) == 101
    # keyed by local path (group "g" stripped); full path kept in the value
    assert "last" in result
    assert result["last"]["full_path"] == "g/last"

    # JSON cache written (local-keyed)
    assert json.loads((tmp_path / "p.json").read_text())["last"]["default_branch"] == "dev"
    # pipe-delimited text cache written: path|ssh|http|default_branch|archived
    lines = (tmp_path / "p.txt").read_text().strip().splitlines()
    assert any(line.startswith("last|sl|hl|dev|") for line in lines)


def test_fetch_uses_url_encoded_group(tmp_path, base_config, fake_subprocess):
    fake_subprocess.handler = lambda cmd, **k: FakeCompleted(stdout="[]")
    cfg = base_config.copy()
    cfg.update(cache_dir=str(tmp_path), cache_json="p.json", cache_file="p.txt")
    fetch_gitlab_projects("group/sub", cfg)
    # the subgroup slash must be percent-encoded for the GitLab API
    assert any("group%2Fsub" in " ".join(c) for c in fake_subprocess.calls)
