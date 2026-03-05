"""Tests for verify (nested-repo detection), cache loading and fetch."""

import json
import os
import re
import urllib.error

from conftest import FakeCompleted
from contextlake import core
from contextlake.core import (
    _gitlab_api_base,
    _gitlab_token,
    configure_network_resilience,
    fetch_gitlab_projects,
    find_nested_repos,
    load_gitlab_projects,
    to_local_path,
    verify_repository,
)


class _FakeResp:
    """Minimal context-manager stand-in for urllib's HTTPResponse."""

    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_pager(pages, captured):
    """A fake urlopen that serves ``pages`` by the URL's page= param and records
    each (url, headers) so a test can assert the request shape."""
    def fake_urlopen(req, timeout=None):
        captured.append((req.full_url, dict(req.header_items())))
        m = re.search(r"[?&]page=(\d+)", req.full_url)
        page = int(m.group(1)) if m else 1
        return _FakeResp(pages.get(page, []))
    return fake_urlopen


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


def test_gitlab_token_resolution(monkeypatch, base_config):
    assert _gitlab_token(base_config) is None  # hermetic: no ambient token
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xyz")
    assert _gitlab_token(base_config) == "glpat-xyz"
    # a configurable env var name is honored
    monkeypatch.delenv("GITLAB_TOKEN")
    cfg = {**base_config, "gitlab_token_env": "MY_TOK"}
    monkeypatch.setenv("MY_TOK", "tok2")
    assert _gitlab_token(cfg) == "tok2"


def test_gitlab_api_base(monkeypatch, base_config):
    assert _gitlab_api_base(base_config) == "https://gitlab.com"  # default
    monkeypatch.setenv("GITLAB_HOST", "gitlab.example.com")
    assert _gitlab_api_base(base_config) == "https://gitlab.example.com"
    monkeypatch.setenv("GITLAB_HOST", "https://self.host/")  # scheme + trailing slash
    assert _gitlab_api_base(base_config) == "https://self.host"


def test_fetch_uses_http_when_token_set(tmp_path, base_config, monkeypatch, fake_subprocess):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-secret")
    pages = {
        1: [{"path_with_namespace": f"g/r{i}", "http_url_to_repo": f"h{i}",
             "ssh_url_to_repo": f"s{i}", "archived": False, "default_branch": "main"}
            for i in range(100)],
        2: [{"path_with_namespace": "g/last", "http_url_to_repo": "hl",
             "ssh_url_to_repo": "sl", "archived": False, "default_branch": "dev"}],
    }
    captured = []
    monkeypatch.setattr(core.urllib.request, "urlopen", _http_pager(pages, captured))
    cfg = {**base_config, "cache_dir": str(tmp_path), "cache_json": "p.json", "cache_file": "p.txt"}

    result = fetch_gitlab_projects("g", cfg)

    assert len(result) == 101 and result["last"]["full_path"] == "g/last"
    assert fake_subprocess.calls == []  # native HTTP used, glab never shelled out
    url0, hdrs0 = captured[0]
    assert "gitlab.com/api/v4/groups/g/projects" in url0
    assert hdrs0.get("Private-token") == "glpat-secret"  # token in header...
    assert "glpat-secret" not in url0                    # ...never in the URL


def test_fetch_http_retries_transient_dns(tmp_path, base_config, monkeypatch, no_sleep):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("dial tcp: lookup gitlab.com: i/o timeout")
        return _FakeResp([])  # empty page -> stop

    monkeypatch.setattr(core.urllib.request, "urlopen", flaky)
    cfg = {**base_config, "cache_dir": str(tmp_path), "cache_json": "p.json", "cache_file": "p.txt"}
    fetch_gitlab_projects("g", cfg)
    assert calls["n"] == 2  # the i/o timeout was retried, not fatal


def test_fetch_no_token_and_no_glab_fails_clean(tmp_path, base_config, monkeypatch):
    # No token, and glab is absent -> FileNotFoundError must not crash; empty result.
    def boom(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'glab'")

    monkeypatch.setattr(core.subprocess, "run", boom)
    cfg = {**base_config, "cache_dir": str(tmp_path), "cache_json": "p.json", "cache_file": "p.txt"}
    assert fetch_gitlab_projects("g", cfg) == {}


def test_configure_network_resilience_sets_and_respects(monkeypatch):
    # autouse fixture cleared RES_OPTIONS -> we set the widened DNS budget
    configure_network_resilience({})
    assert os.environ["RES_OPTIONS"] == "timeout:15 attempts:3"
    # an existing user value is left untouched
    monkeypatch.setenv("RES_OPTIONS", "custom")
    configure_network_resilience({"dns_timeout": "99"})
    assert os.environ["RES_OPTIONS"] == "custom"
