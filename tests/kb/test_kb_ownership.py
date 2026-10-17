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



@pytest.fixture
def outer_repo(tmp_path):
    """A git repo with one commit by an invented author, and a plain directory inside it."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    _commit(outer, "a.py", 3, "Pat Invented", "pat@example.com", "2026-06-20 10:00:00 +0000")
    inner = outer / "inner"
    inner.mkdir()
    (inner / "b.py").write_text("x\n")
    return outer, inner


def test_a_plain_directory_inside_another_repo_is_not_ranked_by_that_repos_authors(outer_repo):
    """`git -C <dir> log` walks UP to the nearest repository, so an extracted archive, a
    subfolder of a monorepo checkout or the bundled sample fleet inside a source tree was
    ranked by the contributors of the repository around it, with commit counts, as if
    they had written the directory."""
    from contextlake.kb import ownership

    outer, inner = outer_repo
    ownership._CACHE.clear()
    # The control: git does see the outer repository's commit.
    assert [o.name for o in compute_owners(outer)] == ["Pat Invented"]

    owners = compute_owners(inner)
    assert owners == []
    assert "not the root of a git checkout" in owners.gap


def test_a_repo_nested_in_another_repo_ranks_only_its_own_authors(outer_repo):
    """The positive control: a directory with its own `.git` is its own checkout, even
    when it sits inside another one."""
    outer, inner = outer_repo
    nested = inner / "nested"
    nested.mkdir()
    _git(nested, "init", "-q")
    _commit(nested, "c.py", 3, "Quinn Invented", "quinn@example.com", "2026-06-21 10:00:00 +0000")

    owners = compute_owners(nested)
    assert [o.name for o in owners] == ["Quinn Invented"]
    assert owners.gap is None


def test_an_empty_ranking_says_why_git_could_not_be_asked(tmp_path, repo, outer_repo, monkeypatch):
    """Four reasons for `[]` were one value, and `who_knows` narrated all of them as "its
    git history was read but attributed no commits to anyone". A missing directory, a path
    that is not its own checkout and a timeout never read a history. Only the last case,
    where git ran and found nothing, leaves `gap` unset."""
    from contextlake.kb import ownership

    outer, inner = outer_repo
    _commit(repo, "a.py", 3, "Ada", "ada@x.io", "2026-06-20 10:00:00 +0000")
    ownership._CACHE.clear()

    assert "clone directory is not there" in compute_owners(tmp_path / "nope").gap
    assert "not the root of a git checkout" in compute_owners(inner).gap
    # git ran, the repo has history, and the pathspec matches nothing: a real empty ranking.
    empty = compute_owners(repo, "no/such/dir")
    assert empty == [] and empty.gap is None

    ownership._CACHE.clear()
    real = ownership.subprocess.run

    def timing_out(cmd, *a, **kw):
        if "log" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(ownership.subprocess, "run", timing_out)
    monkeypatch.setattr(ownership, "log", lambda *a, **kw: None)
    slow = compute_owners(repo)
    assert slow == []
    assert "within" in slow.gap and "timeout" in slow.gap
    # The gap is cached with the empty result, so a cache hit explains itself too.
    assert "timeout" in compute_owners(repo).gap


def _warnings(monkeypatch, fn) -> str:
    """Everything the call under test passed to `log`, as one string.

    Not `caplog`: `logging_setup` sets `propagate = False` on the `contextlake` logger,
    so caplog's root handler never sees these records and every assertion built on
    `caplog.text` passes vacuously. Not `capsys` either -- whether the message also
    reaches stdout depends on whether an earlier test in the file installed a stream
    handler, which makes the assertion depend on test ORDER rather than on behaviour.
    Both were tried here and both were wrong. Patching the seam the module itself calls
    tests the decision (which cause is named) and leaves the transport to the tests that
    own it.
    """
    said = []
    monkeypatch.setattr("contextlake.kb.ownership.log",
                        lambda msg, *a, **kw: said.append(str(msg)))
    fn()
    return "\n".join(said)


def test_a_non_repo_is_not_narrated_as_a_timeout(tmp_path, monkeypatch):
    """The three ways the walk can fail were one boolean, and the caller narrated all of
    them as the slowest one: "could not be read within 30s ... This is a timeout, not an
    empty history." A path that is not a git repository exits 128 in THREE MILLISECONDS.

    So the message asserted a cause nobody observed, in a sentence written specifically to
    stop a different misreading. It fired seven times in one site deploy, over a bundled
    sample fleet that is not a git repository at all -- and had it ever been a real
    timeout, it would have read identically.
    """
    assert "timeout" not in _warnings(monkeypatch, lambda: compute_owners(
        tmp_path / "not-a-clone")).lower(), (
        "a non-repository was reported as a timeout: the warning names a cause that was "
        "never measured")


def test_a_real_timeout_is_still_narrated_as_one(repo, monkeypatch):
    """The pair. Silencing the non-repo case must not silence the case the message was
    written for, which is the failure that actually costs a user 30 seconds."""
    from contextlake.kb import ownership

    _commit(repo, "a.py", 5, "Ada", "ada@x.io", "2026-06-20 10:00:00 +0000")
    ownership._CACHE.clear()
    real = ownership.subprocess.run

    def timing_out(cmd, *a, **kw):
        if "log" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(ownership.subprocess, "run", timing_out)
    said = _warnings(monkeypatch, lambda: ownership.compute_owners(repo))
    assert "timeout" in said.lower(), (
        "the one failure worth 30 seconds of a user's time went unreported")


def test_a_missing_git_is_named_and_is_not_a_timeout(repo, monkeypatch):
    """The third cause. docs/asking-the-graph.md says a missing `git` prints its own warning
    before `No commit history found`, so the warning has to exist and has to name git."""
    from contextlake.kb import ownership

    _commit(repo, "a.py", 5, "Ada", "ada@x.io", "2026-06-20 10:00:00 +0000")
    ownership._CACHE.clear()

    def no_git(cmd, *a, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(ownership.subprocess, "run", no_git)
    said = _warnings(monkeypatch, lambda: ownership.compute_owners(repo))
    assert "git could not be run" in said
    assert "timeout" not in said.lower()


def test_the_docs_describe_the_owners_walk_the_code_runs():
    """The note said `owners` runs exactly one `git log` and prints the same line for a
    missing git and a timeout. It runs a bounded walk then a full one, and two of the
    causes print their own warning first. The phrases are checked against the source too,
    so a reworded warning fails here until the page follows."""
    import inspect
    import re
    from pathlib import Path

    from contextlake.kb import ownership

    page = Path(__file__).resolve().parents[2] / "docs" / "asking-the-graph.md"
    text = re.sub(r"\s+", " ", re.sub(r"^> ?", "", page.read_text(encoding="utf-8"), flags=re.M))
    source = re.sub(r"\s+", " ", inspect.getsource(ownership.compute_owners))
    assert "runs exactly one `git log" not in text
    assert f'--since="{int(ownership._WALK_DAYS)} days ago"' in text
    for phrase in ("git could not be run for", "This is a timeout, not an empty history."):
        assert phrase in text, f"asking-the-graph.md does not mention: {phrase}"
        # The timeout message is two adjacent string literals; drop the join between them.
        assert phrase in source.replace('" "', ""), f"the code no longer says: {phrase}"


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
