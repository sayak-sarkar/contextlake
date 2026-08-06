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
