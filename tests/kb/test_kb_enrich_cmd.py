"""End-to-end test for the `enrich` command: config -> query terms built from an
indexed repo's own codebase -> stubbed source search -> results persisted in the
isolated `@enrich:<repo>` partition."""

import re
from argparse import Namespace
from datetime import date

import contextlake.kb.connectors.enrich as enrich
from contextlake.cli import _resolve_command, build_parser
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

# Same source, embeddings off. The default is on, and building an embedder pulls a
# model over the network -- irrelevant to what the accounting tests measure.
_CONFIG_NO_EMBED = _CONFIG + """
[embeddings]
enabled = false
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
    forecast_service = Node(id="n1", repo=repo_id, kind="class", name="ForecastService",
                          file="app/forecast.py")
    reading_fn = Node(id="n2", repo=repo_id, kind="function", name="readSensor",
                      file="app/readings.py")
    nodes = [forecast_service, reading_fn]
    edges = [
        Edge(src="n1", dst="n2", relation="calls", confidence=Confidence.EXTRACTED,
             provenance=_prov()),
    ]
    write_shard(store_dir, GraphShard(repo=repo_id, head_commit="abc123",
                                       nodes=nodes, edges=edges))
    store.upsert_repo(Repo(id=repo_id, path=repo_path))


def test_enrich_persists_documents_from_configured_source(tmp_path, monkeypatch, gls_logs):
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

    # glyph-prefixed summary (H4): counts/text unchanged, just wrapped
    assert "✓ Enrich complete: 2 document(s) stored" in gls_logs.text


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
    parser = build_parser()
    args = parser.parse_args(["kb", "enrich", "x/y"])
    _resolve_command(args, parser)
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


# --- accounting: every targeted repo lands in exactly one bucket ----------------

_BUCKET_LINE = re.compile(
    r"(\d+) repo\(s\) planned: (\d+) enriched, (\d+) nothing returned, "
    r"(\d+) returned but unattached, (\d+) failed, (\d+) skipped")


def _five_state_fleet(store, store_dir, tmp_path):
    """One repo for each of the five buckets, seeded so the state is produced by the
    real code path rather than asserted into place.

    Every id here is synthetic. The repo whose enrichment fails is not seeded
    differently: the caller makes `write_shard` raise for that partition, which is
    what a full disk or an unwritable store directory does.
    """
    for name in ("attached", "unattached", "empty", "broken"):
        _seed_indexed_repo(store, store_dir, f"group/{name}", str(tmp_path / name))
    # Indexed symbols, so the matcher has real candidates to match or miss. Only
    # `group/skipped` is left without a shard, which is what `build_terms` returns
    # nothing for.
    for name in ("attached", "unattached", "empty", "broken"):
        store.upsert_nodes(f"group/{name}", [
            Node(id=f"{name}-n2", repo=f"group/{name}", kind="function",
                 name="readSensor", file="app/readings.py"),
        ])
    store.upsert_repo(Repo(id="group/skipped", path=str(tmp_path / "skipped")))


_DOCS_BY_REPO_NAME = {
    # names a symbol -> edges to code -> enriched
    "attached": [Document(id="d1", title="Runbook", text="readSensor retries twice",
                          uri="https://x/1")],
    # discusses the repo in prose, names no symbol -> returned, unattached
    "unattached": [Document(id="d2", title="Q3 plan", text="the team owns this service",
                            uri="https://x/2")],
    # the sources had nothing -> nothing returned
    "empty": [],
    "broken": [Document(id="d3", title="Runbook", text="readSensor retries twice",
                        uri="https://x/3")],
}


def test_enrich_buckets_sum_to_the_planned_total(tmp_path, monkeypatch, gls_logs):
    """`kb wiki` had to grow a `suppressed` counter after its four numbers quietly
    added up to less than the run planned, and six missing pages read the same as a
    repo that had none. The planned total here is the target-list length, taken
    before the loop from a different variable than the accumulators, so this
    assertion can actually fail."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG_NO_EMBED.format(store=store_dir.as_posix()))

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    _five_state_fleet(store, store_dir, tmp_path)
    store.close()

    monkeypatch.setattr(
        enrich, "search_source",
        lambda src, terms, timeout=None: _DOCS_BY_REPO_NAME.get(terms[0], []))

    real_write_shard = enrich.write_shard

    def _write_shard(store_dir, shard):
        if shard.repo == enrich_partition("group/broken"):
            raise OSError("no space left on device")
        return real_write_shard(store_dir, shard)

    monkeypatch.setattr(enrich, "write_shard", _write_shard)

    args = Namespace(config=str(cfg), workspace=None, args=[])
    cmd_enrich(args)

    m = _BUCKET_LINE.search(gls_logs.text)
    assert m, f"no bucket line in the output: {gls_logs.text}"
    planned, *buckets = (int(g) for g in m.groups())
    assert planned == 5, "the planned total must be the target-list length, not a sum"
    assert sum(buckets) == planned, (
        f"{planned} repo(s) planned but only {sum(buckets)} accounted for: {gls_logs.text}")
    # enriched, nothing returned, returned but unattached, failed, skipped
    assert buckets == [1, 1, 1, 1, 1]


def test_enrich_reports_returned_but_unattached_as_a_state_not_a_failure(
        tmp_path, monkeypatch, gls_logs):
    """A document that discusses the repo in prose without naming a symbol of three
    or more characters correctly attaches to nothing. Calling that a failure would
    report false failures on correct runs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG_NO_EMBED.format(store=store_dir.as_posix()))

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    _seed_indexed_repo(store, store_dir, REPO, str(tmp_path / "app"))
    store.upsert_nodes(REPO, [
        Node(id="n2", repo=REPO, kind="function", name="readSensor", file="app/readings.py"),
    ])
    store.close()

    docs = [Document(id="d1", title="Q3 plan", text="the team owns this service",
                     uri="https://x/1")]
    monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)

    args = Namespace(config=str(cfg), workspace=None, args=[REPO])
    assert cmd_enrich(args) == 0

    text = gls_logs.text
    assert "0 edges to code (returned, unattached)" in text
    assert "1 returned but unattached, 0 failed" in text
    # The ✓ and the word "complete" are the assertion that this is not graded a
    # failure: a repo counted as failed prints ⚠ and "incomplete".
    assert "✓ Enrich complete: 1 document(s) stored, 0 edge(s) to code" in text


def test_enrich_repo_whose_shard_write_fails_lands_in_the_failed_bucket(
        tmp_path, monkeypatch, gls_logs):
    """`search_source` is contractually non-raising, so a source failure never
    reaches the loop. The failures that do are the store and shard writes, and that
    is the path this drives: an OSError out of `write_shard`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG_NO_EMBED.format(store=store_dir.as_posix()))

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    _seed_indexed_repo(store, store_dir, REPO, str(tmp_path / "app"))
    store.close()

    docs = [Document(id="d1", title="Runbook", text="how to page", uri="https://x/1")]
    monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)

    def _boom(store_dir, shard):
        raise OSError("no space left on device")

    monkeypatch.setattr(enrich, "write_shard", _boom)

    args = Namespace(config=str(cfg), workspace=None, args=[REPO])
    assert cmd_enrich(args) == 1

    text = gls_logs.text
    assert "enrichment failed (no space left on device)" in text
    assert "1 repo(s) planned: 0 enriched, 0 nothing returned, 0 returned but " \
           "unattached, 1 failed, 0 skipped" in text
