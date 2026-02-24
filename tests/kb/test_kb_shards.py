"""Tests for per-repo graph shards + reindex."""

from datetime import date

from contextlake.kb.model import Confidence, Edge, Node, Provenance
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
            Node(id="a", repo="team/api", kind="function", name="OrderService"),
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
    assert store.get_node("a").name == "OrderService"
    assert {e.dst for e in store.neighbors("a", direction="out")} == {"b"}
    assert store.stats().nodes == 2 and store.stats().edges == 1
    # re-running is idempotent (clear + reload), not duplicating
    reindex_shard(store, tmp_path, "team/api")
    assert store.stats().edges == 1
    store.close()


def test_reindex_absent_shard_returns_false(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    assert reindex_shard(store, tmp_path, "missing/repo") is False
    store.close()
