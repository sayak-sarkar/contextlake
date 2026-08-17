"""`kb wiki` produces a real page with no LLM configured.

Before this, the command printed "LLM tier disabled" and wrote nothing, so a user who had
not set up a backend got no wiki at all from a tool whose identity is local-first. The
structural page needs no model, so it is written on every run, and the generated page is
what an LLM adds ON TOP.

Driven through `cmd_wiki` rather than by calling the stage directly, because the property
under test is entirely about the command's control flow: the early return happened BEFORE
any page was written, and a test that called the stage would pass for the whole time that
return was in the way.
"""

from __future__ import annotations

import logging
from argparse import Namespace
from datetime import date

from contextlake.kb.cmds.wiki import cmd_wiki
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

# No [llm] table at all: the state a user is in before they configure anything.
_CFG_NO_LLM = '[kb]\nstore_dir = "{store}"\n'
_CFG_WITH_LLM = '[kb]\nstore_dir = "{store}"\n\n[llm]\nenabled = true\nprovider = "ollama"\n'


def _setup(tmp_path, cfg=_CFG_NO_LLM):
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    (tmp_path / "kb.toml").write_text(cfg.format(store=store_dir.as_posix()),
                                      encoding="utf-8")
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "r")))
    store.close()
    nodes = [
        Node(id="svc", repo="r", kind="class", name="CatalogService", file="svc.py"),
        Node(id="main", repo="r", kind="entry_point", name="main", file="cmd/main.go"),
        Node(id="charge", repo="r", kind="function", name="charge", file="svc.py"),
    ]
    edges = [Edge(src="charge", dst="svc", relation="calls",
                  confidence=Confidence.EXTRACTED,
                  provenance=Provenance(source_file="svc.py", source_line=1,
                                        verified_at=date(2026, 6, 21)))]
    write_shard(store_dir, GraphShard(repo="r", head_commit="abc123",
                                      nodes=nodes, edges=edges))
    return store_dir


def _structural(store_dir):
    return store_dir / "wiki" / "_structure" / "r.md"


def test_a_page_is_written_with_no_llm_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    page = _structural(store_dir)
    assert page.exists(), "no structural page was written without an LLM configured"
    text = page.read_text(encoding="utf-8")
    assert text.startswith("# r")
    assert "`main`" in text, "the entry point did not reach the page"


def test_the_run_says_the_structural_pages_are_complete_on_their_own(tmp_path,
                                                                    monkeypatch, caplog):
    """The message is as much the deliverable here as the file is.

    "LLM tier disabled" told a user their run had failed to do anything. It now does
    something, and the line has to say so, or somebody deletes the store and tries again.

    `caplog` on the project's own logger, not `capsys`: this command logs, so stdout is
    empty and a capsys assertion passes or fails for reasons unrelated to the message.
    The non-empty check below is there because a capture that sees nothing makes every
    `in` assertion after it vacuous.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    _setup(tmp_path)
    with caplog.at_level(logging.INFO, logger="contextlake"):
        assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    out = caplog.text
    assert out.strip(), "the capture saw nothing, so the assertions below prove nothing"
    assert "structural page" in out, out
    assert "need no model" in out, out


def test_the_exit_code_stays_zero(tmp_path, monkeypatch):
    """It was already 0 for "no LLM", and it must stay 0 now that the run does real work:
    a scheduled `kb wiki` on a local-first store is a success, not a skipped step."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0


def test_the_page_is_rewritten_on_a_second_run_without_force(tmp_path, monkeypatch):
    """Structural pages are deterministic and cost milliseconds, so they are not behind
    the freshness machinery, which exists to avoid paying an LLM twice. A page that went
    stale because nobody passed --force would be the worse failure."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    _structural(store_dir).write_text("# stale\n", encoding="utf-8")
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert "stale" not in _structural(store_dir).read_text(encoding="utf-8")


def test_the_structural_page_lands_in_its_own_directory(tmp_path, monkeypatch):
    """`wiki/_structure/`, never `wiki/`. A structural page and a generated page for one
    repository would otherwise collide on `wiki/<slug>.md`, and the generated one would
    win or lose depending on which ran last."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert not (store_dir / "wiki" / "r.md").exists()
    assert _structural(store_dir).exists()
