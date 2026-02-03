"""End-to-end test for the `connect` command: config -> stubbed connector ->
reconciled external nodes/edges persisted in an isolated store partition."""

from argparse import Namespace

import gitlab_sync.kb.connectors.orchestrate as orch
import gitlab_sync.kb.references as refs
from gitlab_sync.kb.commands import cmd_connect
from gitlab_sync.kb.connectors.orchestrate import connect_partition
from gitlab_sync.kb.state import check_schema
from gitlab_sync.kb.store.sqlite_store import SqliteStore

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
        assert designs[0].attrs.get("title") == "Design System"  # best-effort enrich applied
    finally:
        store.close()
