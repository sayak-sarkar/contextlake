"""Tests for bi-temporal shard history and the query --as-of path."""

import logging
from argparse import Namespace

import pytest

from gitlab_sync.kb.commands import _query_as_of
from gitlab_sync.kb.model import Node
from gitlab_sync.kb.store.shards import (
    GraphShard,
    archive_shard,
    list_indexed_commits,
    read_shard_at,
)


@pytest.fixture
def logs():
    logger = logging.getLogger("gitlab_sync")
    saved = logger.handlers[:]
    logger.handlers.clear()
    messages: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield messages
    logger.handlers[:] = saved


def _shard(repo, head, names):
    return GraphShard(
        repo=repo, head_commit=head,
        nodes=[Node(id=f"{head}:{n}", repo=repo, kind="function", name=n) for n in names],
        edges=[],
    )


# --- history primitives ----------------------------------------------------

def test_archive_and_read_shard_at(tmp_path):
    archive_shard(tmp_path, _shard("r", "c1", ["foo"]))
    archive_shard(tmp_path, _shard("r", "c2", ["foo", "bar"]))

    s1 = read_shard_at(tmp_path, "r", "c1")
    s2 = read_shard_at(tmp_path, "r", "c2")
    assert {n.name for n in s1.nodes} == {"foo"}
    assert {n.name for n in s2.nodes} == {"foo", "bar"}
    assert read_shard_at(tmp_path, "r", "missing") is None
    assert list_indexed_commits(tmp_path, "r") == ["c1", "c2"]


def test_archive_skips_commitless_shard(tmp_path):
    assert archive_shard(tmp_path, _shard("r", None, ["x"])) is None


# --- query --as-of (time travel) ------------------------------------------

def _cfg(tmp_path):
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    (tmp_path / "kb.toml").write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')
    return store_dir, str(tmp_path / "kb.toml")


def test_query_as_of_returns_historical_snapshot(tmp_path, logs):
    store_dir, cfg = _cfg(tmp_path)
    archive_shard(store_dir, _shard("r", "old", ["legacy_handler"]))
    archive_shard(store_dir, _shard("r", "new", ["modern_handler"]))

    # "handler" as of the OLD commit sees the legacy symbol, not the modern one
    assert _query_as_of(Namespace(config=cfg, repo="r", kind=None, limit=None,
                                  args=["handler"]), "old") == 0
    text = "\n".join(logs)
    assert "legacy_handler" in text and "modern_handler" not in text

    logs.clear()
    # the same query as of NEW shows the modern symbol instead
    assert _query_as_of(Namespace(config=cfg, repo="r", kind=None, limit=None,
                                  args=["handler"]), "new") == 0
    text = "\n".join(logs)
    assert "modern_handler" in text and "legacy_handler" not in text


def test_query_as_of_requires_repo(tmp_path):
    _, cfg = _cfg(tmp_path)
    args = Namespace(config=cfg, repo=None, kind=None, limit=None, args=["x"])
    assert _query_as_of(args, "c1") == 2  # usage error


def test_query_as_of_unknown_commit(tmp_path):
    store_dir, cfg = _cfg(tmp_path)
    archive_shard(store_dir, _shard("r", "c1", ["foo"]))
    args = Namespace(config=cfg, repo="r", kind=None, limit=None, args=["foo"])
    assert _query_as_of(args, "nope") == 1  # no such snapshot
