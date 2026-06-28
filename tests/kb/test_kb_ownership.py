"""Ownership / SME derivation from git commit history (`owners` verb + who_knows)."""

import os
import subprocess

import pytest

from contextlake.cli import main
from contextlake.kb.ownership import compute_owners


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=env)


def _commit(repo, fname, lines, name, email, date):
    p = repo / fname
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n" * lines)
    _git(repo, "add", fname)
    env = {**os.environ,
           "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
           "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    _git(repo, "commit", "-q", "-m", f"touch {fname}", env=env)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    return r


def test_recency_outranks_equal_volume(repo):
    # Equal volume; Bob's work is recent, Alice's is months old -> Bob ranks first.
    for i in range(3):
        _commit(repo, f"old{i}.py", 5, "Alice", "alice@x.io", "2026-01-05 10:00:00 +0000")
    for i in range(3):
        _commit(repo, f"new{i}.py", 5, "Bob", "bob@x.io", "2026-06-20 10:00:00 +0000")

    owners = compute_owners(repo)
    assert [o.name for o in owners] == ["Bob", "Alice"]
    assert owners[1].last_active == "2026-01-05"
    assert owners[0].commits == 3
    assert abs(sum(o.share for o in owners) - 1.0) < 1e-6   # shares normalise to 1


def test_subpath_filters_to_that_tree(repo):
    _commit(repo, "src/a.py", 4, "Alice", "alice@x.io", "2026-02-01 10:00:00 +0000")
    _commit(repo, "docs/b.md", 4, "Bob", "bob@x.io", "2026-03-01 10:00:00 +0000")
    assert [o.name for o in compute_owners(repo, "docs")] == ["Bob"]
    assert [o.name for o in compute_owners(repo, "src")] == ["Alice"]


def test_empty_for_non_repo(tmp_path):
    assert compute_owners(tmp_path / "nope") == []


def test_cmd_owners_cli_lists_contributors(repo, capsys):
    _commit(repo, "a.py", 3, "Alice", "alice@x.io", "2026-05-01 10:00:00 +0000")
    with pytest.raises(SystemExit) as e:
        main(["owners", str(repo)])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "Alice" in out and "Owners" in out


def test_cmd_owners_usage_error_without_target():
    with pytest.raises(SystemExit) as e:
        main(["owners"])
    assert e.value.code == 2
