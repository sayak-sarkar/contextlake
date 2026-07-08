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


def test_connect_persists_confirmed_links(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate ~/.gitlab-sync/kb.toml
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


def test_connect_returns_nonzero_when_all_sources_fail(tmp_path, monkeypatch):
    """Every source call failing (e.g. an unreachable connector) is a non-zero
    exit, not a silent 'Connect complete: 0 links'."""
    import contextlake.kb.commands as cmds

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_CONFIG.format(store=store_dir.as_posix()))
    repo = tmp_path / "app"
    repo.mkdir()

    def boom_enricher(repo_id, keys, links):
        raise RuntimeError("atlassian unreachable")

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
    finally:
        store.close()
