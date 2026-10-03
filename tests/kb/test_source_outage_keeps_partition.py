"""A source that could not be reached must not replace a good partition with an empty one.

`kb enrich` and `kb connect` rewrite a repo's partition (`@enrich:<repo>`,
`@connect:<repo>`) from whatever the sources return. Their connector calls never raise:
a dead source returns [] and logs the reason. So an outage looked like "the source has
nothing", and the rewrite cleared the repo's previous graph nodes and edges, its shard and
its vectors. The run still exited 0 whenever another repo had results.

The rule these tests pin, in both commands:

* A source that FAILED (the call was written off through `resilience.note_unavailable`, or
  it raised) leaves the repo's previous partition untouched and is reported. The run exits
  1, or 0 under `--exit-zero-on-partial`, the same rule `kb ingest` follows.
* A source that ANSWERED with nothing is a real answer and still clears the partition.
  `connect` sweeps vectors before the enrichers write, for a documented reason, and that
  reason holds: the sweep still happens on every successful pass.
"""

from __future__ import annotations

from argparse import Namespace

import pytest

import contextlake.kb.connectors.atlassian as atlassian_mod
import contextlake.kb.connectors.enrich as enrich
import contextlake.kb.connectors.orchestrate as orch
import contextlake.kb.embeddings as emb_pkg
import contextlake.kb.references as refs
from contextlake.kb.commands import cmd_connect, cmd_enrich
from contextlake.kb.config import KbConfig, SourceCfg
from contextlake.kb.connectors.orchestrate import connect_partition
from contextlake.kb.embeddings.store import VectorStore
from contextlake.kb.model import EXTERNAL_REPO, Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.resilience import note_unavailable, reset_breakers
from contextlake.kb.sources.base import Document
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, read_shard, shard_path, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore


class _FakeEmbedder:
    name = "fake-embedder"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _vector_count(store_dir, part):
    vs = VectorStore(store_dir / "embeddings.sqlite")
    try:
        return vs.count_repo(part)
    finally:
        vs.close()


def _vector_ids(store_dir, part):
    vs = VectorStore(store_dir / "embeddings.sqlite")
    try:
        return sorted(r[0] for r in vs.conn.execute(
            "SELECT node_id FROM embeddings WHERE repo_id=?", (part,)))
    finally:
        vs.close()


@pytest.fixture(autouse=True)
def _fresh_breakers():
    reset_breakers()
    yield
    reset_breakers()


# =================================== kb enrich ===================================

_ENRICH_CONFIG = """
[kb]
store_dir = "{store}"

[[sources]]
type = "mcp"
name = "wiki"
mcp = "http://localhost:9999/mcp"
tool = "search"

[embeddings]
enabled = true
"""

DOWN = "DOWN"


def _prov():
    from datetime import date
    return Provenance(source_file="app/main.py", verified_at=date.today())


def _enrich_world(tmp_path, monkeypatch, config=_ENRICH_CONFIG):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(config.format(store=store_dir.as_posix()))
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    for name in ("alpha", "beta"):
        repo = f"group/{name}"
        nodes = [Node(id=f"{name}-n1", repo=repo, kind="class", name="ForecastService",
                      file="app/forecast.py"),
                 Node(id=f"{name}-n2", repo=repo, kind="function", name="readSensor",
                      file="app/readings.py")]
        edges = [Edge(src=f"{name}-n1", dst=f"{name}-n2", relation="calls",
                      confidence=Confidence.EXTRACTED, provenance=_prov())]
        write_shard(store_dir, GraphShard(repo=repo, head_commit="abc", nodes=nodes,
                                          edges=edges))
        store.upsert_nodes(repo, nodes)
        store.upsert_repo(Repo(id=repo, path=str(tmp_path / name)))
    store.close()
    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())
    # The brute store, so the tests below can read its `embeddings` table directly.
    monkeypatch.setattr("contextlake.kb.embeddings.store.build_vector_store",
                        lambda path, **kw: VectorStore(path))
    return cfg, store_dir


def _docs(*ids):
    # Each text names a symbol of the repo, so the run stores edges to code as well.
    return [Document(id=i, title=f"Page {i}", text=f"{i}: readSensor retries twice",
                     uri=f"https://x/{i}") for i in ids]


def _answer_with(monkeypatch, plan):
    """Stand in for the MCP search. ``plan[(source name, repo name)]`` is a document list
    (an answer, possibly empty) or ``DOWN``, which does what the real
    ``mcp_tool_query`` does with a dead server: log the reason, bump the degraded counter,
    return []."""
    def answer(cfg, terms, timeout=None):
        key = (enrich._cfg_get(cfg, "name"), terms[0])
        outcome = plan[key]
        if outcome == DOWN:
            note_unavailable("mcp tool 'search'", RuntimeError("503 Service Unavailable"))
            return []
        return outcome

    monkeypatch.setattr(enrich, "mcp_tool_query", answer)


def _run_enrich(cfg, **flags):
    return cmd_enrich(Namespace(config=str(cfg), workspace=None, args=[], **flags))


def _enrich_snapshot(store_dir, repo):
    part = enrich.enrich_partition(repo)
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        ids = sorted(r[0] for r in store.conn.execute(
            "SELECT node_id FROM nodes WHERE repo_id=?", (part,)))
        counts = store.repo_counts(part)
    finally:
        store.close()
    shard = shard_path(store_dir, part)
    return {
        "node_ids": ids,
        "edges": counts[1],
        "shard_bytes": shard.read_bytes() if shard.exists() else None,
        "vector_ids": _vector_ids(store_dir, part),
    }


def _healthy_first_run(cfg, store_dir, monkeypatch, plan_docs=("d1", "d2")):
    _answer_with(monkeypatch, {("wiki", "alpha"): _docs(*plan_docs),
                               ("wiki", "beta"): _docs(*plan_docs)})
    assert _run_enrich(cfg) == 0
    before = _enrich_snapshot(store_dir, "group/alpha")
    assert len(before["node_ids"]) == 2 and before["edges"] > 0, "setup: a real partition"
    assert before["shard_bytes"] and before["vector_ids"], "setup: shard and vectors exist"
    return before


def test_enrich_an_outage_for_one_repo_keeps_its_previous_partition(
        tmp_path, monkeypatch, gls_logs):
    cfg, store_dir = _enrich_world(tmp_path, monkeypatch)
    before = _healthy_first_run(cfg, store_dir, monkeypatch)

    # Second run: the source is down for alpha and answers normally for beta.
    _answer_with(monkeypatch, {("wiki", "alpha"): DOWN, ("wiki", "beta"): _docs("d3")})
    code = _run_enrich(cfg)

    after = _enrich_snapshot(store_dir, "group/alpha")
    # LOAD-BEARING: nodes, edges, shard bytes and vectors are all as they were.
    assert after == before
    beta = _enrich_snapshot(store_dir, "group/beta")
    assert beta["node_ids"] == ["@enrich:group/beta:d3"], "the healthy repo was refreshed"
    assert code == 1, "a failed source is a failure of the run, even with results elsewhere"
    assert "kept" in gls_logs.text and "group/alpha" in gls_logs.text


def test_enrich_exit_zero_on_partial_still_keeps_the_partition_and_exits_zero(
        tmp_path, monkeypatch):
    cfg, store_dir = _enrich_world(tmp_path, monkeypatch)
    before = _healthy_first_run(cfg, store_dir, monkeypatch)

    _answer_with(monkeypatch, {("wiki", "alpha"): DOWN, ("wiki", "beta"): _docs("d3")})
    assert _run_enrich(cfg, exit_zero_on_partial=True) == 0
    assert _enrich_snapshot(store_dir, "group/alpha") == before


def test_enrich_an_answer_of_no_documents_still_clears_the_partition(tmp_path, monkeypatch):
    """The other direction. "The source has nothing" is an answer, and the partition
    follows it: stale results must not outlive the source that held them."""
    cfg, store_dir = _enrich_world(tmp_path, monkeypatch)
    _healthy_first_run(cfg, store_dir, monkeypatch)

    _answer_with(monkeypatch, {("wiki", "alpha"): [], ("wiki", "beta"): _docs("d3")})
    assert _run_enrich(cfg) == 0, "nothing failed, so the run is clean"

    after = _enrich_snapshot(store_dir, "group/alpha")
    assert after["node_ids"] == [] and after["edges"] == 0
    assert after["vector_ids"] == []
    assert read_shard(store_dir, enrich.enrich_partition("group/alpha")).nodes == []


def test_enrich_a_source_that_raises_keeps_the_previous_partition_too(
        tmp_path, monkeypatch):
    """`search_source` catches an exception from the Atlassian path itself, a different
    door from the swallowed MCP failure above, and it has to end the same way."""
    config = _ENRICH_CONFIG.replace('type = "mcp"', 'type = "atlassian"').replace(
        'mcp = "http://localhost:9999/mcp"\ntool = "search"\n', "")
    cfg, store_dir = _enrich_world(tmp_path, monkeypatch, config)

    mode = {"alpha": "ok"}

    class _Atlassian:
        def __init__(self, *a, **k):
            pass

        def search(self, query):
            repo = query.split()[0]
            if mode.get(repo, "ok") == "boom":
                raise RuntimeError("503 from the Rovo endpoint")
            return [{"title": f"Runbook for {repo}", "url": f"https://x/{repo}",
                     "text": "readSensor retries twice"}]

    monkeypatch.setattr(atlassian_mod, "AtlassianConnector", _Atlassian)
    assert _run_enrich(cfg) == 0
    before = _enrich_snapshot(store_dir, "group/alpha")
    assert before["node_ids"], "setup: a real partition"

    mode["alpha"] = "boom"
    code = _run_enrich(cfg)
    assert _enrich_snapshot(store_dir, "group/alpha") == before
    assert code == 1


def test_enrich_one_failed_source_keeps_the_whole_partition_even_if_another_answered(
        tmp_path, monkeypatch):
    """The partition is one unit. Rewriting it from the healthy source alone would drop
    the failed source's share of the old one, and that share cannot be told apart."""
    config = _ENRICH_CONFIG.replace('[embeddings]', '''[[sources]]
type = "mcp"
name = "tickets"
mcp = "http://localhost:9998/mcp"
tool = "search"

[embeddings]''')
    cfg, store_dir = _enrich_world(tmp_path, monkeypatch, config)
    _answer_with(monkeypatch, {("wiki", "alpha"): _docs("w1"), ("tickets", "alpha"): _docs("t1"),
                               ("wiki", "beta"): [], ("tickets", "beta"): []})
    assert _run_enrich(cfg) == 0
    before = _enrich_snapshot(store_dir, "group/alpha")
    assert len(before["node_ids"]) == 2

    _answer_with(monkeypatch, {("wiki", "alpha"): _docs("w2"), ("tickets", "alpha"): DOWN,
                               ("wiki", "beta"): [], ("tickets", "beta"): []})
    code = _run_enrich(cfg)
    assert _enrich_snapshot(store_dir, "group/alpha") == before, \
        "the wiki's new page w2 must not be stored while the tickets source is down"
    assert code == 1


def test_run_enrich_repo_reports_the_failed_source_count(tmp_path, monkeypatch):
    _cfg, store_dir = _enrich_world(tmp_path, monkeypatch)
    kb_cfg = KbConfig(sources=[SourceCfg(type="mcp", name="wiki", tool="search")])
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        _answer_with(monkeypatch, {("wiki", "alpha"): DOWN})
        counts = enrich.run_enrich_repo(store, store_dir, kb_cfg, "group/alpha")
        assert counts == enrich.EnrichCounts(terms=counts.terms, documents=0, edges=0,
                                             unavailable=1)
        _answer_with(monkeypatch, {("wiki", "alpha"): []})
        counts = enrich.run_enrich_repo(store, store_dir, kb_cfg, "group/alpha")
        assert counts.unavailable == 0, "an empty answer is not an outage"
    finally:
        store.close()


# =================================== kb connect ==================================

_CONNECT_CONFIG = """
[kb]
store_dir = "{store}"

[[sources]]
type = "atlassian"
name = "site-a"

[[rules]]
type = "branch_key"
pattern = "[A-Z]+-[0-9]+"

[embeddings]
enabled = true
"""


def _connect_world(tmp_path, monkeypatch, config=_CONNECT_CONFIG):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(config.format(store=store_dir.as_posix()))
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        store.upsert_repo(Repo(id=f"group/{name}", path=str(tmp_path / name)))
    store.close()
    monkeypatch.setattr(refs, "extract_issue_keys", lambda path, pattern, **k: ["PROJ-1"])
    monkeypatch.setattr(refs, "scrape_links", lambda path, patterns, **k: [])
    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())
    monkeypatch.setattr("contextlake.kb.embeddings.store.build_vector_store",
                        lambda path, **kw: VectorStore(path))
    return cfg, store_dir


def _run_connect(cfg, **flags):
    return cmd_connect(Namespace(config=str(cfg), workspace=None, source=None, repo=None,
                                 args=[], **flags))


def _connect_snapshot(store_dir, repo):
    part = connect_partition(repo)
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        edges = sorted((r["src"], r["dst"], r["relation"]) for r in store.conn.execute(
            "SELECT src, dst, relation FROM edges WHERE repo_id=?", (part,)))
        # The external nodes are shared by every repo; these are the ones THIS repo's
        # planned enricher made (their ids carry the repo, which real ids do not).
        external = sorted(r[0] for r in store.conn.execute(
            "SELECT node_id FROM nodes WHERE repo_id=? AND node_id LIKE ?",
            (EXTERNAL_REPO, f"%:{repo}:%")))
    finally:
        store.close()
    return {"edges": edges, "external_nodes": external,
            "vector_ids": _vector_ids(store_dir, part)}


def _planned_enricher(monkeypatch, plan, *, name="site-a"):
    """Replace ``_build_enrichers`` with one enricher whose behaviour is ``plan[repo_id]``:
    ``DOWN`` (a connector that writes the call off and returns nothing), ``"RAISE"``, or
    ``(label, ...)`` for an answer holding one issue per label. Like the real enrichers
    it embeds its own nodes through the ``vector_store`` it was built with."""
    import contextlake.kb.cmds.connect as cmds

    held = {}

    def build(sources, store, **kw):
        held["vs"] = kw.get("vector_store")

        def enrich_repo(repo_id, keys, links, symbol_keys):
            outcome = plan[repo_id]
            if outcome == DOWN:
                note_unavailable("gitlab (glab api)", RuntimeError("401 Unauthorized"))
                return [], []
            if outcome == "RAISE":
                raise RuntimeError("connector crashed")
            nodes = [Node(id=f"issue:{repo_id}:{k}", repo=EXTERNAL_REPO, kind="issue", name=k)
                     for k in outcome]
            edges = [Edge(src=f"sym:{repo_id}", dst=n.id, relation="tracked_by",
                          confidence=Confidence.EXTRACTED, provenance=_prov())
                     for n in nodes]
            vs = held["vs"]
            if vs is not None and nodes:
                vs.upsert([(n.id, connect_partition(repo_id), [1.0, 0.0]) for n in nodes])
            return nodes, edges

        return [enrich_repo], [name]

    monkeypatch.setattr(cmds, "_build_enrichers", build)
    return held


def _first_connect_run(cfg, store_dir, monkeypatch):
    plan = {"group/alpha": ("A1", "A2"), "group/beta": ("B1",)}
    _planned_enricher(monkeypatch, plan)
    assert _run_connect(cfg) == 0
    before = _connect_snapshot(store_dir, "group/alpha")
    assert len(before["edges"]) == 2 and len(before["vector_ids"]) == 2, "setup"
    return before


def test_connect_an_outage_for_one_repo_keeps_its_previous_partition(
        tmp_path, monkeypatch, gls_logs):
    cfg, store_dir = _connect_world(tmp_path, monkeypatch)
    before = _first_connect_run(cfg, store_dir, monkeypatch)

    _planned_enricher(monkeypatch, {"group/alpha": DOWN, "group/beta": ("B2", "B3")})
    code = _run_connect(cfg)

    # LOAD-BEARING: alpha's edges, the external nodes they point at, and its vectors.
    assert _connect_snapshot(store_dir, "group/alpha") == before
    beta = _connect_snapshot(store_dir, "group/beta")
    assert [e[1] for e in beta["edges"]] == ["issue:group/beta:B2", "issue:group/beta:B3"]
    assert code == 1, "a failed source is a failure of the run, even with links elsewhere"
    assert "kept" in gls_logs.text and "group/alpha" in gls_logs.text


def test_connect_exit_zero_on_partial_still_keeps_the_partition_and_exits_zero(
        tmp_path, monkeypatch):
    cfg, store_dir = _connect_world(tmp_path, monkeypatch)
    before = _first_connect_run(cfg, store_dir, monkeypatch)

    _planned_enricher(monkeypatch, {"group/alpha": DOWN, "group/beta": ("B2",)})
    assert _run_connect(cfg, exit_zero_on_partial=True) == 0
    assert _connect_snapshot(store_dir, "group/alpha") == before


def test_connect_an_answer_of_no_links_still_clears_edges_nodes_and_vectors(
        tmp_path, monkeypatch):
    """The documented reason for sweeping vectors before the enrichers write still holds:
    an empty-but-successful pass replaces the partition, and the old vectors go with it."""
    cfg, store_dir = _connect_world(tmp_path, monkeypatch)
    _first_connect_run(cfg, store_dir, monkeypatch)

    _planned_enricher(monkeypatch, {"group/alpha": (), "group/beta": ("B2",)})
    assert _run_connect(cfg) == 0

    after = _connect_snapshot(store_dir, "group/alpha")
    assert after["edges"] == []
    assert after["vector_ids"] == []
    assert not [n for n in after["external_nodes"] if n.startswith("issue:group/alpha")], \
        "the external nodes the cleared edges pointed at are pruned"


def test_connect_a_pass_with_new_links_replaces_the_old_vectors(tmp_path, monkeypatch):
    """A successful pass still sweeps, then writes what the sources embedded."""
    cfg, store_dir = _connect_world(tmp_path, monkeypatch)
    _first_connect_run(cfg, store_dir, monkeypatch)

    _planned_enricher(monkeypatch, {"group/alpha": ("A9",), "group/beta": ("B1",)})
    assert _run_connect(cfg) == 0
    assert _connect_snapshot(store_dir, "group/alpha")["vector_ids"] == ["issue:group/alpha:A9"]


def test_connect_a_source_that_raises_keeps_the_previous_partition(tmp_path, monkeypatch):
    cfg, store_dir = _connect_world(tmp_path, monkeypatch)
    before = _first_connect_run(cfg, store_dir, monkeypatch)

    _planned_enricher(monkeypatch, {"group/alpha": "RAISE", "group/beta": ("B2",)})
    code = _run_connect(cfg)
    assert _connect_snapshot(store_dir, "group/alpha") == before
    assert code == 1


def test_connect_one_failed_source_keeps_the_whole_partition_even_if_another_answered(
        tmp_path, monkeypatch):
    import contextlake.kb.cmds.connect as cmds

    cfg, store_dir = _connect_world(tmp_path, monkeypatch)
    before = _first_connect_run(cfg, store_dir, monkeypatch)

    held = {}

    def build(sources, store, **kw):
        held["vs"] = kw.get("vector_store")

        def healthy(repo_id, keys, links, symbol_keys):
            n = Node(id=f"issue:{repo_id}:NEW", repo=EXTERNAL_REPO, kind="issue", name="NEW")
            held["vs"].upsert([(n.id, connect_partition(repo_id), [1.0, 0.0])])
            return [n], [Edge(src=f"sym:{repo_id}", dst=n.id, relation="tracked_by",
                              confidence=Confidence.EXTRACTED, provenance=_prov())]

        def down(repo_id, keys, links, symbol_keys):
            note_unavailable("slack channel C1", RuntimeError("not_authed"))
            return [], []

        return [healthy, down], ["site-a", "team"]

    monkeypatch.setattr(cmds, "_build_enrichers", build)
    code = _run_connect(cfg)
    after = _connect_snapshot(store_dir, "group/alpha")
    assert after == before, "the healthy source's NEW link must not be stored"
    assert "issue:group/alpha:NEW" not in after["vector_ids"]
    assert code == 1


# --- a source dropped while the enrichers are being built ------------------------

_TWO_SOURCE_CONFIG = """
[kb]
store_dir = "{store}"

[[sources]]
type = "atlassian"
name = "site-a"

[[sources]]
type = "slack"
name = "team"

[[rules]]
type = "branch_key"
pattern = "[A-Z]+-[0-9]+"

[[rules]]
type = "link_scrape"
patterns = ["https://acme.slack.com/"]

[embeddings]
enabled = false
"""


class _AtlassianStub:
    name = "site-a"

    def __init__(self, reachable):
        self.reachable = reachable

    def discover_sites(self):
        if not self.reachable:
            raise OSError("connection refused")
        return {"https://example.atlassian.net": "cloud-1"}

    def verify_issues(self, cloud_id, keys, batch=100):
        meta = {"summary": "Real", "status": "Open",
                "url": "https://example.atlassian.net/browse/PROJ-1"}
        return {"PROJ-1": meta} if "PROJ-1" in keys else {}


class _SlackStub:
    name = "team"
    hosts = ("slack.com",)

    def verify(self, channel, **kw):
        return True

    def fetch_messages(self, channel, **kw):
        return []


def test_connect_a_source_dropped_at_build_time_keeps_every_previous_partition(
        tmp_path, monkeypatch, gls_logs):
    """Site discovery failing drops the Atlassian source before any repo is visited. With
    a second source configured, every repo's partition used to be rewritten from that one
    alone, which wiped the stored ticket links, and the run exited 0."""
    cfg, store_dir = _connect_world(tmp_path, monkeypatch, _TWO_SOURCE_CONFIG)
    monkeypatch.setattr(refs, "scrape_links",
                        lambda path, patterns, **k: ["https://acme.slack.com/archives/C0123ABCD"])
    monkeypatch.setattr(orch, "build_slack", lambda src: _SlackStub())

    state = {"up": True}
    monkeypatch.setattr(orch, "build_atlassian", lambda src: _AtlassianStub(state["up"]))
    assert _run_connect(cfg) == 0
    before = _connect_snapshot(store_dir, "group/alpha")
    assert any(e[2] == "tracked_by" for e in before["edges"]), "setup: ticket links stored"

    state["up"] = False
    code = _run_connect(cfg)

    assert _connect_snapshot(store_dir, "group/alpha") == before
    assert _connect_snapshot(store_dir, "group/beta")["edges"], "beta kept its links too"
    assert code == 1
    assert "site-a" in gls_logs.text and "kept" in gls_logs.text


def test_build_enrichers_names_the_sources_it_had_to_drop():
    from contextlake.kb.cmds.connect import _build_enrichers
    from contextlake.kb.config import SourceCfg

    orig = orch.build_atlassian
    orch.build_atlassian = lambda src: _AtlassianStub(False)
    try:
        dropped: list[str] = []
        enrichers, names = _build_enrichers(
            [SourceCfg(type="atlassian", name="site-a")], store=None, dropped=dropped)
    finally:
        orch.build_atlassian = orig
    assert enrichers == [] and names == []
    assert dropped == ["site-a"]


# --- the escape hatch reaches kb connect and kb enrich from the real CLI ----------

def test_exit_zero_on_partial_is_read_by_connect_from_the_real_cli(tmp_path, monkeypatch):
    """A `Namespace(exit_zero_on_partial=True)` passes even if argparse never sets it, so
    this goes through `cli.main`. The flag is a pre-command global."""
    from contextlake.cli import main

    cfg, _store_dir = _connect_world(tmp_path, monkeypatch)
    _planned_enricher(monkeypatch, {"group/alpha": DOWN, "group/beta": ("B1",)})

    with pytest.raises(SystemExit) as strict:
        main(["kb", "connect", "--config", str(cfg)])
    assert strict.value.code == 1
    with pytest.raises(SystemExit) as lenient:
        main(["--exit-zero-on-partial", "kb", "connect", "--config", str(cfg)])
    assert lenient.value.code == 0


def test_exit_zero_on_partial_is_read_by_enrich_from_the_real_cli(tmp_path, monkeypatch):
    from contextlake.cli import main

    cfg, _store_dir = _enrich_world(tmp_path, monkeypatch)
    _answer_with(monkeypatch, {("wiki", "alpha"): DOWN, ("wiki", "beta"): _docs("d1")})

    with pytest.raises(SystemExit) as strict:
        main(["kb", "enrich", "--config", str(cfg)])
    assert strict.value.code == 1
    with pytest.raises(SystemExit) as lenient:
        main(["--exit-zero-on-partial", "kb", "enrich", "--config", str(cfg)])
    assert lenient.value.code == 0
