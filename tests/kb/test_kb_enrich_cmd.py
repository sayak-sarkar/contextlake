"""End-to-end test for the `enrich` command: config -> query terms built from an
indexed repo's own codebase -> stubbed source search -> results persisted in the
isolated `@enrich:<repo>` partition."""

import re
from argparse import Namespace
from datetime import date

import pytest

import contextlake.kb.connectors.enrich as enrich
from contextlake.cli import _resolve_command, build_parser, main
from contextlake.kb import embeddings
from contextlake.kb.commands import cmd_enrich
from contextlake.kb.connectors.enrich import enrich_partition
from contextlake.kb.embeddings.store import build_vector_store
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
    monkeypatch.setattr(enrich, "search_source", lambda src, terms, **_kw: docs)

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
    monkeypatch.setattr(enrich, "search_source", lambda src, terms, **_kw: docs)

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
        lambda src, terms, **_kw: _DOCS_BY_REPO_NAME.get(terms[0], []))

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
    monkeypatch.setattr(enrich, "search_source", lambda src, terms, **_kw: docs)

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
    monkeypatch.setattr(enrich, "search_source", lambda src, terms, **_kw: docs)

    def _boom(store_dir, shard):
        raise OSError("no space left on device")

    monkeypatch.setattr(enrich, "write_shard", _boom)

    args = Namespace(config=str(cfg), workspace=None, args=[REPO])
    assert cmd_enrich(args) == 1

    text = gls_logs.text
    assert "enrichment failed (no space left on device)" in text
    assert "1 repo(s) planned: 0 enriched, 0 nothing returned, 0 returned but " \
           "unattached, 1 failed, 0 skipped" in text


def _seed_enrich_store(tmp_path, monkeypatch, repo_ids=(REPO,), config=None):
    """Indexed repos in a fresh store with one term-searchable source, and a source search
    that records each call. Returns ``(config path, store dir, calls)``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text((config or _CONFIG_NO_EMBED).format(store=store_dir.as_posix()))
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    for repo_id in repo_ids:
        _seed_indexed_repo(store, store_dir, repo_id, str(tmp_path / repo_id.replace("/", "-")))
    store.close()
    calls = []
    monkeypatch.setattr(enrich, "search_source",
                        lambda src, terms, **_kw: calls.append(terms) or [])
    return cfg, store_dir, calls


def test_enrich_names_a_misspelt_repo_id_and_exits_nonzero(tmp_path, monkeypatch, gls_logs):
    """A misspelt id filtered the target list to nothing, and `kb enrich` read that as an
    empty store: "No repos to enrich (index some first ...)", exit 0. A scheduled run
    reported success having enriched nothing, and the message named the wrong fix."""
    cfg, store_dir, calls = _seed_enrich_store(tmp_path, monkeypatch)
    args = Namespace(config=str(cfg), workspace=None, args=["group/ap"])
    assert cmd_enrich(args) == 1
    assert "Unknown repo 'group/ap'" in gls_logs.text
    assert "Did you mean: group/app" in gls_logs.text
    assert "index some first" not in gls_logs.text
    assert calls == []


def test_enrich_refuses_the_whole_run_when_one_of_two_ids_is_misspelt(
        tmp_path, monkeypatch, gls_logs):
    cfg, store_dir, calls = _seed_enrich_store(tmp_path, monkeypatch)
    args = Namespace(config=str(cfg), workspace=None, args=[REPO, "group/ap"])
    assert cmd_enrich(args) == 1
    assert "Unknown repo 'group/ap'" in gls_logs.text
    assert calls == []
    assert read_shard(store_dir, enrich_partition(REPO)) is None


def test_enrich_dry_run_also_names_a_misspelt_repo_id(tmp_path, monkeypatch, gls_logs):
    cfg, store_dir, calls = _seed_enrich_store(tmp_path, monkeypatch)
    args = Namespace(config=str(cfg), workspace=None, args=["group/ap"], dry_run=True)
    assert cmd_enrich(args) == 1
    assert "Dry run" not in gls_logs.text


def test_enrich_on_an_empty_store_keeps_the_index_some_first_message(
        tmp_path, monkeypatch, gls_logs):
    """The hint stays for the case it is true of: nothing is indexed and no id was named."""
    cfg, store_dir, calls = _seed_enrich_store(tmp_path, monkeypatch, repo_ids=())
    args = Namespace(config=str(cfg), workspace=None, args=[])
    assert cmd_enrich(args) == 0
    assert "index some first" in gls_logs.text
    assert "Unknown repo" not in gls_logs.text


def test_enrich_ignores_positional_ids_for_a_workspace(tmp_path, monkeypatch, gls_logs):
    """`--workspace` never reads the positionals, so an id that is not in the store is not an
    error there."""
    cfg, store_dir, calls = _seed_enrich_store(tmp_path, monkeypatch)
    empty = tmp_path / "empty-workspace"
    empty.mkdir()
    args = Namespace(config=str(cfg), workspace=str(empty), args=["group/ap"])
    assert cmd_enrich(args) == 0
    assert "Unknown repo" not in gls_logs.text


def test_enrich_names_a_misspelt_repo_id_when_no_source_can_search(
        tmp_path, monkeypatch, gls_logs):
    """The check sat below the "No term-searchable sources" return, so a misspelt id on a
    config with nothing to search exited 0 and the docs promised exit 1."""
    cfg, store_dir, calls = _seed_enrich_store(tmp_path, monkeypatch, config=_NO_SOURCE_CONFIG)
    assert cmd_enrich(Namespace(config=str(cfg), workspace=None, args=["group/ap"])) == 1
    assert "Unknown repo 'group/ap'" in gls_logs.text
    assert "No term-searchable sources" not in gls_logs.text


def test_enrich_with_no_source_and_no_id_still_exits_zero(tmp_path, monkeypatch, gls_logs):
    """The control: the early return is unchanged when no id is named."""
    cfg, store_dir, calls = _seed_enrich_store(tmp_path, monkeypatch, config=_NO_SOURCE_CONFIG)
    assert cmd_enrich(Namespace(config=str(cfg), workspace=None, args=[REPO])) == 0
    assert "No term-searchable sources" in gls_logs.text


# --- a changed embedder is refused before enrich clears a partition's vectors -----

_CONFIG_FAKE_EMBED = _CONFIG + """
[embeddings]
enabled = true
provider = "ollama"
"""


class _ModelEmbedder:
    """Two instances differ only in `identity` and `dim`, as two models do."""

    def __init__(self, identity, dim=2, *, flaky_first=False, down=False):
        self.identity = self.name = identity
        self.dim = dim
        self._flaky_first = flaky_first
        self._down = down
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self._down:
            raise ConnectionError("embedder is down")
        if self._flaky_first and self.calls == 1:
            raise ConnectionError("first call timed out")
        return [[1.0] + [float(i)] * (self.dim - 1) for i, _ in enumerate(texts)]


def _vectors_of(store_dir, part):
    vs = build_vector_store(store_dir / "embeddings.sqlite")
    try:
        return vs.count_repo(part)
    finally:
        vs.close()


@pytest.mark.parametrize("dim_b", [2, 3], ids=["same-dimension", "new-dimension"])
def test_enrich_refuses_a_changed_embedder_before_it_clears_any_vector(
        tmp_path, monkeypatch, capsys, dim_b):
    """`run_enrich_repo` cleared the repo's `@enrich` vectors and then embedded, so a changed
    `[embeddings]` model deleted them before the store's identity guard refused the run. A
    probe vector now goes through the guard first, as `kb ingest` does."""
    cfg, store_dir, calls = _seed_enrich_store(
        tmp_path, monkeypatch, config=_CONFIG_FAKE_EMBED)
    docs = [Document(id="d1", title="Runbook", text="how to page", uri="https://x/1"),
            Document(id="d2", title="Design doc", text="architecture notes", uri="https://x/2")]
    monkeypatch.setattr(enrich, "search_source",
                        lambda src, terms, **_kw: calls.append(terms) or docs)
    part = enrich_partition(REPO)

    def run(embedder):
        monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: embedder)
        with pytest.raises(SystemExit) as e:
            main(["kb", "enrich", REPO, "--config", str(cfg)])
        return e.value.code

    assert run(_ModelEmbedder("openai:fake-a")) == 0
    assert _vectors_of(store_dir, part) == 2
    nodes_before = sorted(n.id for n in read_shard(store_dir, part).nodes)
    calls.clear()
    capsys.readouterr()

    assert run(_ModelEmbedder("openai:fake-b", dim_b)) == 1

    out = capsys.readouterr().out
    assert out.strip(), "the capture saw nothing, so the assertions below prove nothing"
    assert _vectors_of(store_dir, part) == 2, "the refused run cleared the partition's vectors"
    assert sorted(n.id for n in read_shard(store_dir, part).nodes) == nodes_before
    assert calls == [], "a source was searched on a run the guard refused"
    assert "this embedding store was built with" in out
    assert "This run changed nothing" in out


def test_enrich_goes_on_when_the_probe_got_no_answer(tmp_path, monkeypatch, gls_logs):
    """An embedder that timed out on the probe is not a refusal: the guard did not run, and
    the embed phase reports an embedder that stays down. Today's behavior stays."""
    cfg, store_dir, calls = _seed_enrich_store(
        tmp_path, monkeypatch, config=_CONFIG_FAKE_EMBED)
    docs = [Document(id="d1", title="Runbook", text="how to page", uri="https://x/1")]
    monkeypatch.setattr(enrich, "search_source", lambda src, terms, **_kw: docs)
    monkeypatch.setattr(embeddings, "build_embedder",
                        lambda _cfg: _ModelEmbedder("openai:fake-a", flaky_first=True))

    assert cmd_enrich(Namespace(config=str(cfg), workspace=None, args=[REPO])) == 0
    assert _vectors_of(store_dir, enrich_partition(REPO)) == 1
    assert "This run changed nothing" not in gls_logs.text


def _enrich_twice(tmp_path, monkeypatch, second, second_docs):
    """Enrich ``REPO`` with a working embedder (two documents, two vectors), then again with
    ``second`` and ``second_docs``. Returns ``(store dir, partition)``."""
    cfg, store_dir, _calls = _seed_enrich_store(
        tmp_path, monkeypatch, config=_CONFIG_FAKE_EMBED)
    docs = [Document(id="d1", title="Runbook", text="how to page", uri="https://x/1"),
            Document(id="d2", title="Design doc", text="architecture notes", uri="https://x/2")]
    part = enrich_partition(REPO)

    def run(embedder, found):
        monkeypatch.setattr(enrich, "search_source", lambda src, terms, **_kw: found)
        monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: embedder)
        return cmd_enrich(Namespace(config=str(cfg), workspace=None, args=[REPO]))

    run(_ModelEmbedder("openai:fake-a"), docs)
    assert _vectors_of(store_dir, part) == 2, "setup: the first run stored no vectors"
    run(second, second_docs if second_docs is not None else docs)
    return store_dir, part


def test_enrich_keeps_the_vectors_when_the_embedder_never_answers(
        tmp_path, monkeypatch, gls_logs):
    """The probe got no answer, so the guard had not run when `run_enrich_repo` cleared the
    partition's vectors. An embedder that stayed down for the batch too left the partition
    with 0 vectors, and the run still ended with `Enrich complete`."""
    store_dir, part = _enrich_twice(
        tmp_path, monkeypatch, _ModelEmbedder("openai:fake-a", down=True), None)

    assert "embed: stopped after 0 of 2 document(s)" in gls_logs.text, (
        "the embed phase never failed, so the count below proves nothing")
    assert _vectors_of(store_dir, part) == 2


def test_enrich_clears_the_old_vectors_when_every_document_body_is_empty_and_the_embedder_is_down(
        tmp_path, monkeypatch):
    """The one exception to the keep rule: no batch runs, so nothing waits on a first batch,
    and the old vectors belong to text that no longer exists."""
    docs = [Document(id="d1", title="Runbook", text="", uri="https://x/1"),
            Document(id="d2", title="Design doc", text="", uri="https://x/2")]
    store_dir, part = _enrich_twice(
        tmp_path, monkeypatch, _ModelEmbedder("openai:fake-a", down=True), docs)

    assert _vectors_of(store_dir, part) == 0


def test_enrich_clears_the_old_vectors_once_the_first_batch_after_an_unanswered_probe_passes(
        tmp_path, monkeypatch):
    """The clear waits for the first batch, it is not dropped: d2 is gone from the source,
    so its vector goes when the first batch of the new run holds a vector."""
    docs = [Document(id="d1", title="Runbook", text="how to page", uri="https://x/1")]
    store_dir, part = _enrich_twice(
        tmp_path, monkeypatch, _ModelEmbedder("openai:fake-a", flaky_first=True), docs)

    assert _vectors_of(store_dir, part) == 1


@pytest.mark.parametrize(("flaky_first", "checked"), [(False, True), (True, False)],
                         ids=["probe-answers", "probe-gets-no-answer"])
def test_enrich_tells_each_repo_run_whether_the_probe_passed_the_guard(
        tmp_path, monkeypatch, flaky_first, checked):
    cfg, _store_dir, _calls = _seed_enrich_store(
        tmp_path, monkeypatch, config=_CONFIG_FAKE_EMBED)
    seen = []
    real = enrich.run_enrich_repo
    monkeypatch.setattr(enrich, "run_enrich_repo",
                        lambda *a, **k: (seen.append(k["embedder_checked"]), real(*a, **k))[1])
    monkeypatch.setattr(embeddings, "build_embedder",
                        lambda _cfg: _ModelEmbedder("openai:fake-a", flaky_first=flaky_first))

    assert cmd_enrich(Namespace(config=str(cfg), workspace=None, args=[REPO])) == 0

    assert seen == [checked]
