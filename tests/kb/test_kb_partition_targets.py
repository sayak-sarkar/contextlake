"""Which partitions `embed` works on, versus which repos `connect`/`enrich` do.

The defect these pin: `_connect_targets` is named for connect/enrich, which scrape a
working tree, and its `[... for r in store.list_repos() if r.path]` filter is right for
them -- no clone, nothing to scrape. `embed` was bolted onto the same helper and
inherited the filter, which excluded every partition twice over: `@connect:<repo>`,
`@enrich:<repo>`, `@wiki:<repo>` and `@ingest:<name>` have no `repos` row *and* no path.

So connector, enrichment and ingested content was never embedded. The search side had
already been fixed -- `embeddings.store._repo_scope` expands a `repo=` filter to the
connector partitions at query time -- which made this hard to see: the scoping was
correct and matched nothing, because the write side never put vectors there.
"""

from __future__ import annotations

import types

import pytest

from contextlake.kb.cmds._common import _connect_targets, _content_targets
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "index.sqlite")
    check_schema(s)
    yield s
    s.close()


def _nodes(store, repo_id: str, n: int = 2) -> None:
    store.upsert_nodes(repo_id, [
        Node(id=f"{repo_id}:n{i}", repo=repo_id, kind="function", name=f"f{i}", file="a.py")
        for i in range(n)])


def _args(**kw):
    base = dict(workspace=None, source=None, repo=None, args=[])
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def seeded(store, tmp_path):
    """One real repo, its connector partition, a standalone ingest, and a sentinel."""
    store.upsert_repo(Repo(id="acme/app", path=str(tmp_path / "app")))
    for rid in ("acme/app", "@connect:acme/app", "@ingest:handbook", "(shared)"):
        _nodes(store, rid)
    return store


def test_list_partitions_sees_what_list_repos_cannot(seeded):
    # The root cause in one assertion: partitions own nodes and have no `repos` row.
    assert [r.id for r in seeded.list_repos()] == ["acme/app"]
    assert set(seeded.list_partitions()) == {
        "acme/app", "@connect:acme/app", "@ingest:handbook", "(shared)"}


def test_embed_targets_include_the_partitions(seeded):
    got = _content_targets(_args(), seeded)
    assert "@connect:acme/app" in got, "connector content still not embedded"
    assert "@ingest:handbook" in got, "ingested content still not embedded"
    assert "acme/app" in got


def test_embed_targets_exclude_sentinels(seeded):
    # (shared)/(packages) own nodes, so list_partitions truthfully reports them -- but
    # they are cross-repo aggregates, not content anyone asked to index.
    assert "(shared)" not in _content_targets(_args(), seeded)


def test_connect_targets_are_unchanged(seeded):
    # connect/enrich must NOT gain partitions: with no clone there is nothing to scrape.
    assert [t[0] for t in _connect_targets(_args(), seeded)] == ["acme/app"]


def test_a_repo_with_rows_but_no_nodes_is_still_offered(store, tmp_path):
    """`embed` reads shards, not node rows, so a repo whose rows are absent must stay in
    the work set. Enumerating from `nodes` alone would have silently narrowed it."""
    store.upsert_repo(Repo(id="acme/rowsonly", path=str(tmp_path / "ro")))
    assert _content_targets(_args(), store) == ["acme/rowsonly"]


def test_scoping_to_a_repo_takes_its_partitions_and_not_a_strangers(seeded):
    got = _content_targets(_args(args=["acme/app"]), seeded)
    assert set(got) == {"acme/app", "@connect:acme/app"}, (
        "scoping must follow the repo's own connector content, and only that repo's")


def test_an_explicit_source_is_not_widened(seeded, tmp_path):
    """--source names exactly one thing; it must not pull in unrelated partitions that
    happen to share the store."""
    got = _content_targets(_args(source=str(tmp_path), repo="just/this"), seeded)
    assert got == ["just/this"]
