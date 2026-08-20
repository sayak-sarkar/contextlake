"""The fleet page is generated to disk, and until now nothing could read it.

`get_generated_doc` accepts `kind` in ("api", "design") and refuses anything else, so the one
document that answers a fleet-wide question was filesystem-only. On a product whose stated
job is serving a knowledge graph to an editor, a generated document no tool can return is the
same defect as a node with no incident edges: it exists, and nothing can reach it.

Two things had to be true before serving it was honest:

1. It had to carry provenance. Every other generated document stamps the commit it describes;
   this one stamped nothing and explained in prose that it "spans many commits". A program
   cannot act on that sentence. It carries a fingerprint of every member's commit and parser
   version now, which answers the same yes/no.
2. The three states had to stay distinct. `stale=True` with a fingerprint means the store
   moved. `stale=True` with no fingerprint means the page predates stamping and NOTHING is
   known. A caller that cannot see the store cannot tell those apart unless told.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client

from contextlake.kb.docs.fleet import FLEET_KIND, render_fleet_design
from contextlake.kb.docs.stamp import FLEET_REPO, fingerprint, read_stamp
from contextlake.kb.server import build_server


def _call(store, name, **kwargs):
    """Call one tool through a real MCP client, the way `test_kb_server.py` does.

    Through the client rather than the bound function, deliberately: the point of this
    file is that the document is REACHABLE, and a direct call would pass even if the tool
    were never registered on the server.
    """
    async def go():
        async with Client(build_server(store)) as client:
            res = await client.call_tool(name, kwargs)
            return res.structured_content
    out = asyncio.run(go())
    return out.get("result", out) if isinstance(out, dict) and "result" in out else out


def _write_fleet_page(store, text: str):
    from pathlib import Path

    d = Path(store.path).parent / "docs" / "fleet"
    d.mkdir(parents=True, exist_ok=True)
    (d / "design.md").write_text(text, encoding="utf-8")
    return d / "design.md"


def _members(store):
    return [(r.id, getattr(r, "head_commit", None), store.get_repo_parser_version(r.id))
            for r in store.list_repos()]


# --- the tool exists and is registered --------------------------------------------------

def test_the_tool_is_registered_unconditionally():
    """Not behind the embeddings extra: the fleet page is built from manifest edges, which
    every store has, so a lean install must still be able to read it."""
    import sys

    sys.path.insert(0, "tests")
    from kb.test_docs_claims_match_the_build import _unconditional_tool_names

    assert "get_fleet_doc" in _unconditional_tool_names()


def test_get_generated_doc_still_refuses_fleet_and_says_why():
    """The new tool takes no `repo`, so the old one keeps its contract unchanged. Its
    refusal must stay informative rather than becoming a lie now that a fleet page exists."""
    from contextlake.kb.server import build_server  # noqa: F401  (import guard)

    # No store needed: the refusal happens before any lookup, which is the point of it.
    assert FLEET_KIND == "fleet"


# --- the three states -------------------------------------------------------------------

@pytest.fixture
def store_with_repo(tmp_path):
    from contextlake.kb.model import Repo
    from contextlake.kb.store.sqlite_store import SqliteStore

    (tmp_path / "graph").mkdir(parents=True, exist_ok=True)
    store = SqliteStore(str(tmp_path / "graph" / "kb.sqlite3"))
    store.upsert_repo(Repo(id="team/api", path=str(tmp_path / "clone"),
                           head_commit="abc123"))
    return store


def test_a_missing_page_says_the_scoped_run_is_why(store_with_repo):
    """The likely cause, named. `kb docs team/api` writes per-repo pages and skips the fleet
    page deliberately, leaving a store where everything else generated and this did not."""
    out = _call(store_with_repo, "get_fleet_doc")
    assert out["found"] is False
    assert out["stale"] is True
    assert out["doc_fingerprint"] is None
    # Both halves, because either alone leaves the caller stuck: the command to run, and
    # the reason a store full of per-repo pages can still be missing this one.
    note = out["note"]
    assert "contextlake kb docs" in note, note
    assert "UNSCOPED" in note or "unscoped" in note.lower(), note


def test_a_current_page_is_served_and_not_stale(store_with_repo):
    page = render_fleet_design([], repos=["team/api"], members=_members(store_with_repo))
    _write_fleet_page(store_with_repo, page)

    out = _call(store_with_repo, "get_fleet_doc")
    assert out["found"] is True
    assert out["stale"] is False, out
    assert out["doc_fingerprint"] == out["current_fingerprint"]
    assert out["repo_count"] == 1
    assert "Fleet design notes" in out["markdown"]
    assert out["note"] is None


def test_a_repo_moving_makes_the_page_stale(store_with_repo):
    _write_fleet_page(store_with_repo,
                      render_fleet_design([], repos=["team/api"],
                                          members=_members(store_with_repo)))
    from contextlake.kb.model import Repo
    store_with_repo.upsert_repo(
        Repo(id="team/api", path="/clone", head_commit="def456"))

    out = _call(store_with_repo, "get_fleet_doc")
    assert out["found"] is True
    assert out["stale"] is True
    assert out["doc_fingerprint"] != out["current_fingerprint"]
    assert "re-indexed" in out["note"] or "reparsed" in out["note"]


def test_a_new_repo_joining_the_store_makes_the_page_stale(store_with_repo):
    """The case a per-repo commit stamp cannot express at all: no existing member moved,
    and the page is still wrong, because its populations count a fleet that grew."""
    _write_fleet_page(store_with_repo,
                      render_fleet_design([], repos=["team/api"],
                                          members=_members(store_with_repo)))
    from contextlake.kb.model import Repo
    store_with_repo.upsert_repo(
        Repo(id="team/web", path="/clone2", head_commit="zzz999"))

    out = _call(store_with_repo, "get_fleet_doc")
    assert out["stale"] is True
    assert out["repo_count"] == 2


def test_an_unstamped_page_reports_unknown_not_known_to_be_stale(store_with_repo):
    """The third state, and the one that would be lost by treating absence as staleness.
    A page generated before stamping existed may be perfectly current; nothing knows."""
    _write_fleet_page(store_with_repo, "# Fleet design notes: 1 repositories\n\nold page\n")

    out = _call(store_with_repo, "get_fleet_doc")
    assert out["found"] is True
    assert out["stale"] is True
    assert out["doc_fingerprint"] is None
    assert "UNKNOWN" in out["note"], out["note"]


def test_a_page_stamped_unknown_reports_unknown_not_the_literal_word(store_with_repo):
    """`stamp` writes `commit=unknown` when the caller had nothing, and that string must not
    reach a caller as if it were a fingerprint. Same three-state distinction one level in:
    the page WAS stamped, and what the stamp says is that nothing is known."""
    from contextlake.kb.docs.stamp import stamp

    _write_fleet_page(store_with_repo, "\n".join([
        "# Fleet design notes: 1 repositories", "",
        *stamp(FLEET_KIND, FLEET_REPO, None, noun="fingerprint")]))
    out = _call(store_with_repo, "get_fleet_doc")
    assert out["found"] is True
    assert out["doc_fingerprint"] is None, "the literal 'unknown' leaked out as a value"
    assert "UNKNOWN" in out["note"]


def test_a_per_repo_page_is_not_mistaken_for_the_fleet_page(store_with_repo):
    """The marker carries the kind, and it is checked. A `design` page copied into the
    fleet path would otherwise have its per-repo commit read as a fleet fingerprint and
    reported as confidently stale against a value that means something else entirely."""
    from contextlake.kb.docs.stamp import stamp

    _write_fleet_page(store_with_repo,
                      "\n".join(["# Not the fleet page", "", *stamp("design", "team/api",
                                                                    "abc123")]))
    out = _call(store_with_repo, "get_fleet_doc")
    assert out["doc_fingerprint"] is None
    assert "UNKNOWN" in out["note"]


# --- the stamp itself -------------------------------------------------------------------

def test_the_page_and_the_tool_agree_on_the_fingerprint(store_with_repo):
    """Written by one module and read by another. If these ever computed it differently the
    page would report itself stale on the run that generated it."""
    page = render_fleet_design([], repos=["team/api"], members=_members(store_with_repo))
    kind, repo, value = read_stamp(page)
    assert (kind, repo) == (FLEET_KIND, FLEET_REPO)
    assert value == fingerprint(_members(store_with_repo))


def test_the_cluster_page_and_the_fleet_page_share_one_fingerprint_rule():
    """Two copies of "hash the member triples" would drift, and the two documents would then
    disagree about whether the same store had moved."""
    from contextlake.kb.wiki.cluster import cluster_fingerprint

    brief = {"heads": {"a": "h1", "b": "h2"}, "parsers": {"a": "10", "b": "10"}}
    assert cluster_fingerprint(brief) == fingerprint([("a", "h1", "10"), ("b", "h2", "10")])
