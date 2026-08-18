"""The one contract the MCP server owes an agent: a recoverable condition never errors.

Every tool in ``kb/server.py`` already answers a miss with a success-shaped response
carrying its own disclosure -- ``found=False``, a ``note``, a ``ranking_gap``, a ``gap``,
``answered=False``, ``indexed=False`` -- and `raise` appears only where an argument is not
a smaller question but no question at all. Nothing tested that, so it was an *emergent*
property: true today, true by the accumulated habit of the people who wrote each handler,
and one refactor away from being false in exactly one tool without a single test going red.

Why it is worth a file of its own. An agent does not read an error result as "that
particular lookup missed"; it reads it as "this tool is broken" and stops calling the
server for the rest of the session. So the cost of one handler regressing to a raise is
not one bad answer, it is every answer the agent would have got from the graph afterwards,
replaced by grepping. A negative that arrives as data ("no repository with this id is
indexed") keeps the conversation going; the same negative that arrives as a transport
error ends it.

Two layers, and both are load-bearing:

* the **sweep** enumerates the live tool list over the wire and calls every tool with
  miss-shaped arguments, so a tool added next year is covered on the day it is added
  rather than the day somebody remembers this file exists;
* the **targeted** cases assert the SHAPE of each named negative, because "it did not
  error" says nothing about whether the response actually says *not found* -- an empty
  list with no disclosure passes the sweep and is the very defect the disclosure fields
  in ``server.py`` were added to fix.

And the near-miss at the bottom is what stops the sweep from being a tautology: a test
that would still pass if every tool raised, or if no tool ever refused anything, measures
nothing. It runs through the same ``_call`` helper as the sweep, with the assertion
inverted, so the two are visibly the same measurement pointed in opposite directions.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import date

import pytest
from mcp import Client

from contextlake.kb import server as server_mod
from contextlake.kb.embeddings.store import VectorStore
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore

# --- fixtures: synthetic throughout -----------------------------------------

REPO = "team/api"

# Chosen to be absent from any store this file builds, and absent in a way the graph can
# PROVE rather than merely fail to find: a name no fixture contains, and a node id that is
# well-formed (the `file::symbol` shape the ids module produces) so nothing is being
# rejected for its syntax.
ABSENT_NAME = "FrobnicateTheWidget"
ABSENT_ID = "src/nowhere.py::FrobnicateTheWidget"
ABSENT_REPO = "team/never-indexed"


class _FakeEmbedder:
    """Matches everything perfectly, which is the hard case for a miss.

    A nearest-neighbour index has no notion of "no match" -- it ranks its top k however
    far away they are. An embedder that scores every node at cosine 1.0 removes any
    chance that a retrieval tool comes back empty by luck, so when it still returns
    nothing the relevance floor is what returned nothing.
    """

    name = "fake"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _seed(store, *, repo_path: str | None = None) -> None:
    """Two symbols and one call edge, optionally with a repo row.

    ``repo_path`` is the local clone recorded for the repo: ``None`` registers no repo at
    all (an unindexed fleet), ``""`` registers one with no clone on record, and a path
    that does not exist registers one whose clone has gone missing. Those three are
    different facts about the same question and the handlers distinguish them, so the
    fixture has to be able to produce all three.
    """
    if repo_path is not None:
        store.upsert_repo(Repo(id=REPO, path=repo_path, head_commit="0" * 40))
    store.upsert_nodes(REPO, [
        Node(id="a", repo=REPO, kind="function", name="CatalogService", file="svc.py"),
        Node(id="b", repo=REPO, kind="function", name="charge", file="svc.py"),
    ])
    store.upsert_edges(REPO, [Edge(
        src="a", dst="b", relation="calls", confidence=Confidence.EXTRACTED,
        provenance=Provenance(source_file="svc.py", source_line=5,
                              verified_at=date(2026, 6, 21)),
    )])


def _build(tmp_path, *, seed: bool = True, repo_path: str | None = None):
    """A server over a fresh store, with embeddings configured.

    Embeddings are always wired so ``semantic_search`` and ``hybrid_search`` are in the
    tool list the sweep enumerates: they are the two tools that register conditionally,
    which makes them the two most likely to be left out of a hand-written list.
    """
    store = SqliteStore(tmp_path / "kb.sqlite")
    vectors = VectorStore(tmp_path / "embeddings.sqlite")
    if seed:
        _seed(store, repo_path=repo_path)
        vectors.upsert([("a", REPO, [1.0, 0.0]), ("b", REPO, [1.0, 0.0])])
    return build_server(store, embedder=_FakeEmbedder(), vector_store=vectors), store, vectors


# --- the one measurement ----------------------------------------------------

def _call(server, tool: str, args: dict):
    """Call a tool over the in-memory MCP transport and return the raw result.

    Deliberately returns the result rather than asserting on it. `raise` inside a handler
    reaches a client as ``is_error=True`` with the exception text as content, so
    ``is_error`` is the *observable* form of the invariant -- what an agent sees, which is
    the thing the guarantee is about. Both the sweep and the near-miss go through here so
    the pass and the refusal are the same measurement.
    """
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(go())


def _tools(server):
    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    return asyncio.run(go())


def _unwrap(structured):
    """MCPServer wraps non-object returns (lists, Optionals) under a 'result' key."""
    if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
        return structured["result"]
    return structured


def _ok(server, tool: str, args: dict):
    """Call a tool, assert it did not error, and hand back the payload."""
    res = _call(server, tool, args)
    assert not res.is_error, (
        f"{tool}({args}) answered with an error result: "
        f"{res.content[0].text if res.content else '<no content>'}")
    return _unwrap(res.structured_content)


# --- the sweep --------------------------------------------------------------

# One miss-shaped value per argument name the tools use. Keyed by name rather than by tool
# because the point is to cover tools this file has never heard of.
MISS_ARGS = {
    "repo": ABSENT_REPO,
    "node_id": ABSENT_ID,
    "src_id": ABSENT_ID,
    "dst_id": "src/nowhere.py::AlsoAbsent",
    "name": ABSENT_NAME,
    "package": "no-such-package",
    "query": ABSENT_NAME,
    "question": f"where is {ABSENT_NAME} defined?",
}

# The tool list as it stands, asserted as a SUBSET of what the sweep saw. Without it a
# sweep over an empty tool list -- a build_server that registered nothing, a transport
# that answered no tools -- would pass by iterating zero times, which is the classic way
# a universal assertion becomes free.
EXPECTED_TOOLS = frozenset({
    "graph_stats", "who_knows", "get_node", "get_neighbors", "search_code",
    "find_definition", "find_callers", "find_callees", "find_dependents",
    "repo_dependencies", "repo_flow", "repo_event_flow", "blast_radius", "get_wiki",
    "get_generated_doc",
    "get_readme", "get_repo_brief", "list_repos", "get_repo_links", "graph_health",
    "shortest_path", "semantic_search", "hybrid_search", "ask",
})


def _miss_args(tool) -> dict:
    """Miss-shaped arguments for one advertised tool, or a failure naming the gap.

    A tool whose required argument has no entry in ``MISS_ARGS`` would otherwise be called
    with ``{}``: either the SDK refuses it against the schema and the sweep fails for a
    reason that has nothing to do with this invariant, or every argument is optional and
    the tool answers a hit-shaped success that passes trivially. Both are silent losses of
    coverage, so a new argument name is an explicit instruction instead.
    """
    schema = tool.input_schema or {}
    props = schema.get("properties") or {}
    args = {p: MISS_ARGS[p] for p in props if p in MISS_ARGS}
    unfilled = [p for p in (schema.get("required") or []) if p not in args]
    assert not unfilled, (
        f"{tool.name} requires {unfilled}, for which this test has no miss-shaped value: "
        "add one to MISS_ARGS so the sweep keeps covering every tool")
    return args


@pytest.mark.parametrize("shape,kwargs", [
    # An empty store: nothing indexed at all, the state a freshly-created knowledge base
    # is in and the one an agent is most likely to meet on its first call.
    ("empty-store", {"seed": False}),
    # A populated store asked about things it does not hold: the ordinary miss.
    ("indexed-fleet", {"repo_path": None}),
    # Indexed, but the local clone the graph was built from is gone -- the tools that
    # reach the filesystem (who_knows, get_readme, graph_health) run and find nothing.
    ("clone-missing", {"repo_path": "/nonexistent/clone/path"}),
])
def test_no_tool_errors_for_a_recoverable_condition(tmp_path, shape, kwargs):
    """The invariant, over every tool the server actually advertises.

    Not a hand-written list of calls: the tool list is read from the wire, so this covers
    tools added after this test was written. Every one of them is asked about something
    that is not there, and every one of them must answer.
    """
    server, store, vectors = _build(tmp_path, **kwargs)
    with closing(store), closing(vectors):
        advertised = _tools(server)
        names = {t.name for t in advertised}
        assert EXPECTED_TOOLS <= names, (
            f"tools missing from the {shape} server: {sorted(EXPECTED_TOOLS - names)}")

        for tool in advertised:
            args = _miss_args(tool)
            res = _call(server, tool.name, args)
            assert not res.is_error, (
                f"[{shape}] {tool.name}({args}) answered with an ERROR result instead of "
                "a not-found response: "
                f"{res.content[0].text if res.content else '<no content>'}")


# --- the shapes: a miss has to SAY it missed --------------------------------

def test_an_absent_symbol_is_reported_as_absent_not_as_unaffected(tmp_path):
    """A symbol the graph has never seen.

    The disclosure matters more here than anywhere else, because the empty answer is the
    reassuring one: "nothing calls this" and "nothing depends on this, safe to change"
    are what an undisclosed miss reads as. (`test_kb_server.py` pins the same two notes
    for `find_callers`/`blast_radius`; this case exists to hold the whole family to it.)
    """
    server, store, vectors = _build(tmp_path)
    with closing(store), closing(vectors):
        for tool in ("find_callers", "find_callees", "blast_radius"):
            out = _ok(server, tool, {"name": ABSENT_NAME})
            assert out["total"] == 0
            assert out["note"] == f"No indexed symbol named {ABSENT_NAME!r}.", tool

        # blast_radius must not echo the unresolved string back as a seed: a seeded
        # impact analysis of a symbol that does not exist is a well-formed lie.
        assert _ok(server, "blast_radius", {"name": ABSENT_NAME})["seed"] == ""

        # find_definition and search_code used to return bare lists, on the reasoning that
        # "no definition with this name" is what an empty result already means, so there
        # was nothing a note could add. That reasoning was wrong in the case it did not
        # consider: with a `kind` or `repo` filter, an empty result reads as "X is not
        # defined" when the truth is "X is defined, and your filter excluded it". They
        # carry the same envelope as every sibling now.
        definition = _ok(server, "find_definition", {"name": ABSENT_NAME})
        assert definition["nodes"] == [] and definition["total"] == 0
        assert ABSENT_NAME in definition["note"]

        search = _ok(server, "search_code", {"query": ABSENT_NAME})
        assert search["nodes"] == [] and search["total"] == 0
        assert search["note"], "an empty search must say why it is empty"

        answer = _ok(server, "ask", {"question": f"where is {ABSENT_NAME} defined?"})
        assert answer["answered"] is False
        assert ABSENT_NAME in answer["note"]


@pytest.mark.parametrize("route,question", [
    ("callers", f"who calls {ABSENT_NAME}?"),
    ("subclasses", f"what extends {ABSENT_NAME}?"),
    ("impact", f"what breaks if I change {ABSENT_NAME}?"),
    ("owners", f"who owns {ABSENT_REPO}?"),
])
def test_ask_labels_an_established_negative_as_unanswered(tmp_path, route, question):
    """``answered`` must agree with the prose beside it, on every route.

    Found by the empty-store sweep above and fixed in ``server.py``: these four routes
    left ``answered`` at its ``True`` default when they had established that no such
    symbol or repo is indexed, so the miss existed only in the ``note``. That field is
    there precisely so an agent does not have to parse the prose to know whether it got
    an answer, and a stated non-answer labelled ``answered=true`` is worse than no
    label at all.

    Parametrized per route rather than asserted through one question, because a single
    incidental question is what let three of the four go unnoticed in the first place.
    """
    server, store, vectors = _build(tmp_path)
    with closing(store), closing(vectors):
        out = _ok(server, "ask", {"question": question})
        assert out["route"] == route
        assert out["answered"] is False, (
            f"the {route} route labelled an established negative as answered: {out['note']}")
        assert out["note"].startswith("Couldn't")
        # Nothing is carried alongside that could be mistaken for a partial answer.
        assert out["nodes"] == [] and out["blast"] is None and out["owners"] is None


def test_a_repo_that_was_never_indexed_answers_found_false(tmp_path):
    """A repo id nothing in the store knows.

    Every one of these tools used to return an empty result indistinguishable from a real
    architectural fact: a mistyped repo id read as "this repo depends on nothing / has no
    HTTP flow / has no external links / has no owners". ``found=False`` is what separates
    "I have never heard of this repo" from "I looked, and the answer is none".
    """
    server, store, vectors = _build(tmp_path)
    with closing(store), closing(vectors):
        for tool in ("repo_dependencies", "repo_flow", "repo_event_flow",
                     "get_repo_links", "get_wiki", "get_readme", "get_repo_brief"):
            assert _ok(server, tool, {"repo": ABSENT_REPO})["found"] is False, tool

        owners = _ok(server, "who_knows", {"repo": ABSENT_REPO})
        assert owners["found"] is False
        assert owners["owners"] == []
        # Not just false, but WHY: an empty ranking with no gap statement is one a caller
        # can narrate as a completed ranking that found nobody.
        assert "indexed" in owners["ranking_gap"]


@pytest.mark.parametrize("repo_path,expected_gap", [
    # No clone was ever recorded, so no git command ran.
    ("", "never read"),
    # A clone WAS recorded and its directory is gone. git runs and fails.
    ("/nonexistent/clone/path", None),
])
def test_a_repo_whose_clone_is_missing_still_answers(tmp_path, repo_path, expected_gap):
    """Indexed repo, unusable checkout: the filesystem-reading tools must degrade.

    Two sub-shapes, because they are two different facts and the handler branches on
    them. ``expected_gap`` is only pinned for the branch whose wording is derived from
    what actually happened; the missing-directory branch is asserted to carry *a* gap
    rather than a specific sentence, on purpose -- see AGENT-REPORT-W1.md, its current
    wording claims the history was read, which is not what happened.
    """
    server, store, vectors = _build(tmp_path, repo_path=repo_path)
    with closing(store), closing(vectors):
        owners = _ok(server, "who_knows", {"repo": REPO})
        # found=True is correct and is the distinction being kept: the REPO is indexed,
        # it is the clone that is unusable. Collapsing the two would make a typo and a
        # missing checkout the same answer.
        assert owners["found"] is True
        assert owners["owners"] == []
        assert owners["ranking_gap"], "an empty ranking with no gap statement is unreadable"
        if expected_gap:
            assert expected_gap in owners["ranking_gap"]

        # The README lives in the clone, so there is nothing to read.
        assert _ok(server, "get_readme", {"repo": REPO})["found"] is False

        # The graph itself is unaffected: a missing clone does not stop a graph walk.
        assert _ok(server, "find_callers", {"node_id": "b"})["total"] == 1

        # And the condition is reported where an operator would look for it, rather than
        # being folded into "stale, re-run index" -- which re-indexing could never clear.
        health = _ok(server, "graph_health", {})
        assert health["unreadable_repos"] == [REPO]


def test_an_empty_store_says_it_is_empty_rather_than_healthy(tmp_path):
    """Nothing indexed at all.

    Zero stale, zero dangling, zero everything is the exact output of a perfectly healthy
    fleet, and it was also the output of a store that has never been indexed. The counts
    were never wrong, they were unqualified -- so the answer that needs disclosing here is
    the *reassuring* one.
    """
    server, store, vectors = _build(tmp_path, seed=False)
    with closing(store), closing(vectors):
        health = _ok(server, "graph_health", {})
        assert health["indexed"] is False
        assert health["repos"] == 0 and health["stale"] == 0 and health["dangling"] == 0

        stats = _ok(server, "graph_stats", {})
        assert stats["nodes"] == 0 and stats["edges"] == 0

        repos = _ok(server, "list_repos", {})
        assert repos["total"] == 0 and repos["repos"] == []

        # `ask` over an empty store must not hand back the nearest k of nothing under a
        # note that reads like an answer.
        answer = _ok(server, "ask", {"question": f"who calls {ABSENT_NAME}?"})
        assert answer["answered"] is False and answer["nodes"] == []

        # Retrieval over an empty vector store: the floor, not a crash and not k hits.
        for tool in ("semantic_search", "hybrid_search"):
            assert _ok(server, tool, {"query": ABSENT_NAME})["nodes"] == [], tool


def test_a_well_formed_but_absent_node_id_answers_normally(tmp_path):
    """An id shaped exactly like a real one, naming a node the graph does not hold.

    This is the case that most tempts a lookup into raising -- the argument is not
    malformed, so there is nothing to reject; it simply is not there.
    """
    server, store, vectors = _build(tmp_path)
    with closing(store), closing(vectors):
        # get_node is the one tool whose miss is a null rather than a flagged envelope,
        # and that is fine: its return type is `NodeOut | None`, so null is unambiguous
        # and there is no reassuring misreading of it. Left as it is on purpose rather
        # than grown a `found` field for uniformity's sake.
        assert _ok(server, "get_node", {"node_id": ABSENT_ID}) is None

        neighbors = _ok(server, "get_neighbors", {"node_id": ABSENT_ID})
        assert neighbors["edges"] == [] and neighbors["total"] == 0
        assert neighbors["truncated"] is False

        # A path query has three outcomes and a bare node list rendered them
        # identically. `gap` says which of the two misses this is.
        path = _ok(server, "shortest_path",
                   {"src_id": ABSENT_ID, "dst_id": "src/nowhere.py::AlsoAbsent"})
        assert path["found"] is False and path["nodes"] == []
        assert "no indexed node with" in path["gap"]
        assert ABSENT_ID in path["gap"]

        # Two nodes that ARE both indexed, with no route between them, is the third
        # outcome and must not be confused with the first. Same empty `nodes`, different
        # `gap` -- which is the whole reason the field exists.
        store.upsert_nodes(REPO, [Node(id="island", repo=REPO, kind="function",
                                       name="island", file="other.py")])
        stranded = _ok(server, "shortest_path", {"src_id": "a", "dst_id": "island"})
        assert stranded["found"] is False
        assert "both nodes are indexed" in stranded["gap"]


# --- the near-miss: what is still an error, and why -------------------------

def test_a_nonsense_argument_still_errors(tmp_path):
    """The line, and the proof the sweep above is not vacuous.

    Same ``_call`` helper, assertion inverted. If the sweep were passing because every
    result comes back non-error whatever happens -- a harness that swallows failures, an
    ``is_error`` that is never set -- this test fails, because nothing here would be an
    error either.

    Where the line sits: an absent symbol, an unindexed repo, an empty store and a
    missing clone are all *questions with a real negative answer*, and the graph can
    state that answer, so they are data. ``hops=-1`` is not a smaller question about
    impact and ``direction="sideways"`` is not a narrower view of a repo's edges; neither
    is a question at all, and answering them emptily would hand back "nothing is
    affected" / "this repo has no dependencies", a positive claim manufactured from an
    argument the tool had in fact rejected. Refusing is the only reading that cannot be
    mistaken for a fact.

    ``hops`` is the primary case because the refusal is the handler's own `raise
    ValueError` (``server.py``, in ``blast_radius``): asserting its message proves the
    error came from contextlake and not from schema plumbing. `test_kb_server.py` pins
    each of these individually; here they are the boundary of the sweep.
    """
    server, store, vectors = _build(tmp_path)
    with closing(store), closing(vectors):
        # 1. A handler-level raise. hops=0 is a real request (look zero hops out) and
        #    still answers, so the line is at "below zero", not "not positive".
        bad_hops = _call(server, "blast_radius", {"node_id": "b", "hops": -1})
        assert bad_hops.is_error, "a negative traversal depth was answered instead of refused"
        assert "hops must be 0 or greater" in bad_hops.content[0].text
        assert _ok(server, "blast_radius", {"node_id": "b", "hops": 0})["total"] == 0

        # 2. An out-of-vocabulary direction, refused by the advertised schema before any
        #    handler runs. A different mechanism from the raise above and worth having
        #    both: the schema is what makes the refusal visible to the agent up front.
        bad_dir = _call(server, "get_neighbors", {"node_id": "a", "direction": "sideways"})
        assert bad_dir.is_error, "an invalid direction was answered instead of refused"

        # 3. And in-process, the shared filter really does `raise` -- so a caller that
        #    bypasses the schema cannot get a silent empty list where the store's own
        #    `neighbors` would have refused.
        with pytest.raises(ValueError, match="invalid direction: 'sideways'"):
            server_mod._repo_side([{"src": "a", "dst": "b"}], "a", "sideways")

        # The recoverable calls in the very same session still answer, which is the
        # asymmetry the whole file is about: refusing nonsense must not make the tool
        # look broken for the next question.
        assert _ok(server, "find_callers", {"name": ABSENT_NAME})["note"]
