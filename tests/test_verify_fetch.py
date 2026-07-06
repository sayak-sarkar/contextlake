"""Tests for verify (nested-repo detection), cache loading and fetch."""

import json
import os
import re
import urllib.error

import pytest

from conftest import FakeCompleted
from contextlake import core
from contextlake.core import (
    FetchError,
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


def test_fetch_no_token_and_no_glab_raises_and_writes_nothing(tmp_path, base_config, monkeypatch):
    # No token, and glab is absent -> an honest FetchError, and no cache is created
    # (an empty dict must never masquerade as a successful enumeration).
    def boom(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'glab'")

    monkeypatch.setattr(core.subprocess, "run", boom)
    cfg = {**base_config, "cache_dir": str(tmp_path), "cache_json": "p.json", "cache_file": "p.txt"}
    with pytest.raises(FetchError):
        fetch_gitlab_projects("g", cfg)
    assert not (tmp_path / "p.json").exists()
    assert not (tmp_path / "p.txt").exists()


def test_fetch_failure_preserves_existing_cache(tmp_path, base_config, fake_subprocess, no_sleep):
    # Regression: a transient failure mid-enumeration used to overwrite a good cache
    # with the partial result and report success. The old cache must survive intact.
    good = {"r0": {"full_path": "g/r0", "http": "h", "ssh": "s",
                   "archived": False, "default_branch": "main"}}
    (tmp_path / "p.json").write_text(json.dumps(good))
    (tmp_path / "p.txt").write_text("r0|s|h|main|False\n")

    page1 = [{"path_with_namespace": f"g/r{i}", "http_url_to_repo": f"h{i}",
              "ssh_url_to_repo": f"s{i}", "archived": False, "default_branch": "main"}
             for i in range(100)]

    def handler(cmd, **kwargs):
        endpoint = cmd[-1]
        if "&page=1" in endpoint:
            return FakeCompleted(stdout=json.dumps(page1))
        raise RuntimeError("boom: transient API failure")

    fake_subprocess.handler = handler
    cfg = base_config.copy()
    cfg.update(cache_dir=str(tmp_path), cache_json="p.json", cache_file="p.txt")

    with pytest.raises(FetchError):
        fetch_gitlab_projects("g", cfg)
    # both caches still hold the pre-failure content, byte for byte
    assert json.loads((tmp_path / "p.json").read_text()) == good
    assert (tmp_path / "p.txt").read_text() == "r0|s|h|main|False\n"


def _one_page_urlopen(routes, captured):
    """A fake urlopen serving canned JSON per URL-substring; [] for page>=2.

    The page param is parsed with an anchored regex — a bare substring check
    would match the "page=1..." inside "per_page=100" on every page and feed
    the (empty-page-terminated) pagination loop forever.
    """
    def fake(req, timeout=None):
        captured.append(req.full_url)
        m = re.search(r"[?&]page=(\d+)", req.full_url)
        if (int(m.group(1)) if m else 1) == 1:
            for needle, payload in routes.items():
                if needle in req.full_url:
                    return _FakeResp(payload)
        return _FakeResp([])
    return fake


def test_fetch_github_org_normalizes(tmp_path, base_config, monkeypatch):
    rows = [{"full_name": "acme/api", "clone_url": "https://x/acme/api.git",
             "ssh_url": "git@x:acme/api.git", "archived": False,
             "default_branch": "trunk", "created_at": "2026-01-01",
             "pushed_at": "2026-06-01"}]
    captured = []
    monkeypatch.setattr(core.urllib.request, "urlopen",
                        _one_page_urlopen({"/orgs/acme/repos": rows}, captured))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-x")
    cfg = {**base_config, "platform": "github", "cache_dir": str(tmp_path),
           "cache_json": "p.json", "cache_file": "p.txt"}
    result = fetch_gitlab_projects("acme", cfg)
    assert result["api"]["full_path"] == "acme/api"
    assert result["api"]["default_branch"] == "trunk"
    assert "api.github.com/orgs/acme/repos" in captured[0]
    assert "ghp-x" not in captured[0]  # token in header, never the URL


def test_fetch_gitea_falls_back_to_user_endpoint(tmp_path, base_config, monkeypatch):
    import urllib.error
    rows = [{"full_name": "ada/tool", "clone_url": "https://cb/ada/tool.git",
             "ssh_url": "", "archived": False, "default_branch": "main",
             "created_at": "2026-01-01", "updated_at": "2026-02-01"}]
    captured = []

    def fake(req, timeout=None):
        captured.append(req.full_url)
        if "/orgs/" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        if "page=1" in req.full_url:
            return _FakeResp(rows)
        return _FakeResp([])

    monkeypatch.setattr(core.urllib.request, "urlopen", fake)
    cfg = {**base_config, "platform": "codeberg", "cache_dir": str(tmp_path),
           "cache_json": "p.json", "cache_file": "p.txt"}
    result = fetch_gitlab_projects("ada", cfg)
    assert result["tool"]["full_path"] == "ada/tool"  # org 404 -> user endpoint
    assert captured[0].startswith("https://codeberg.org/api/v1/orgs/ada")
    assert "/users/ada/repos" in captured[1]


def test_fetch_bitbucket_workspace_normalizes(tmp_path, base_config, monkeypatch):
    payload = {"values": [{
        "full_name": "acme/billing",
        "links": {"clone": [{"name": "https", "href": "https://bb/acme/billing.git"},
                            {"name": "ssh", "href": "git@bb:acme/billing.git"}]},
        "mainbranch": {"name": "develop"},
        "created_on": "2026-01-01", "updated_on": "2026-03-01"}]}
    captured = []
    monkeypatch.setattr(core.urllib.request, "urlopen",
                        _one_page_urlopen({"/repositories/acme": payload}, captured))
    cfg = {**base_config, "platform": "bitbucket", "cache_dir": str(tmp_path),
           "cache_json": "p.json", "cache_file": "p.txt"}
    result = fetch_gitlab_projects("acme", cfg)
    assert result["billing"]["http"] == "https://bb/acme/billing.git"
    assert result["billing"]["default_branch"] == "develop"
    assert result["billing"]["archived"] is False


def test_unknown_platform_raises_cleanly(tmp_path, base_config):
    cfg = {**base_config, "platform": "sourceforge", "cache_dir": str(tmp_path),
           "cache_json": "p.json", "cache_file": "p.txt"}
    with pytest.raises(FetchError, match="unknown platform"):
        fetch_gitlab_projects("g", cfg)
    assert not (tmp_path / "p.json").exists()


def test_configure_network_resilience_sets_and_respects(monkeypatch):
    # autouse fixture cleared RES_OPTIONS -> we set the widened DNS budget
    configure_network_resilience({})
    assert os.environ["RES_OPTIONS"] == "timeout:15 attempts:3"
    # an existing user value is left untouched
    monkeypatch.setenv("RES_OPTIONS", "custom")
    configure_network_resilience({"dns_timeout": "99"})
    assert os.environ["RES_OPTIONS"] == "custom"


def test_match_repo_filter_glob_and_substring():
    from contextlake.core import _repo_filter_patterns, match_repo_filter
    pats = _repo_filter_patterns({"repo_filter": "billing/*, team/api ,frontend"})
    assert pats == ["billing/*", "team/api", "frontend"]
    assert match_repo_filter("acme/billing/core", "billing/core", pats)   # glob on local
    assert match_repo_filter("acme/team/api", "team/api", pats)           # exact substring
    assert match_repo_filter("acme/frontend/app", "frontend/app", pats)   # substring
    assert not match_repo_filter("acme/auth/svc", "auth/svc", pats)
    assert match_repo_filter("ACME/Billing/Core", "Billing/Core", pats)   # case-insensitive


def test_fetch_repo_filter_narrows_the_cache(tmp_path, base_config, fake_subprocess):
    page1 = [{"path_with_namespace": f"g/{n}", "http_url_to_repo": "h",
              "ssh_url_to_repo": "s", "archived": False, "default_branch": "main"}
             for n in ("api", "web", "billing-core", "billing-reports", "auth")]
    fake_subprocess.handler = lambda cmd, **k: (
        FakeCompleted(stdout=json.dumps(page1)) if "&page=1" in cmd[-1]
        else FakeCompleted(stdout="[]"))
    cfg = base_config.copy()
    cfg.update(cache_dir=str(tmp_path), cache_json="p.json", cache_file="p.txt",
               repo_filter="billing,api")
    result = fetch_gitlab_projects("g", cfg)
    # only the matching repos survive into the returned map + the cache
    assert set(result) == {"api", "billing-core", "billing-reports"}
    cached = json.loads((tmp_path / "p.json").read_text())
    assert set(cached) == {"api", "billing-core", "billing-reports"}
