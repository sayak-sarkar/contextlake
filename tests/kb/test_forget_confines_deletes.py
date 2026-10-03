"""`kb forget` must delete only files that belong to the repo id it was given.

The repo id comes from a clone's remote URL, and a remote URL is whatever the clone's owner
wrote. `normalize_remote_url` keeps `..` segments, and a `file:///path` remote gives an
absolute id. `forget` joined the history directory from that raw id
(`store_dir / "history" / repo_id`) and passed it to `shutil.rmtree`. `shard_path` was
meant to be the one check, but it only guards the shard: it appends `.json` to the LAST
segment, so `h/../..` resolves to `graph/...json` and passes, while the history path built
from the same id resolves to the store root.

Every test here keeps its store at `tmp_path/a/b/kb`, so an escape lands in a directory
the test made. Nothing here may point at a path outside `tmp_path`.
"""

from __future__ import annotations

import shutil
import types

import pytest

from contextlake.kb.cmds.forget import _disk_artifacts, _partitions, _wiki_pages, cmd_forget
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A store at `tmp_path/a/b/kb` with a sentinel one level above it, and an unrelated
    repo (`other`) that owns a shard and a history snapshot."""
    monkeypatch.setenv("HOME", str(tmp_path))
    outer = tmp_path / "a" / "b"
    store_dir = outer / "kb"
    store_dir.mkdir(parents=True)
    (outer / "precious.txt").write_text("precious")
    (store_dir / "graph").mkdir()
    (store_dir / "graph" / "other.json").write_text("{}")
    (store_dir / "history" / "other").mkdir(parents=True)
    (store_dir / "history" / "other" / "c1.json").write_text("{}")
    # A real host directory, as an indexed repo on that host would leave.
    (store_dir / "history" / "evil.example").mkdir()
    (store_dir / "history" / "x").mkdir()
    # `history/team/../other` only resolves to `history/other` if `history/team` exists.
    (store_dir / "history" / "team").mkdir()
    (tmp_path / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n\n[embeddings]\nenabled = false\n')
    return types.SimpleNamespace(tmp=tmp_path, outer=outer, store=store_dir)


def _index(store_dir, repo_id: str) -> None:
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id=repo_id, path="/tmp/x"))
    store.upsert_nodes(repo_id, [Node(id=f"{repo_id}:n", repo=repo_id, kind="function",
                                      name="f", file="a.py")])
    store.close()


def _args(tmp_path, repo, *, dry_run=False):
    return types.SimpleNamespace(
        config=str(tmp_path / "kb.toml"), repo=repo, dry_run=dry_run,
        verbose=False, quiet=True, json=False,
    )


# Each id would, before the fix, put a path OUTSIDE the repo's own history directory on
# the delete list. The comment names where it pointed.
HOSTILE_IDS = [
    "../..",                       # history/../.. : the directory above the store
    "evil.example/../..",          # the store root itself (`history/evil.example` exists)
    "x/..",                        # history/ itself: every repo's history
    "team/../other",               # history/other and graph/other.json: ANOTHER repo's files
    "evil.example/../../..",       # three levels up, again outside the store
]


@pytest.mark.parametrize("repo_id", HOSTILE_IDS)
def test_no_delete_target_is_taken_from_an_id_that_escapes(world, repo_id):
    """THE LOAD-BEARING ASSERTION, at the point where the delete list is built.

    The fixture holds no file of its own for any of these ids, so an honest answer is an
    empty list. A non-empty one names something this id does not own.
    """
    found = _disk_artifacts(world.store, _partitions(repo_id), repo_id)
    assert found == [], (
        f"{repo_id!r} put {[str(p) for p in found]} on the delete list; "
        f"resolved: {[str(p.resolve()) for p in found]}")


def test_an_absolute_id_cannot_name_a_directory_elsewhere(world):
    """pathlib drops everything left of an absolute segment, so `history / "/abs/dir"` IS
    `/abs/dir`. A `file:///abs/dir` remote gives such an id. The directory here is inside
    tmp_path, so a regression deletes only a test directory."""
    victim = world.tmp / "victim_dir"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    found = _disk_artifacts(world.store, _partitions(str(victim)), str(victim))
    assert found == [], f"an absolute id put {[str(p) for p in found]} on the delete list"


def test_a_refused_target_is_named_and_not_counted(world, gls_logs):
    """Refusing silently would read as a repo with no files. The warning names the id."""
    _index(world.store, "../..")
    with gls_logs.at_level("INFO"):
        assert cmd_forget(_args(world.tmp, "../..", dry_run=True)) == 0
    out = gls_logs.text
    assert "on disk  0 file(s)/dir(s)" in out, out
    assert "'../..'" in out and "not removing" in out, out


def test_forget_with_an_escaping_id_leaves_everything_outside_alone(world):
    """End to end, not a dry run. The escape target is `tmp_path/a/b`, a directory the
    fixture made, so a regression removes test files only."""
    _index(world.store, "../..")
    cmd_forget(_args(world.tmp, "../..", dry_run=False))
    assert (world.outer / "precious.txt").read_text() == "precious"
    assert (world.store / "index.sqlite").exists()
    assert (world.store / "history" / "other" / "c1.json").exists()
    # The rows of the id itself are still removed: it is the files that are refused.
    store = SqliteStore(world.store / "index.sqlite")
    try:
        assert store.get_repo("../..") is None
    finally:
        store.close()


def test_forget_with_an_alias_id_keeps_the_other_repos_files(world):
    """`team/../other` is the shard `graph/other.json` and the history `history/other`.
    Both are repo `other`'s. Forgetting the alias must not take them."""
    _index(world.store, "team/../other")
    cmd_forget(_args(world.tmp, "team/../other", dry_run=False))
    assert (world.store / "graph" / "other.json").exists()
    assert (world.store / "history" / "other" / "c1.json").exists()


# --- positive controls: the guard must not refuse what it owns ------------------------

def test_a_normal_repo_still_loses_its_shard_and_history(world):
    _index(world.store, "team/app")
    (world.store / "graph" / "team").mkdir()
    (world.store / "graph" / "team" / "app.json").write_text("{}")
    (world.store / "history" / "team" / "app").mkdir(parents=True)
    (world.store / "history" / "team" / "app" / "c1.json").write_text("{}")

    found = _disk_artifacts(world.store, _partitions("team/app"), "team/app")
    assert {p.name for p in found} == {"app.json", "app"}

    assert cmd_forget(_args(world.tmp, "team/app")) == 0
    assert not (world.store / "history" / "team" / "app").exists()
    assert not (world.store / "graph" / "team" / "app.json").exists()
    assert (world.store / "history" / "other" / "c1.json").exists()


def test_a_symlinked_history_root_is_still_honoured(world):
    """A user may keep `history/` on another disk. Both sides are resolved, so the repo's
    directory is inside the (resolved) root and is removed."""
    real = world.tmp / "elsewhere_history"
    (real / "team" / "app").mkdir(parents=True)
    (real / "team" / "app" / "c1.json").write_text("{}")
    shutil.rmtree(world.store / "history")
    (world.store / "history").symlink_to(real, target_is_directory=True)

    found = _disk_artifacts(world.store, _partitions("team/app"), "team/app")
    assert [p.name for p in found] == ["app"]


def test_a_history_dir_that_is_a_symlink_out_of_the_store_is_not_listed(world):
    """Not a hostile id: a link planted under `history/`. It resolves outside the store, so
    it is not this repo's directory, and its size must not be counted as space to reclaim."""
    outside = world.tmp / "outside_dir"
    outside.mkdir()
    (outside / "big.bin").write_text("x" * 100)
    (world.store / "history" / "team" / "app").parent.mkdir(parents=True, exist_ok=True)
    try:
        (world.store / "history" / "team" / "app").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")

    assert _disk_artifacts(world.store, _partitions("team/app"), "team/app") == []


# --- the wiki-page glob takes the raw id too -------------------------------------------

def test_a_glob_character_in_an_id_does_not_sweep_other_repos_wiki_pages(tmp_path):
    """`_wiki_pages` globs `<id>__*.md` in `_modules/`. An id containing `*` made the glob
    match the pages of every other repo on that host. They are regenerable, but they are
    not this repo's."""
    modules = tmp_path / "wiki" / "_modules"
    modules.mkdir(parents=True)
    (modules / "host__other__mod.md").write_text("other")
    (modules / "host__*__mod.md").write_text("own")
    got = {p.name for p in _wiki_pages(tmp_path / "wiki", "host/*")}
    assert got == {"host__*__mod.md"}, got


# --- the new helper -------------------------------------------------------------------

def test_strictly_within_refuses_the_base_itself(tmp_path):
    from contextlake.kb.paths import strictly_within

    base = tmp_path / "history"
    (base / "a").mkdir(parents=True)
    assert strictly_within(base, base / "a")
    assert not strictly_within(base, base)            # `within` says True here
    assert not strictly_within(base, base / "a" / "..")
    assert not strictly_within(base, tmp_path)
    assert not strictly_within(base, base / "a" / ".." / ".." / "history_backup")


def test_strictly_within_does_not_raise_on_a_symlink_loop(tmp_path):
    """On Python 3.10 to 3.12 `resolve()` raises RuntimeError for a loop; 3.13 stopped. The
    answer differs by version, so this pins only that the call returns."""
    from contextlake.kb.paths import strictly_within

    base = tmp_path / "history"
    base.mkdir()
    (base / "loop").symlink_to(base / "loop")
    assert strictly_within(base, base / "loop") in (True, False)
