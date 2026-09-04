"""End-to-end test for the `connect` command: config -> stubbed connector ->
reconciled external nodes/edges persisted in an isolated store partition."""

import re
from argparse import Namespace

import contextlake.kb.connectors.orchestrate as orch
import contextlake.kb.references as refs
from contextlake.kb.cmds.connect import _rule_patterns
from contextlake.kb.commands import cmd_connect
from contextlake.kb.config import RuleCfg
from contextlake.kb.connectors.orchestrate import connect_partition
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore

_CONFIG = """
[kb]
store_dir = "{store}"

[[sources]]
type = "atlassian"
name = "site-a"

[[rules]]
type = "branch_key"
pattern = "[A-Z]+-[0-9]+"
"""


class _Stub:
    name = "site-a"

    def discover_sites(self):
        return {"https://example.atlassian.net": "cloud-1"}

    def verify_issues(self, cloud_id, keys, batch=100):
        meta = {"summary": "Real", "status": "Open",
                "url": "https://example.atlassian.net/browse/PROJ-1"}
        return {"PROJ-1": meta} if "PROJ-1" in keys else {}


def test_connect_persists_confirmed_links(tmp_path, monkeypatch, gls_logs):
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate ~/.contextlake/kb.toml
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG.format(store=store_dir.as_posix()))

    repo = tmp_path / "app"
    repo.mkdir()

    monkeypatch.setattr(orch, "build_atlassian", lambda src: _Stub())
    monkeypatch.setattr(refs, "extract_issue_keys", lambda path, pattern, **k: ["PROJ-1", "UTF-8"])
    monkeypatch.setattr(refs, "scrape_links", lambda path, patterns, **k: [])

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 0

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        check_schema(store)
        issues = store.nodes_by_name("PROJ-1")
        assert issues and issues[0].kind == "issue"
        assert issues[0].attrs.get("summary") == "Real"
        assert not store.nodes_by_name("UTF-8")  # false-positive pruned
        # output lives in the isolated connector partition
        assert store.stats().nodes >= 2  # repo node + issue node
    finally:
        store.close()

    # glyph-prefixed summary (H4): counts/text unchanged, just wrapped
    assert "✓ Connect complete:" in gls_logs.text


def test_connect_skips_disabled_sources(tmp_path, monkeypatch):
    """A source with `enabled = false` must be skipped entirely -- no connector
    is even built for it -- so `disable` is a real no-op guarantee, not just a
    cosmetic flag in `source list`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG.replace(
        'name = "site-a"', 'name = "site-a"\nenabled = false',
    ).format(store=store_dir.as_posix()))
    repo = tmp_path / "app"
    repo.mkdir()

    built = []
    monkeypatch.setattr(orch, "build_atlassian", lambda src: built.append(src) or _Stub())
    monkeypatch.setattr(refs, "extract_issue_keys", lambda path, pattern, **k: ["PROJ-1"])
    monkeypatch.setattr(refs, "scrape_links", lambda path, patterns, **k: [])

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 0
    assert built == []  # the disabled source's connector is never constructed


def test_connect_attributes_ticket_to_symbol_via_docstring(tmp_path, monkeypatch, gls_logs):
    """End-to-end: a symbol's own docstring carries an issue key -> a
    symbol-SOURCED tracked_by edge is verified and stored, not just a
    repo-level one."""
    from contextlake.kb.model import Node
    from contextlake.kb.store.shards import GraphShard, write_shard

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG.format(store=store_dir.as_posix()))
    repo = tmp_path / "app"
    repo.mkdir()

    symbol = Node(id="sym-ingest", repo="group/app", kind="function", name="ingest_batch",
                 file="ingest.py", line_start=10,
                 attrs={"doc": "Handles a reading batch. See PROJ-1 for the backfill edge case."})
    write_shard(store_dir, GraphShard(repo="group/app", head_commit="abc123", nodes=[symbol]))

    monkeypatch.setattr(orch, "build_atlassian", lambda src: _Stub())
    monkeypatch.setattr(refs, "extract_issue_keys", lambda path, pattern, **k: [])
    monkeypatch.setattr(refs, "scrape_links", lambda path, patterns, **k: [])

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 0

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        check_schema(store)
        ticket_edges = [e for e in store.neighbors("sym-ingest", direction="out")
                        if e.relation == "tracked_by"]
        assert len(ticket_edges) == 1
        assert ticket_edges[0].confidence.value == "INFERRED"  # promoted, JQL-confirmed
        issue = store.get_node(ticket_edges[0].dst)
        assert issue.kind == "issue" and issue.name == "PROJ-1"
    finally:
        store.close()


def test_connect_returns_nonzero_when_all_sources_fail(tmp_path, monkeypatch):
    """Every source call failing (e.g. an unreachable connector) is a non-zero
    exit, not a silent 'Connect complete: 0 links'."""
    import contextlake.kb.cmds.connect as cmds

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG.format(store=store_dir.as_posix()))
    repo = tmp_path / "app"
    repo.mkdir()

    def boom_enricher(repo_id, keys, links, symbol_keys):
        raise RuntimeError("atlassian unreachable")

    # Patch cmd_connect's own module (kb/cmds/connect.py), where _build_enrichers
    # is defined and called directly -- not contextlake.kb.commands (the shim's
    # separate re-exported copy), which cmd_connect never looks up at call time.
    monkeypatch.setattr(cmds, "_build_enrichers",
                        lambda sources, store, **kw: ([boom_enricher], ["site-a"]))
    monkeypatch.setattr(refs, "extract_issue_keys", lambda path, pattern, **k: ["PROJ-1"])
    monkeypatch.setattr(refs, "scrape_links", lambda path, patterns, **k: [])

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 1


def test_partition_name():
    assert connect_partition("group/app") == "@connect:group/app"


_SAMPLE_FIGMA_URL = "https://www.figma.com/design/Xy9/Flow"
_SAMPLE_SLACK_URL = "https://acme.slack.com/archives/C0123ABCD"


def _matches(patterns, url):
    """Does some pattern actually match `url`? Substring containment would pass
    on a pattern that never matches anything (and breaks the moment a metachar
    is correctly escaped), so assert on the regex behaviour instead."""
    return any(re.search(p, url) for p in patterns)


def test_rule_patterns_includes_builtin_defaults_when_unconfigured():
    branch_key, link_patterns = _rule_patterns([])  # no [[rules]] at all
    assert branch_key is None
    assert _matches(link_patterns, _SAMPLE_FIGMA_URL)
    assert _matches(link_patterns, _SAMPLE_SLACK_URL)


def test_rule_patterns_explicit_link_scrape_rule_still_works_alongside_defaults():
    rules = [RuleCfg(type="link_scrape", pattern=r"https://internal\.wiki/\S+")]
    branch_key, link_patterns = _rule_patterns(rules)
    assert _matches(link_patterns, "https://internal.wiki/page")
    assert _matches(link_patterns, _SAMPLE_FIGMA_URL)  # built-ins still present
    assert _matches(link_patterns, _SAMPLE_SLACK_URL)


def test_builtin_link_patterns_do_not_match_lookalike_hosts():
    _, link_patterns = _rule_patterns([])
    assert not _matches(link_patterns, "https://www.figmaXcom/design/Xy9/Flow")
    assert not _matches(link_patterns, "https://acme.slackXcom/archives/C0123ABCD")


_FIGMA_CONFIG = """
[kb]
store_dir = "{store}"

[[sources]]
type = "figma"
name = "design"

[[rules]]
type = "link_scrape"
patterns = ["https://www.figma.com/"]
"""


class _FigmaStub:
    name = "design"
    hosts = ("figma.com",)

    def fetch_metadata(self, file_key, **kw):
        return {"name": "Design System"}

    def verify(self, file_key, **kw):
        return True


def test_connect_persists_figma_designs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_FIGMA_CONFIG.format(store=store_dir.as_posix()))
    repo = tmp_path / "app"
    repo.mkdir()

    monkeypatch.setattr(orch, "build_figma", lambda src: _FigmaStub())
    monkeypatch.setattr(refs, "extract_issue_keys", lambda *a, **k: [])
    monkeypatch.setattr(
        refs, "scrape_links",
        lambda *a, **k: ["https://www.figma.com/design/Xy9/Flow"],
    )

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 0

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        check_schema(store)
        designs = store.nodes_by_name("Xy9")
        assert designs and designs[0].kind == "design"
        assert designs[0].attrs.get("title") == "Flow"  # name from the URL slug
        assert designs[0].attrs.get("verified") is True  # best-effort liveness flag
        assert designs[0].attrs.get("name") == "Design System"  # real metadata, deeper enrichment
    finally:
        store.close()


_SLACK_CONFIG = """
[kb]
store_dir = "{store}"

[[sources]]
type = "slack"
name = "team"

[[rules]]
type = "link_scrape"
patterns = ["https://acme.slack.com/"]
"""


class _SlackStub:
    name = "team"
    hosts = ("slack.com",)

    def verify(self, channel, **kw):
        return True

    def fetch_messages(self, channel, **kw):
        return []


def test_connect_persists_slack_channels(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_SLACK_CONFIG.format(store=store_dir.as_posix()))
    repo = tmp_path / "app"
    repo.mkdir()

    monkeypatch.setattr(orch, "build_slack", lambda src: _SlackStub())
    monkeypatch.setattr(refs, "extract_issue_keys", lambda *a, **k: [])
    monkeypatch.setattr(
        refs, "scrape_links",
        lambda *a, **k: ["https://acme.slack.com/archives/C0123ABCD"],
    )

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 0

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        check_schema(store)
        channels = store.nodes_by_name("C0123ABCD")
        assert channels and channels[0].kind == "channel"
        assert channels[0].attrs.get("verified") is True
    finally:
        store.close()


_SLACK_EMBED_CONFIG = _SLACK_CONFIG + """
[embeddings]
enabled = true
"""


class _FakeEmbedder:
    name = "fake-embedder"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def test_connect_sweeps_stale_connector_vectors(tmp_path, monkeypatch):
    """A connector node that disappears between runs must not leave its vector
    behind. `connect` rewrites the whole `@connect:<repo>` partition each pass,
    and the enrichers embed their own nodes mid-pass -- so the sweep has to run
    *before* they do, not alongside the graph's own clear_repo (which lands after
    the vectors were already written)."""
    from contextlake.kb.embeddings.store import VectorStore

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_SLACK_EMBED_CONFIG.format(store=store_dir.as_posix()))
    repo = tmp_path / "app"
    repo.mkdir()

    part = connect_partition("group/app")
    vec_path = store_dir / "embeddings.sqlite"
    seeded = VectorStore(vec_path)
    seeded.upsert([("stale_node_from_a_previous_run", part, [1.0, 0.0])])
    seeded.close()

    monkeypatch.setattr(orch, "build_slack", lambda src: _SlackStub())
    monkeypatch.setattr(refs, "extract_issue_keys", lambda *a, **k: [])
    monkeypatch.setattr(
        refs, "scrape_links",
        lambda *a, **k: ["https://acme.slack.com/archives/C0123ABCD"],
    )
    monkeypatch.setattr("contextlake.kb.embeddings.build_embedder",
                        lambda cfg_: _FakeEmbedder())
    monkeypatch.setattr("contextlake.kb.embeddings.store.build_vector_store",
                        lambda path, **kw: VectorStore(path))

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 0

    vs = VectorStore(vec_path)
    try:
        ids = {row[0] for row in
               vs.conn.execute("SELECT node_id FROM embeddings WHERE repo_id=?", (part,))}
        assert "stale_node_from_a_previous_run" not in ids  # swept
        assert ids  # ...and this pass's own channel vector survived the sweep
    finally:
        vs.close()


def test_connect_does_not_report_success_when_every_call_was_written_off(
        tmp_path, monkeypatch, gls_logs):
    """An expired token must not read like a repo with no open work.

    Connector methods are contractually non-raising, so a source whose every
    call 401s returns [] and the enricher itself "succeeds". That made a dead
    source produce a green tick, `0 external link(s) stored` and exit 0 --
    byte-identical to a healthy run over a quiet repo.
    """
    import subprocess

    import contextlake.kb.cmds.connect as cmds
    from contextlake.kb.resilience import note_unavailable, reset_breakers

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG.format(store=store_dir.as_posix()))
    repo = tmp_path / "app"
    repo.mkdir()
    reset_breakers()

    def unauthorized_enricher(repo_id, keys, links, symbol_keys):
        # Exactly what a connector does with a rejected call: log the reason and
        # return empty, rather than raise.
        note_unavailable("gitlab (glab api)", subprocess.CalledProcessError(
            1, ["glab", "api", "projects/x/merge_requests"], output="",
            stderr="401 Unauthorized (HTTP 401)\n"))
        return [], []

    monkeypatch.setattr(cmds, "_build_enrichers",
                        lambda sources, store, **kw: ([unauthorized_enricher], ["gl"]))
    monkeypatch.setattr(refs, "extract_issue_keys", lambda path, pattern, **k: ["PROJ-1"])
    monkeypatch.setattr(refs, "scrape_links", lambda path, patterns, **k: [])

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 1, "a run where every call was refused is not a success"

    text = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "401" in text, "the reason the calls failed must reach the user"
    assert not re.search(r"✓ Connect complete", text), "no green tick over a failed run"


def test_connect_one_bad_repo_does_not_abort_the_others(tmp_path, monkeypatch, gls_logs):
    """One unreadable repository must cost that repository, not the run.

    Reproduced on a real 20-repository fleet: a commit carrying a cp1252 byte
    made `extract_issue_keys` raise `UnicodeDecodeError` on repo 1, and because
    nothing between the loop and the enrichers caught it, the other 19 were never
    reached. The decode itself is fixed separately; this pins the containment, so
    the next unexpected per-repo failure costs one repo instead of the fleet.
    """
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG.format(store=store_dir.as_posix()))

    workspace = tmp_path / "ws"
    for name in ("alpha", "beta"):
        r = workspace / name
        r.mkdir(parents=True)
        subprocess.run(["git", "-C", str(r), "init", "-q"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "config", "user.email", "t@example.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "config", "user.name", "T"],
                       check=True, capture_output=True)
        (r / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "PROJ-1 init"],
                       check=True, capture_output=True)

    def flaky(path, pattern, **kw):
        if path.endswith("alpha"):
            raise UnicodeDecodeError("utf-8", b"\x96", 0, 1, "invalid start byte")
        return ["PROJ-1"]

    monkeypatch.setattr(orch, "build_atlassian", lambda src: _Stub())
    monkeypatch.setattr(refs, "extract_issue_keys", flaky)
    monkeypatch.setattr(refs, "scrape_links", lambda path, patterns, **k: [])

    args = Namespace(config=str(cfg), workspace=str(workspace), source=None, repo=None)
    # Non-zero, because a skipped repository leaves the graph incomplete...
    assert cmd_connect(args) == 1
    # ...but "beta" was still enriched, which is the whole point.
    assert "1 of 2 repo(s) failed" in gls_logs.text

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        check_schema(store)
        assert store.nodes_by_name("PROJ-1")
    finally:
        store.close()
