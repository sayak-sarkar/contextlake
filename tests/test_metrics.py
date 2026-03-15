"""Tests for the post-sync repo audit (emptiness classification + age metrics)."""

import json
import subprocess
from datetime import datetime, timezone

from contextlake.metrics import (
    _parse_dt,
    classify,
    report_repo_metrics,
    scan_repo_metrics,
    summarize,
)


def _cp(cmd, code=0, out=""):
    return subprocess.CompletedProcess(cmd, code, out, "")


def _fake_git(facts_by_rel):
    """A subprocess.run stand-in answering git queries from canned per-repo facts."""
    def run(cmd, timeout=15):
        path = cmd[2].replace("\\", "/")
        rel = next((r for r in facts_by_rel if path.endswith(r)), None)
        f = facts_by_rel.get(rel, {})
        sub = cmd[3:]
        if sub[:2] == ["rev-parse", "HEAD"]:
            return _cp(cmd, 0 if f.get("head") else 1, "sha\n" if f.get("head") else "")
        if sub[:1] == ["ls-files"]:
            return _cp(cmd, 0, "\n".join(f.get("files", [])))
        if "--max-parents=0" in cmd:
            return _cp(cmd, 0, f.get("root", "") or "")
        if sub[:2] == ["log", "-1"]:
            return _cp(cmd, 0, f.get("last", "") or "")
        return _cp(cmd, 0, "")
    return run


def test_classify():
    assert classify([], has_head=True) == "empty"
    assert classify(["x"], has_head=False) == "empty"
    assert classify(["README.md"], has_head=True) == "readme-only"
    assert classify(["README.md", "LICENSE", ".gitignore"], has_head=True) == "boilerplate"
    assert classify(["README.md", "src/app.py"], has_head=True) == "content"
    assert classify(["docs/guide.md"], has_head=True) == "content"  # non-meta md is content


def test_parse_dt_is_tolerant():
    assert _parse_dt(None) is None
    assert _parse_dt("2026-06-22T14:30:00+05:30").year == 2026
    assert _parse_dt("2015-03-01T10:00:00.123Z").year == 2015  # 'Z' + fractional seconds
    assert _parse_dt("not-a-date") is None


def test_scan_classifies_and_falls_back_to_git_dates(tmp_path):
    for rel in ("emptyrepo", "tmpl", "app"):
        (tmp_path / rel / ".git").mkdir(parents=True)
    facts = {
        "emptyrepo": {"head": False},
        "tmpl": {"head": True, "files": ["README.md"],
                 "last": "2024-01-02T00:00:00+00:00", "root": "2020-01-01T00:00:00+00:00"},
        "app": {"head": True, "files": ["README.md", "src/app.py"],
                "last": "2026-06-01T00:00:00+00:00", "root": "2019-05-05T00:00:00+00:00"},
    }
    metrics = scan_repo_metrics(str(tmp_path), {}, max_workers=2, run=_fake_git(facts))
    by = {m["repo"]: m for m in metrics}
    assert by["emptyrepo"]["classification"] == "empty"
    assert by["tmpl"]["classification"] == "readme-only"
    assert by["app"]["classification"] == "content"
    # no GitLab cache -> created falls back to the git root-commit date
    assert by["app"]["created"] == "2019-05-05T00:00:00+00:00"
    assert by["app"]["created_source"] == "git"
    assert by["app"]["last_commit"] == "2026-06-01T00:00:00+00:00"


def test_scan_prefers_gitlab_dates(tmp_path):
    (tmp_path / "r" / ".git").mkdir(parents=True)
    facts = {"r": {"head": True, "files": ["a.py"],
                   "last": "2026-01-01T00:00:00+00:00", "root": "2018-01-01T00:00:00+00:00"}}
    projects = {"r": {"full_path": "g/r", "created_at": "2015-03-01T10:00:00.000Z",
                      "last_activity_at": "2026-06-01T00:00:00Z", "archived": True}}
    m = scan_repo_metrics(str(tmp_path), projects, run=_fake_git(facts))[0]
    assert m["created"] == "2015-03-01T10:00:00.000Z" and m["created_source"] == "gitlab"
    assert m["last_activity"] == "2026-06-01T00:00:00Z"
    assert m["archived"] is True


def test_summarize_counts_and_staleness():
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    metrics = [
        {"classification": "content", "archived": False, "created": "2018-01-01T00:00:00+00:00",
         "last_commit": "2026-06-01T00:00:00+00:00"},
        {"classification": "readme-only", "archived": False, "created": "2024-01-01T00:00:00+00:00",
         "last_commit": "2024-01-01T00:00:00+00:00"},   # >1y stale
        {"classification": "empty", "archived": True, "created": None, "last_commit": None},
    ]
    s = summarize(metrics, now=now)
    assert s["total"] == 3 and s["by_class"]["empty"] == 1 and s["archived"] == 1
    assert s["oldest_created"] == "2018-01-01" and s["newest_created"] == "2024-01-01"
    assert s["stale_over_1y"] == 1 and s["no_commits"] == 1


def test_report_writes_json_and_csv(tmp_path):
    metrics = [{"repo": "a", "full_path": "g/a", "classification": "empty", "tracked_files": 0,
                "created": None, "created_source": "unknown", "last_commit": None,
                "last_activity": None, "default_branch": None, "archived": False}]
    out = tmp_path / "audit.json"
    s = report_repo_metrics(metrics, report_path=out)
    assert s["total"] == 1
    assert out.exists() and (tmp_path / "audit.csv").exists()
    d = json.loads(out.read_text())
    assert d["summary"]["total"] == 1 and d["repos"][0]["repo"] == "a"
    assert (tmp_path / "audit.csv").read_text().startswith("repo,classification,tracked_files")


def test_scan_with_real_git(tmp_path):
    """End-to-end against a real git repo, to confirm the actual git commands work."""
    repo = tmp_path / "real"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    import os
    runenv = {**os.environ, **env}

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env=runenv)

    git("init", "-q")
    (repo / "README.md").write_text("# real\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    metrics = scan_repo_metrics(str(tmp_path), {}, max_workers=1)
    m = metrics[0]
    assert m["repo"] == "real" and m["classification"] == "readme-only"
    assert m["last_commit"] and m["created_source"] == "git"
