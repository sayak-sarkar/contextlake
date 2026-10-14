"""A page generated for an enriched repo records its connector items, on disk and in store.

The connector-access rule hides a wiki page from a key without connector access when the
page footer says its prompt carried connector items, and hides the page's `@wiki:`
sections when their `connector_text` attribute says so. An undercount on either is the one
way that design leaks, so this drives the real write path: `cmd_wiki` with a fake model,
one repo with enrichment documents and one without as the control.
"""

from __future__ import annotations

from argparse import Namespace

import contextlake.kb.llm as llm_pkg
from contextlake.kb.cmds.wiki import cmd_wiki
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_CFG = ('[kb]\nstore_dir = "{store}"\n\n[llm]\nenabled = true\nprovider = "ollama"\n\n'
        '[embeddings]\nenabled = false\n')


class _FakeLlm:
    """Passes the council and writes a draft built from the structural page."""

    name = "fake"

    def __init__(self):
        self.page_prompts: list[str] = []

    def generate(self, prompt, *, system=None):
        if "Review lens" in prompt:
            return '{"score": 0.95, "issues": []}'
        self.page_prompts.append(prompt)
        from wiki_doubles import sound_draft

        return sound_draft(prompt)


def _setup(tmp_path):
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    (tmp_path / "kb.toml").write_text(_CFG.format(store=store_dir.as_posix()),
                                      encoding="utf-8")
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    for rid in ("acme/api", "acme/web"):
        clone = tmp_path / rid.replace("/", "_")
        clone.mkdir()
        store.upsert_repo(Repo(id=rid, path=str(clone)))
        nodes = [Node(id=f"{rid}:fn{i}", repo=rid, kind="function", name=f"handle{i}",
                      file=f"app/f{i}.py") for i in range(6)]
        store.upsert_nodes(rid, nodes)
        write_shard(store_dir, GraphShard(repo=rid, head_commit="h1", nodes=nodes,
                                          edges=[]))
    store.close()
    part = "@enrich:acme/api"
    write_shard(store_dir, GraphShard(repo=part, head_commit="enrich", nodes=[
        Node(id=f"{part}:d{i}", repo=part, kind="document", name=f"Ticket {i}",
             file=f"https://tracker.invalid/{i}",
             attrs={"snippet": f"handle{i} retries twice", "source": "api"})
        for i in range(2)], edges=[]))
    return store_dir


def test_the_footer_and_the_stored_sections_record_the_connector_items(tmp_path,
                                                                        monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store_dir = _setup(tmp_path)
    llm = _FakeLlm()
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: llm)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert any("External context" in p for p in llm.page_prompts), (
        "control: the enriched repo's prompt carried its connector items")

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        for rid, count, flag in (("acme/api", 2, True), ("acme/web", 0, False)):
            page = (store_dir / "wiki" / f"{rid.replace('/', '__')}.md").read_text(
                encoding="utf-8")
            assert "Built from the knowledge graph with no language model" not in page, (
                f"{rid}: the generated page did not replace the structural one")
            assert page.rstrip().endswith(f"Connector items: {count}.*"), page[-200:]
            first = store.get_node(f"@wiki:{rid}:0")
            assert first is not None and first.attrs.get("connector_text") is flag
    finally:
        store.close()
