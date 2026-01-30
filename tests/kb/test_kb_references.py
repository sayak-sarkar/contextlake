"""Tests for repo-side association-signal extraction."""

import subprocess

import pytest

from gitlab_sync.kb.references import extract_issue_keys, scrape_links


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "ABC-45 initial commit")
    _git(tmp_path, "branch", "ABC-123-add-feature")
    return tmp_path


def test_extract_issue_keys_from_branches_and_commits(repo):
    keys = extract_issue_keys(str(repo), r"[A-Z]+-\d+")
    assert set(keys) == {"ABC-45", "ABC-123"}


def test_extract_issue_keys_bad_pattern_is_safe(repo):
    assert extract_issue_keys(str(repo), r"[unclosed") == []


def test_extract_issue_keys_on_non_repo(tmp_path):
    assert extract_issue_keys(str(tmp_path), r"[A-Z]+-\d+") == []


def test_scrape_links_finds_configured_urls(tmp_path):
    (tmp_path / "README.md").write_text(
        "See https://acme.atlassian.net/wiki/spaces/X/pages/123 and "
        "the design at https://www.figma.com/file/abc/Spec\n"
        "Ignore https://example.com/other\n"
    )
    (tmp_path / "code.py").write_text("# https://acme.atlassian.net/wiki/should-be-ignored\n")
    links = scrape_links(str(tmp_path), [
        r"https://[\w.-]+\.atlassian\.net/\S+",
        r"https://www\.figma\.com/\S+",
    ])
    assert "https://acme.atlassian.net/wiki/spaces/X/pages/123" in links
    assert any("figma.com/file/abc" in u for u in links)
    assert not any("example.com" in u for u in links)  # not matched
    assert not any("should-be-ignored" in u for u in links)  # .py not scanned


def test_scrape_links_no_patterns(tmp_path):
    (tmp_path / "README.md").write_text("https://x.atlassian.net/y")
    assert scrape_links(str(tmp_path), []) == []
