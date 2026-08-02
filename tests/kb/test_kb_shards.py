"""Tests for per-repo graph shards + reindex."""

from datetime import date

from contextlake.kb.model import SHARED_REPO, Confidence, Edge, Node, Provenance
from contextlake.kb.store.shards import (
    GraphShard,
    read_shard,
    reindex_shard,
    shard_path,
    write_shard,
)
from contextlake.kb.store.sqlite_store import SqliteStore


def _shard():
    prov = Provenance(source_file="a.py", source_line=3, verified_at=date(2026, 6, 21))
    return GraphShard(
        repo="team/api",
        head_commit="deadbeef",
        nodes=[
            Node(id="a", repo="team/api", kind="function", name="CatalogService"),
            Node(id="b", repo="team/api", kind="function", name="charge"),
        ],
        edges=[Edge(src="a", dst="b", relation="calls", confidence=Confidence.EXTRACTED,
                    provenance=prov)],
    )


def test_write_then_read_is_lossless(tmp_path):
    s = _shard()
    write_shard(tmp_path, s)
    assert read_shard(tmp_path, "team/api") == s


def test_shard_path_nests_namespace(tmp_path):
    p = shard_path(tmp_path, "team/sub/api")
    assert p.parts[-3:] == ("team", "sub", "api.json")


def test_read_missing_returns_none(tmp_path):
    assert read_shard(tmp_path, "nope/x") is None


def test_reindex_matches_direct_upsert(tmp_path):
    s = _shard()
    write_shard(tmp_path, s)
    store = SqliteStore(tmp_path / "kb.sqlite")
    assert reindex_shard(store, tmp_path, "team/api") is True
    assert store.get_node("a").name == "CatalogService"
    assert {e.dst for e in store.neighbors("a", direction="out")} == {"b"}
    assert store.stats().nodes == 2 and store.stats().edges == 1
    # re-running is idempotent (clear + reload), not duplicating
    reindex_shard(store, tmp_path, "team/api")
    assert store.stats().edges == 1
    store.close()


def test_reindex_of_two_repos_shares_a_module_node_via_the_sentinel(tmp_path):
    """The actual production path (Finding #10): two repos each shard their own
    "import requests" as a module node with the exact same id and repo=SHARED_REPO;
    reindex_shard runs upsert_nodes(repo_id, shard.nodes) per repo, exactly like
    a real `contextlake index` run would. The shared node must read back with the
    sentinel (not either repo's id) and survive either repo's reindex/clear_repo."""
    prov = Provenance(source_file="x.py", source_line=1, verified_at=date(2026, 6, 21))
    shared = Node(id="module_requests", repo=SHARED_REPO, kind="module", name="requests")

    api = GraphShard(
        repo="team/api", head_commit="a1",
        nodes=[Node(id="api_file", repo="team/api", kind="file", name="svc.py"), shared],
        edges=[Edge(src="api_file", dst="module_requests", relation="imports",
                    confidence=Confidence.EXTRACTED, provenance=prov)],
    )
    web = GraphShard(
        repo="team/web", head_commit="w1",
        nodes=[Node(id="web_file", repo="team/web", kind="file", name="client.py"), shared],
        edges=[Edge(src="web_file", dst="module_requests", relation="imports",
                    confidence=Confidence.EXTRACTED, provenance=prov)],
    )
    write_shard(tmp_path, api)
    write_shard(tmp_path, web)

    store = SqliteStore(tmp_path / "kb.sqlite")
    reindex_shard(store, tmp_path, "team/api")
    reindex_shard(store, tmp_path, "team/web")
    assert store.get_node("module_requests").repo == SHARED_REPO

    # reindexing (or clearing) team/api alone must not delete the node team/web
    # still has a live "imports" edge to.
    reindex_shard(store, tmp_path, "team/api")
    assert store.get_node("module_requests") is not None
    assert store.get_node("web_file") is not None
    assert {e.src for e in store.neighbors("module_requests", direction="in")} \
        == {"api_file", "web_file"}
    store.close()


def test_reindex_absent_shard_returns_false(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    assert reindex_shard(store, tmp_path, "missing/repo") is False
    store.close()


def test_shard_path_rejects_traversal(tmp_path):
    """repo_id can arrive from an untrusted caller (an MCP tool arg), so a value that
    would escape the graph/ dir must be refused rather than resolved to an outside file."""
    import pytest

    from contextlake.kb.store.shards import shard_path

    # legitimate nested namespace ids still work
    assert shard_path(tmp_path, "team/api").name == "api.json"
    for bad in ("../../etc/passwd", "team/../../outside", "/etc/passwd"):
        with pytest.raises(ValueError):
            shard_path(tmp_path, bad)


def test_read_shard_returns_none_for_traversal(tmp_path):
    # a traversal attempt reads nothing and degrades to "no such shard"
    outside = tmp_path / "secret.json"
    outside.write_text('{"repo": "x"}', encoding="utf-8")
    assert read_shard(tmp_path / "store", "../secret") is None


# --- in-memory shard cache (perf regression: dashboard repo-detail slowness) -

def test_read_shard_does_not_reparse_an_unchanged_file(tmp_path, monkeypatch):
    """A second read_shard() for the same, unchanged file must reuse the cached
    parse rather than re-running GraphShard.model_validate_json -- the exact
    per-request cost identified as the dashboard repo-detail slowdown's root
    cause (JSON parse + pydantic validation, re-run from scratch on every
    request with no caching of any kind)."""
    write_shard(tmp_path, _shard())
    calls = []
    real_parse = GraphShard.model_validate_json  # bound classmethod, captured pre-patch

    def _tracked(*a, **kw):
        calls.append(1)
        return real_parse(*a, **kw)

    monkeypatch.setattr(
        GraphShard, "model_validate_json",
        classmethod(lambda cls, *a, **kw: _tracked(*a, **kw)),
    )

    first = read_shard(tmp_path, "team/api")
    second = read_shard(tmp_path, "team/api")

    assert len(calls) == 1          # parsed once, not twice
    assert second is first          # same cached object returned
    assert second == _shard()       # and it's still correct


def test_read_shard_picks_up_a_same_process_rewrite(tmp_path):
    """write_shard() must invalidate its own cache entry immediately -- a
    caller that writes then reads back in the same process (e.g. reindex)
    must never see a stale cached parse, regardless of filesystem mtime
    resolution."""
    s = _shard()
    write_shard(tmp_path, s)
    assert read_shard(tmp_path, "team/api").nodes[0].name == "CatalogService"

    changed = s.model_copy(deep=True)
    changed.nodes[0].name = "RenamedService"
    write_shard(tmp_path, changed)

    assert read_shard(tmp_path, "team/api").nodes[0].name == "RenamedService"


def test_read_shard_picks_up_an_external_rewrite(tmp_path):
    """A shard rewritten by a *different* process (e.g. a `contextlake index`
    run while `dashboard --serve` stays up) must still be picked up on the
    next read -- the cache is validated against the file's own (mtime_ns,
    size) on every call, not just invalidated by this process's own writes."""
    import os

    from contextlake.kb.store.shards import shard_path

    write_shard(tmp_path, _shard())
    assert read_shard(tmp_path, "team/api").nodes[0].name == "CatalogService"

    p = shard_path(tmp_path, "team/api")
    new_content = _shard().model_copy(deep=True)
    new_content.nodes[0].name = "RenamedService"
    p.write_text(new_content.model_dump_json(indent=2), encoding="utf-8")
    # force a distinct (mtime, size) signal so this can't pass by timestamp
    # luck alone, regardless of filesystem mtime resolution
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    assert read_shard(tmp_path, "team/api").nodes[0].name == "RenamedService"
