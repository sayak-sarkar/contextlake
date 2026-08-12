"""The repo-id migration must never delete a repo this run was not going to index.

This is a regression test for observed data loss, not a hypothetical. A real store held
two repos under hand-made ids. A command was then run with `--workspace` pointing at a
different, unrelated directory. The migration walked **every** repo in the store, saw
that neither id matched the canonical id its checkout resolved to, and deleted both --
rows, nodes, edges, shard files and vectors -- on the reasoning stated in its own
docstring: "the caller's normal discovery+incremental-index loop does that immediately
after". That promise is only true for repos the run actually discovers. Nothing restored
them, and nothing failed, because from the migration's point of view the job was done.

The fix is ordering plus scope: discover first, then migrate only what was discovered.
"""

import subprocess

from contextlake.kb.model import Node, Repo
from contextlake.kb.repo_migrate import migrate_stale_repo_ids
from contextlake.kb.store.sqlite_store import SqliteStore

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
        "PATH": "/usr/bin:/bin"}


def _repo(path):
    """A real git repo with a remote, so the canonical id resolves to something that
    differs from the hand-made id below -- which is what makes the repo look stale."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    for cmd in (["init", "-q"], ["remote", "add", "origin",
                                 "https://example.invalid/team/thing.git"],
                ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True, env=_ENV,
                       capture_output=True)
    return path


def _store_with(tmp_path, repo_path, repo_id="hand-made-id"):
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id=repo_id, path=str(repo_path)))
    store.upsert_nodes(repo_id, [
        Node(id="n1", repo=repo_id, kind="function", name="f", file="a.py",
             line_start=1, line_end=2, lang="python")])
    return store


def test_a_repo_outside_this_runs_scope_is_left_alone(tmp_path):
    """THE LOAD-BEARING ASSERTION. Without the scope argument this deletes the repo and
    returns cleared=[(hand-made-id, ...)], and the caller -- indexing somewhere else
    entirely -- never puts it back."""
    kept = _repo(tmp_path / "elsewhere")
    store = _store_with(tmp_path, kept)

    # This run will index a different directory, so `kept` is not in scope.
    result = migrate_stale_repo_ids(store, tmp_path, in_scope=[str(tmp_path / "other-ws")])

    assert result.cleared == []
    assert result.skipped_out_of_scope == ["hand-made-id"]
    assert store.get_repo("hand-made-id") is not None
    assert store.repo_counts("hand-made-id")[0] == 1, "its nodes must still be there"


def test_a_repo_this_run_will_index_is_still_migrated(tmp_path):
    """The near-miss, and the reason the fix is scope rather than simply not deleting.
    The migration must keep working for the case it was written for: same store, same
    stale id, but now the run IS going to index that checkout, so deleting it is safe
    because discovery will re-create it under the canonical id."""
    target = _repo(tmp_path / "in-scope")
    store = _store_with(tmp_path, target)

    result = migrate_stale_repo_ids(store, tmp_path, in_scope=[str(target)])

    assert [old for old, _new in result.cleared] == ["hand-made-id"]
    assert store.get_repo("hand-made-id") is None


def test_a_symlinked_or_unnormalised_spelling_of_the_same_path_still_matches(tmp_path):
    """Scope is compared on resolved paths. If it were compared on raw strings, a run
    that discovered `/ws/./repo` while the store recorded `/ws/repo` would treat the repo
    as out of scope, silently skipping a migration that should happen -- the mirror image
    of the bug, and just as quiet."""
    target = _repo(tmp_path / "in-scope")
    store = _store_with(tmp_path, target)

    odd = str(tmp_path / "in-scope" / "." )
    result = migrate_stale_repo_ids(store, tmp_path, in_scope=[odd])

    assert [old for old, _new in result.cleared] == ["hand-made-id"]


def test_pairs_passed_by_mistake_do_not_raise_out_of_the_migration(tmp_path):
    """`discover_repos` returns (repo_id, path) pairs, and passing the pairs raised a
    TypeError straight out of the migration, failing the whole index run. Unresolvable
    entries must simply fail to match, which leaves repos alone -- the safe direction."""
    kept = _repo(tmp_path / "elsewhere")
    store = _store_with(tmp_path, kept)

    result = migrate_stale_repo_ids(store, tmp_path,
                                    in_scope=[("some-id", str(kept))])  # the wrong shape

    assert result.cleared == []
    assert store.get_repo("hand-made-id") is not None


def test_an_out_of_scope_skip_does_not_mark_the_store_clean(tmp_path):
    """The trap inside the fix. A process-lifetime cache remembers stores with nothing
    left to migrate, so `index --watch` does not re-resolve every repo each tick. If an
    out-of-scope skip counted as clean, a later run that DID include that repo would skip
    the migration entirely -- a fix planting a new silent bug."""
    from contextlake.kb import repo_migrate

    target = _repo(tmp_path / "in-scope")
    store = _store_with(tmp_path, target)
    repo_migrate._clean_store_dirs.discard(str(tmp_path))

    migrate_stale_repo_ids(store, tmp_path, in_scope=[str(tmp_path / "nowhere")])
    assert str(tmp_path) not in repo_migrate._clean_store_dirs

    # ...and the deferred migration still happens once the repo is in scope.
    result = migrate_stale_repo_ids(store, tmp_path, in_scope=[str(target)])
    assert [old for old, _new in result.cleared] == ["hand-made-id"]
