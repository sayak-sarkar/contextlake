"""End-to-end test for the `connect` command: config -> stubbed connector ->
reconciled external nodes/edges persisted in an isolated store partition."""

from argparse import Namespace

import contextlake.kb.connectors.orchestrate as orch
import contextlake.kb.references as refs
from contextlake.kb.commands import cmd_connect
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

    symbol = Node(id="sym-checkout", repo="group/app", kind="function", name="checkout",
                 file="billing.py", line_start=10,
                 attrs={"doc": "Handles checkout. See PROJ-1 for the refund edge case."})
    write_shard(store_dir, GraphShard(repo="group/app", head_commit="abc123", nodes=[symbol]))

    monkeypatch.setattr(orch, "build_atlassian", lambda src: _Stub())
    monkeypatch.setattr(refs, "extract_issue_keys", lambda path, pattern, **k: [])
    monkeypatch.setattr(refs, "scrape_links", lambda path, patterns, **k: [])

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 0

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        check_schema(store)
        ticket_edges = [e for e in store.neighbors("sym-checkout", direction="out")
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
    monkeypatch.setattr(cmds, "_build_enrichers", lambda sources: ([boom_enricher], ["site-a"]))
    monkeypatch.setattr(refs, "extract_issue_keys", lambda path, pattern, **k: ["PROJ-1"])
    monkeypatch.setattr(refs, "scrape_links", lambda path, patterns, **k: [])

    args = Namespace(config=str(cfg), workspace=None, source=str(repo), repo="group/app")
    assert cmd_connect(args) == 1


def test_partition_name():
    assert connect_partition("group/app") == "@connect:group/app"


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
