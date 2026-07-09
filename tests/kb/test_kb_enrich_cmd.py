"""End-to-end test for the `enrich` command: config -> query terms built from an
indexed repo's own codebase -> stubbed source search -> results persisted in the
isolated `@enrich:<repo>` partition."""

from argparse import Namespace
from datetime import date

import contextlake.kb.connectors.enrich as enrich
from contextlake.cli import build_parser
from contextlake.kb.commands import cmd_enrich
from contextlake.kb.connectors.enrich import enrich_partition
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.sources.base import Document
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, read_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = "group/app"

_CONFIG = """
[kb]
store_dir = "{store}"

[[sources]]
type = "mcp"
name = "wiki-search"
mcp = "http://localhost:9999/mcp"
tool = "search"
"""

_NO_SOURCE_CONFIG = """
[kb]
store_dir = "{store}"

[[sources]]
type = "figma"
name = "design"
"""


def _prov():
    return Provenance(source_file="app/main.py", verified_at=date.today())


def _seed_indexed_repo(store, store_dir, repo_id, repo_path):
    """An indexed repo: a shard with an embeddable symbol (so build_terms finds
    something) plus the store's repo record (so `_connect_targets` lists it)."""
    order_service = Node(id="n1", repo=repo_id, kind="class", name="OrderService",
                          file="app/order.py")
    charge_fn = Node(id="n2", repo=repo_id, kind="function", name="chargeCard",
                      file="app/billing.py")
    nodes = [order_service, charge_fn]
    edges = [
        Edge(src="n1", dst="n2", relation="calls", confidence=Confidence.EXTRACTED,
             provenance=_prov()),
    ]
    write_shard(store_dir, GraphShard(repo=repo_id, head_commit="abc123",
                                       nodes=nodes, edges=edges))
    store.upsert_repo(Repo(id=repo_id, path=repo_path))


def test_enrich_persists_documents_from_configured_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG.format(store=store_dir.as_posix()))

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    _seed_indexed_repo(store, store_dir, REPO, str(tmp_path / "app"))
    store.close()

    docs = [
        Document(id="d1", title="Runbook", text="how to page", uri="https://x/1"),
        Document(id="d2", title="Design doc", text="architecture notes", uri="https://x/2"),
    ]
    monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)

    args = Namespace(config=str(cfg), workspace=None, args=[REPO])
    assert cmd_enrich(args) == 0

    part = enrich_partition(REPO)
    shard = read_shard(store_dir, part)
    assert shard is not None
    assert len(shard.nodes) == 2
    for node in shard.nodes:
        assert node.kind == "document"


def test_enrich_no_term_searchable_sources_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_NO_SOURCE_CONFIG.format(store=store_dir.as_posix()))

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.close()

    args = Namespace(config=str(cfg), workspace=None, args=[])
    assert cmd_enrich(args) == 0


def test_parser_registers_enrich_positional_repo():
    args = build_parser().parse_args(["enrich", "x/y"])
    assert args.command == "enrich"
    assert args.args == ["x/y"]


def test_enrich_positional_repo_filters_to_that_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG.format(store=store_dir.as_posix()))

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    _seed_indexed_repo(store, store_dir, REPO, str(tmp_path / "app"))
    _seed_indexed_repo(store, store_dir, "group/other", str(tmp_path / "other"))
    store.close()

    docs = [Document(id="d1", title="Runbook", text="how to page", uri="https://x/1")]
    monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)

    args = Namespace(config=str(cfg), workspace=None, args=[REPO])
    assert cmd_enrich(args) == 0

    assert read_shard(store_dir, enrich_partition(REPO)) is not None
    assert read_shard(store_dir, enrich_partition("group/other")) is None
