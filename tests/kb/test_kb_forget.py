"""`kb forget` must empty every tier a repo occupies, and only that repo's.

Before this command existed, contextlake could tell you a stored repo was wrong
("git can't find a repository here at all ... re-clone or remove it") and offer no
way to act on it. The only supported repair was deleting the whole store.

The trap these pin: connector output lives in separate `@connect:<repo>` /
`@enrich:<repo>` partitions, and `clear_repo`/`delete_repo` match the literal id
only. Removing just the literal partition leaves connector nodes, edges and vectors
answering queries under a repo id that no longer resolves.
"""

from __future__ import annotations

import types

import pytest

from contextlake.kb.cmds.forget import _partitions, _wiki_pages, cmd_forget
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="a.py", verified_at="2026-01-01T00:00:00Z")


def _seed(db, repo_id: str, *, n: int = 3) -> None:
    store = SqliteStore(db)
    check_schema(store)
    for part in _partitions(repo_id):
        store.upsert_repo(Repo(id=part, path=f"/tmp/{part}"))
        nodes = [Node(id=f"{part}:n{i}", repo=part, kind="function",
                      name=f"f{i}", file="a.py") for i in range(n)]
        store.upsert_nodes(part, nodes)
        store.upsert_edges(part, [Edge(src=f"{part}:n0", dst=f"{part}:n1",
                                       relation="calls",
                                       confidence=Confidence.EXTRACTED,
                                       provenance=_PROV)])
    store.close()


def _args(tmp_path, repo, *, dry_run=False):
    return types.SimpleNamespace(
        config=str(tmp_path / "kb.toml"), repo=repo, dry_run=dry_run,
        verbose=False, quiet=True, json=False,
    )


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    d = tmp_path / "kb"
    d.mkdir()
    (tmp_path / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{d.as_posix()}"\n\n[embeddings]\nenabled = false\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    return d


def test_partitions_covers_the_connector_shards():
    parts = _partitions("team/app")
    assert parts[0] == "team/app"
    # The whole point: the connector partitions are included, or their rows survive.
    assert "@connect:team/app" in parts
    assert "@enrich:team/app" in parts


def test_forget_removes_the_repo_and_its_connector_partitions(store_dir, capsys):
    db = store_dir / "index.sqlite"
    _seed(db, "team/app")
    _seed(db, "team/other")

    store = SqliteStore(db)
    assert store.repo_counts("team/app") == (3, 1)
    assert store.repo_counts("@connect:team/app") == (3, 1)
    store.close()

    assert cmd_forget(_args(store_dir.parent, "team/app")) == 0

    store = SqliteStore(db)
    try:
        for part in _partitions("team/app"):
            assert store.repo_counts(part) == (0, 0), f"{part} survived"
        assert store.get_repo("team/app") is None
        # A neighbour with an overlapping prefix must be untouched.
        assert store.repo_counts("team/other") == (3, 1)
        assert store.get_repo("team/other") is not None
    finally:
        store.close()


def test_dry_run_reports_but_removes_nothing(store_dir, caplog):
    db = store_dir / "index.sqlite"
    _seed(db, "team/app")
    with caplog.at_level("INFO"):
        assert cmd_forget(_args(store_dir.parent, "team/app", dry_run=True)) == 0
    store = SqliteStore(db)
    try:
        # The rows are the assertion that matters: a dry run that deletes is the
        # worst possible bug in a destructive command.
        assert store.repo_counts("team/app") == (3, 1), "dry run deleted rows"
        assert store.repo_counts("@connect:team/app") == (3, 1)
    finally:
        store.close()
    assert "dry run" in caplog.text.lower()


def test_an_unknown_repo_is_an_error_not_a_silent_success(store_dir):
    _seed(store_dir / "index.sqlite", "team/app")
    # Exit 1: "forgot nothing" reported as success would hide a typo'd id, and the
    # id is exactly the thing users get wrong here.
    assert cmd_forget(_args(store_dir.parent, "team/nope")) == 1


# --- wiki page selection ---------------------------------------------------

def test_wiki_pages_matches_the_repos_own_pages_only(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "_modules").mkdir(parents=True)
    (wiki / "team__app.md").write_text("whole repo")
    (wiki / "_modules" / "team__app__src.md").write_text("module")
    (wiki / "_modules" / "team__app__lib.md").write_text("module")
    # Must NOT be swept: a different repo whose sanitized name shares the prefix.
    (wiki / "team__appendix.md").write_text("other repo")
    (wiki / "_modules" / "team__appendix__src.md").write_text("other repo module")

    got = {p.name for p in _wiki_pages(wiki, "team/app")}
    assert got == {"team__app.md", "team__app__src.md", "team__app__lib.md"}


def test_wiki_pages_on_a_store_with_no_wiki_dir_is_empty(tmp_path):
    assert _wiki_pages(tmp_path / "nope", "team/app") == []


# --- the on-disk tier ------------------------------------------------------
#
# These were the gap. The suite above seeds rows only, so `forget` deleting rows
# while leaving `graph/<id>.json` and `history/<id>/` behind passed every test --
# and on a real store that was 173 MB retained by a command that had just reported
# the repo removed. The shard is the *large* half of a repo, and reclaiming it is
# the whole reason someone forgets a mis-indexed pseudo-repo.

def _seed_files(store_dir, repo_id: str, *, size: int = 4096) -> None:
    """Write the files an indexed repo owns: a shard per partition + history."""
    for part in _partitions(repo_id):
        p = store_dir / "graph" / f"{part}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * size)
    h = store_dir / "history" / repo_id
    h.mkdir(parents=True, exist_ok=True)
    (h / "deadbeef.json").write_text("x" * size)


def test_forget_reclaims_the_shard_and_history_files(store_dir):
    _seed(store_dir / "index.sqlite", "team/app")
    _seed_files(store_dir, "team/app")
    # A neighbour whose files must survive.
    _seed(store_dir / "index.sqlite", "team/other")
    _seed_files(store_dir, "team/other")

    assert cmd_forget(_args(store_dir.parent, "team/app")) == 0

    for part in _partitions("team/app"):
        assert not (store_dir / "graph" / f"{part}.json").exists(), f"{part} shard survived"
    assert not (store_dir / "history" / "team/app").exists(), "history survived"
    # Untouched neighbour: deleting by prefix would have taken these too.
    assert (store_dir / "graph" / "team/other.json").exists()
    assert (store_dir / "history" / "team/other" / "deadbeef.json").exists()


def test_dry_run_leaves_the_files_alone(store_dir):
    _seed(store_dir / "index.sqlite", "team/app")
    _seed_files(store_dir, "team/app")
    assert cmd_forget(_args(store_dir.parent, "team/app", dry_run=True)) == 0
    assert (store_dir / "graph" / "team/app.json").exists()
    assert (store_dir / "history" / "team/app" / "deadbeef.json").exists()


def test_forget_reports_the_space_it_reclaimed(store_dir, caplog):
    _seed(store_dir / "index.sqlite", "team/app")
    _seed_files(store_dir, "team/app", size=200_000)
    with caplog.at_level("INFO"):
        assert cmd_forget(_args(store_dir.parent, "team/app")) == 0
    # 4 files x 200 KB: reported in KB/MB, not as a raw byte count nobody reads.
    assert "on disk" in caplog.text
    assert "MB" in caplog.text or "KB" in caplog.text


def test_forget_works_on_a_store_that_has_no_files_yet(store_dir):
    """Rows without files: an interrupted index, or a store built by an older
    version. Must not raise on the missing graph/ and history/ directories."""
    _seed(store_dir / "index.sqlite", "team/app")
    assert cmd_forget(_args(store_dir.parent, "team/app")) == 0


def test_forget_compacts_the_index_and_leaves_it_usable(store_dir):
    """Deleting rows frees SQLite pages, not file space: the freed pages go on the
    freelist and the file stays at its high-water mark. Without the VACUUM, the largest
    file in the store does not move when a user forgets a repo to reclaim space."""
    import sqlite3

    db = store_dir / "index.sqlite"
    _seed(db, "team/app", n=2000)
    _seed(db, "team/other", n=5)

    assert cmd_forget(_args(store_dir.parent, "team/app")) == 0

    c = sqlite3.connect(db)
    try:
        free = c.execute("PRAGMA freelist_count").fetchone()[0]
        assert free == 0, f"index left {free} free pages; VACUUM did not run"
        # The neighbour must still be readable -- a VACUUM that corrupted the store
        # would be far worse than the space it reclaimed.
        assert c.execute(
            "SELECT COUNT(*) FROM nodes WHERE repo_id='team/other'").fetchone()[0] == 5
    finally:
        c.close()


def test_dry_run_does_not_compact(store_dir):
    import sqlite3

    db = store_dir / "index.sqlite"
    _seed(db, "team/app", n=500)
    # Create free pages so a stray VACUUM would be visible.
    c = sqlite3.connect(db)
    c.execute("DELETE FROM nodes WHERE repo_id='@connect:team/app'")
    c.commit()
    before = c.execute("PRAGMA freelist_count").fetchone()[0]
    c.close()

    assert cmd_forget(_args(store_dir.parent, "team/app", dry_run=True)) == 0

    c = sqlite3.connect(db)
    try:
        assert c.execute("PRAGMA freelist_count").fetchone()[0] == before, (
            "dry run compacted the index")
    finally:
        c.close()


# --- the shared/sentinel tier ----------------------------------------------
#
# The gap these pin. `(shared)`, `(packages)`, `(external)` and `(system)` hold the
# nodes no single repo owns, and `delete_repo`/`clear_repo` match a literal repo id,
# so nothing above them removes a single sentinel node. Measured on a real store,
# forgetting the only repo left 734 behind: 536 packages, 198 modules, 7 endpoints,
# 3 topics, all still listed and still searchable, describing imports and routes
# belonging to nothing.
#
# The fix has to be narrow. Deleting sentinel nodes per-repo is the *worse* bug and
# exactly the one the stable sentinel was introduced to prevent -- it would take the
# packages a surviving repo still imports. A sentinel node is garbage only once
# nothing references it, so the rule is reachability, not ownership.


def _seed_sharing(db, repo_id: str, shared: dict[str, str]) -> None:
    """Seed a repo whose code links to sentinel-owned nodes.

    ``shared`` maps a sentinel node id to the sentinel repo that owns it. The node
    is written with ``repo=<sentinel>`` while the *edge* to it is written under
    ``repo_id``: that split is the whole model. The store dedupes the node to one
    row (``ON CONFLICT(node_id)``), so seeding the same id from two repos leaves one
    node with two inbound edges, which is the shape that makes forgetting one of
    them a real question rather than a bookkeeping one.
    """
    store = SqliteStore(db)
    check_schema(store)
    store.upsert_repo(Repo(id=repo_id, path=f"/tmp/{repo_id}"))
    own = f"{repo_id}:file"
    store.upsert_nodes(repo_id, [Node(id=own, repo=repo_id, kind="file",
                                      name="a.py", file="a.py")])
    for node_id, owner in shared.items():
        store.upsert_nodes(repo_id, [Node(id=node_id, repo=owner, kind="package",
                                          name=node_id)])
        store.upsert_edges(repo_id, [Edge(src=own, dst=node_id, relation="imports",
                                          confidence=Confidence.EXTRACTED,
                                          provenance=_PROV)])
    # The setup assertion, not a redundant one: `upsert_nodes` takes a repo_id AND
    # the nodes carry their own `repo`. If the parameter won, these rows would sit
    # under `repo_id`, `clear_repo` would take them, and every test below would pass
    # green while testing nothing.
    for node_id, owner in shared.items():
        assert store.get_node(node_id).repo == owner
    store.close()


def test_forgetting_the_last_repo_leaves_no_orphaned_shared_nodes(store_dir):
    """Nothing else references them once the only repo is gone, so a store reporting
    itself empty must not still hold 536 packages and 198 modules."""
    db = store_dir / "index.sqlite"
    _seed_sharing(db, "team/app", {"pkg:npm:left-pad": "(packages)",
                                   "mod:py:os.path": "(shared)"})

    assert cmd_forget(_args(store_dir.parent, "team/app")) == 0

    store = SqliteStore(db)
    try:
        assert store.repo_counts("(packages)") == (0, 0), "orphaned package survived"
        assert store.repo_counts("(shared)") == (0, 0), "orphaned module survived"
        assert store.list_partitions() == []
    finally:
        store.close()


def test_forgetting_one_repo_keeps_shared_nodes_the_other_still_imports(store_dir):
    """The test that matters. Per-repo attribution for a shared node lives on its
    edges, never on the node's own `repo`, so a sweep that goes by ownership would
    delete a package `team/other` still imports -- reintroducing precisely the bug
    the stable sentinel was introduced to prevent. Both halves are asserted here on
    purpose: `shared-by-both` catches a sweep that is too wide, `only-app` catches a
    fix that never sweeps at all."""
    db = store_dir / "index.sqlite"
    _seed_sharing(db, "team/app", {"pkg:npm:shared-by-both": "(packages)",
                                   "pkg:npm:only-app": "(packages)"})
    _seed_sharing(db, "team/other", {"pkg:npm:shared-by-both": "(packages)"})

    store = SqliteStore(db)
    assert store.repo_counts("(packages)") == (2, 0)
    store.close()

    assert cmd_forget(_args(store_dir.parent, "team/app")) == 0

    store = SqliteStore(db)
    try:
        assert store.get_node("pkg:npm:shared-by-both") is not None, (
            "deleted a package the surviving repo still imports")
        assert store.get_node("pkg:npm:only-app") is None, (
            "kept a package nothing references any more")
        # The surviving repo's own edge to it has to be intact too: a node that
        # survives with no edges is unreachable, which is the same litter.
        assert len(store.neighbors("pkg:npm:shared-by-both")) == 1
    finally:
        store.close()


def test_dry_run_prunes_no_shared_nodes(store_dir):
    """The prune is store-wide, so a dry run that ran it would damage repos the user
    never named -- the worst failure mode this new path has."""
    db = store_dir / "index.sqlite"
    _seed_sharing(db, "team/app", {"pkg:npm:only-app": "(packages)"})

    assert cmd_forget(_args(store_dir.parent, "team/app", dry_run=True)) == 0

    store = SqliteStore(db)
    try:
        assert store.get_node("pkg:npm:only-app") is not None, "dry run pruned a node"
        assert store.repo_counts("(packages)") == (1, 0)
    finally:
        store.close()


def test_forget_reports_the_shared_nodes_it_pruned(store_dir, caplog):
    """Reported on its own line, never folded into the node count: that figure is
    what the repo owned, and a shared node never belonged to it."""
    db = store_dir / "index.sqlite"
    _seed_sharing(db, "team/app", {"pkg:npm:left-pad": "(packages)",
                                   "mod:py:os.path": "(shared)"})
    with caplog.at_level("INFO"):
        assert cmd_forget(_args(store_dir.parent, "team/app")) == 0
    assert "pruned 2 shared node(s)" in caplog.text
    # The repo itself owned exactly one node, its file; the two shared nodes are
    # counted apart from it rather than inflating what it is said to have held.
    assert "1 node(s)" in caplog.text
