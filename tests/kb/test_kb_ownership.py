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
        main(["kb", "owners", str(repo)])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "Alice" in out and "Owners" in out


def test_cmd_owners_usage_error_without_target():
    with pytest.raises(SystemExit) as e:
        main(["kb", "owners"])
    assert e.value.code == 2


def test_non_utf8_author_name_does_not_abort(repo, commit_raw_bytes):
    """`git log --format=%an` replays the author ident's bytes as stored, so a
    contributor whose tooling wrote cp1252 puts an undecodable byte in the middle
    of the log. Strict decoding raised out of `subprocess.run` itself, before any
    of this module's error handling could run."""
    (repo / "a.py").write_text("x\n")
    _git(repo, "add", "a.py")
    # commit_raw_bytes, never GIT_AUTHOR_NAME -- see the fixture's docstring: git
    # transcodes the ident too, so the ordinary spelling proves nothing.
    commit_raw_bytes(repo, message=b"add a\n",
                     author=b"Ren\x96e Doe <renee@example.com>")

    owners = compute_owners(repo)
    assert [o.email for o in owners] == ["renee@example.com"]
    assert owners[0].name.startswith("Ren")


# --- the log walk is bounded and cached (E12) --------------------------------

def test_owners_are_cached_on_head_so_a_second_call_does_no_git_work(repo, monkeypatch):
    """The dashboard, `kb owners` and the MCP `who_knows` tool each called this, and each
    paid a full `git log --numstat` walk -- 30 of the 41 seconds a repo-detail request
    took on a 36,290-commit repository.

    Asserted by counting subprocess calls, not by timing: a timing assertion is flaky and
    would not have caught the bug this test exists for. The first version of the cache
    stored its entry under a variable the aggregation loop had already rebound to a
    contributor email, so it wrote a garbage key and never hit -- while every existing
    test still passed.
    """
    from contextlake.kb import ownership

    _commit(repo, "a.py", 5, "Ada", "ada@x.io", "2026-06-20 10:00:00 +0000")
    _commit(repo, "b.py", 3, "Ada", "ada@x.io", "2026-06-21 10:00:00 +0000")
    ownership._CACHE.clear()

    calls = []
    real = ownership.subprocess.run

    def counting(cmd, *a, **kw):
        calls.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(ownership.subprocess, "run", counting)

    first = ownership.compute_owners(repo)
    n_after_first = len(calls)
    second = ownership.compute_owners(repo)

    assert first == second, "a cache hit must return the same answer"
    log_walks = [c for c in calls[n_after_first:] if "log" in c]
    assert not log_walks, (
        f"second call re-walked the log: {log_walks}. The cache is keyed on HEAD; if this "
        f"fails the key is wrong, not the concept.")


def test_the_log_walk_is_bounded_but_falls_back_for_a_dormant_repo(repo, monkeypatch):
    """The bound is what fixes the FIRST call; the fallback is what stops it lying.

    A repository whose newest commit predates the window returns nothing from the bounded
    walk, and answering "no owners" for a dormant-but-real repo would be a worse bug than
    the slowness the bound removes.
    """
    from contextlake.kb import ownership

    _commit(repo, "a.py", 5, "Ada", "ada@x.io", "2026-06-20 10:00:00 +0000")
    _commit(repo, "b.py", 3, "Ada", "ada@x.io", "2026-06-21 10:00:00 +0000")
    ownership._CACHE.clear()
    seen = []
    real = ownership.subprocess.run

    def spy(cmd, *a, **kw):
        seen.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(ownership.subprocess, "run", spy)
    owners = ownership.compute_owners(repo)

    walks = [c for c in seen if "log" in c]
    assert walks, "expected at least one log walk"
    assert any(any(str(x).startswith("--since=") for x in c) for c in walks), (
        "the first walk must be bounded")
    assert owners, "a repo with history must yield owners"

    # THE POINT OF THIS TEST. The fixture's commits are recent, so the bounded walk must
    # FIND them and no unbounded retry may happen. Passing a float to `--since` makes git
    # return zero commits while exiting 0, which made the bound useless and cost a second
    # full walk -- and left every other assertion here still passing.
    assert len(walks) == 1, (
        f"fresh history must be found by the bounded walk alone, got {len(walks)} walks: "
        f"{walks}. If the second is unbounded, the --since argument is not parsing.")


def test_a_timeout_is_not_retried_unbounded_and_is_not_re_paid_next_request(repo, monkeypatch):
    """The 7.1.0 bound left a hole exactly where the walk is slowest, and this pins both
    halves of the repair.

    Measured on real clones before the fix: 0.4s, 2.1s, and **60.1s**. The 60s case was two
    30-second timeouts back to back, because the old code wrote
    `rows = _walk(bounded) or _walk(None)` -- and `or` cannot tell "succeeded, found
    nothing" from "timed out". So a repository that had just proved it could not finish the
    CHEAP walk was immediately asked to do the EXPENSIVE one. Worse, the empty result
    returned before the cache write, so the dashboard re-paid the whole thing on every
    single repo-detail request.

    Two load-bearing assertions: exactly ONE walk is attempted, and the second call does no
    git work at all.
    """
    from contextlake.kb import ownership

    _commit(repo, "a.py", 5, "Ada", "ada@x.io", "2026-06-20 10:00:00 +0000")
    ownership._CACHE.clear()
    calls = []
    real = ownership.subprocess.run

    def timing_out(cmd, *a, **kw):
        calls.append(cmd)
        if "log" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30))
        return real(cmd, *a, **kw)      # rev-parse still works, so the cache has a key

    monkeypatch.setattr(ownership.subprocess, "run", timing_out)
    assert ownership.compute_owners(repo) == []

    walks = [c for c in calls if "log" in c]
    assert len(walks) == 1, (
        f"a timed-out bounded walk must NOT be followed by an unbounded one, got "
        f"{len(walks)}: {walks}")

    # ...and the failure is remembered for this commit, so the cost is paid once.
    before = len(calls)
    assert ownership.compute_owners(repo) == []
    assert not [c for c in calls[before:] if "log" in c], (
        "the second call re-ran a log walk: an uncached timeout is re-paid on every "
        "request, which is the bug this test exists for")


def test_a_genuinely_empty_bounded_walk_still_falls_back(repo, monkeypatch):
    """The near-miss that stops the fix above from becoming a different bug. Suppressing
    the fallback on TIMEOUT must not suppress it on a real empty result, or a dormant
    repository whose history predates the window silently reports no owners."""
    from contextlake.kb import ownership

    _commit(repo, "a.py", 5, "Ada", "ada@x.io", "2026-06-20 10:00:00 +0000")
    ownership._CACHE.clear()
    walks = []
    real = ownership.subprocess.run

    def empty_when_bounded(cmd, *a, **kw):
        if "log" in cmd:
            walks.append(cmd)
            if any(str(x).startswith("--since=") for x in cmd):
                # Succeeds, finds nothing: exactly a repo dormant longer than the window.
                return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(ownership.subprocess, "run", empty_when_bounded)
    owners = ownership.compute_owners(repo)

    assert len(walks) == 2, "an honest empty must still be retried unbounded"
    assert owners, "the unbounded walk must recover the dormant repo's owners"
