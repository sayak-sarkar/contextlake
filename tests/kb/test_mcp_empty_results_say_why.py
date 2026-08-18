"""An empty MCP result must say which of two opposite things happened.

The caller of these tools is an agent that cannot see the store. `get_neighbors` returned
byte-for-byte identical output -- `{"edges": [], "total": 0, "truncated": false}` -- for a
real node with genuinely zero edges and for a node id that was never indexed, so the agent
reports "this symbol has no relationships" when the truth is "no such symbol". Nineteen
sibling tools already resolve exactly this with a note field; three did not.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from datetime import date

import pytest

from contextlake.kb.model import (
    Confidence,
    Edge,
    Node,
    Provenance,
)
from contextlake.kb.server import build_server
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore


@pytest.fixture
def server(tmp_path):
    store = SqliteStore(tmp_path / "index.sqlite")
    check_schema(store)
    store.upsert_nodes("svc-billing", [
        Node(id="svc-billing::charge_card", repo="svc-billing", kind="function",
             name="charge_card", file="billing.py", line_start=1),
        Node(id="svc-billing::submit_payment", repo="svc-billing", kind="function",
             name="submit_payment", file="billing.py", line_start=5),
        # An island: real, indexed, and genuinely without edges.
        Node(id="svc-billing::Calculator", repo="svc-billing", kind="class",
             name="Calculator", file="calc.py", line_start=1),
    ])
    store.upsert_edges("svc-billing", [
        Edge(src="svc-billing::charge_card", dst="svc-billing::submit_payment",
             relation="calls", confidence=Confidence.EXTRACTED,
             provenance=Provenance(source_file="billing.py", source_line=2,
                                   verified_at=date(2026, 8, 18)))])
    srv = build_server(store)
    with closing(store):
        yield srv


def _call(server, tool, args):
    """The payload, with the same unwrapping `test_server_contract.py` uses.

    The SDK hands back a `CallToolResult`; a first version of this file indexed it
    directly and every test failed with a TypeError that said nothing about the behaviour
    under test.
    """
    res = asyncio.run(server.call_tool(tool, args))
    assert not res.is_error, (
        f"{tool}({args}) answered with an error: "
        f"{res.content[0].text if res.content else '<no content>'}")
    structured = res.structured_content
    if isinstance(structured, str):
        structured = json.loads(structured)
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


def test_a_node_that_is_not_in_the_graph_says_so(server):
    out = _call(server, "get_neighbors", {"node_id": "svc-billing::no_such_symbol"})
    assert out["edges"] == [] and out["total"] == 0
    assert out["note"], "an unresolved id must not look like a node without edges"
    assert "no_such_symbol" in out["note"]


def test_a_real_node_with_no_edges_is_not_reported_as_missing(server):
    """The other direction, and the one a careless guard breaks: this node IS indexed."""
    out = _call(server, "get_neighbors", {"node_id": "svc-billing::Calculator"})
    assert out["edges"] == [] and out["total"] == 0
    assert out["note"] is None, (
        "a real node with genuinely no edges must not be reported as unknown")


def test_a_filter_that_excluded_everything_says_that_instead(server):
    out = _call(server, "get_neighbors",
                {"node_id": "svc-billing::charge_card", "relation": "inherits"})
    assert out["edges"] == []
    assert out["note"] and "inherits" in out["note"]


def test_a_node_with_edges_still_returns_them_and_no_note(server):
    out = _call(server, "get_neighbors", {"node_id": "svc-billing::charge_card"})
    assert out["total"] == 1
    assert out["note"] is None


def test_find_definition_distinguishes_absent_from_filtered_out(server):
    absent = _call(server, "find_definition", {"name": "no_such_name"})
    assert absent["nodes"] == [] and absent["note"]
    assert "no_such_name" in absent["note"]

    filtered = _call(server, "find_definition", {"name": "charge_card", "kind": "class"})
    assert filtered["nodes"] == []
    assert "IS defined" in filtered["note"], (
        "the symbol exists; saying only 'not found' would be the reassuring misreading")


def test_find_definition_still_answers_a_real_lookup(server):
    out = _call(server, "find_definition", {"name": "charge_card"})
    assert out["total"] == 1
    assert out["note"] is None


def test_search_code_reports_a_total_and_why_it_is_empty(server):
    empty = _call(server, "search_code", {"query": "zzz_nothing_like_this"})
    assert empty["nodes"] == [] and empty["total"] == 0
    assert empty["note"], "an empty search must say whether the term is indexed at all"

    found = _call(server, "search_code", {"query": "charge_card"})
    assert found["total"] >= 1
    assert found["truncated"] is False


def test_search_code_reports_truncation(server):
    """A capped list with no total is the same defect one step along."""
    out = _call(server, "search_code", {"query": "payment", "limit": 1})
    assert out["total"] >= 0
    assert isinstance(out["truncated"], bool)


def test_the_search_total_is_a_true_count_not_one_more_than_asked_for(tmp_path):
    """`total` must mean the same thing here as on every sibling.

    Setting it from a `limit + 1` fetch is enough to compute `truncated`, and it would make
    one field carry two meanings across the tool set -- which is the shape this whole batch
    exists to remove. Counted to a bound instead, with the note saying so at the bound.
    """
    from contextlake.kb.server import _SEARCH_TOTAL_CEILING

    store = SqliteStore(tmp_path / "index.sqlite")
    check_schema(store)
    store.upsert_nodes("svc-billing", [
        Node(id=f"svc-billing::charge_{i}", repo="svc-billing", kind="function",
             name=f"charge_{i}", file="billing.py", line_start=i)
        for i in range(20)])
    srv = build_server(store)
    with closing(store):
        out = _call(srv, "search_code", {"query": "charge", "limit": 5})
        assert len(out["nodes"]) == 5
        assert out["total"] == 20, "the total is what matched, not what was returned"
        assert out["truncated"] is True
        assert out["note"] is None, "20 is below the ceiling, so the total is exact"
        assert _SEARCH_TOTAL_CEILING > 20, (
            "this fixture only tests the exact-total path while it stays under the ceiling")


def test_a_zero_limit_still_reports_what_exists(tmp_path):
    """Returning nothing must not claim nothing exists -- the clamp for a negative limit
    lands here, and that is the reassuring misreading it would otherwise produce."""
    store = SqliteStore(tmp_path / "index.sqlite")
    check_schema(store)
    store.upsert_nodes("svc-billing", [
        Node(id=f"svc-billing::charge_{i}", repo="svc-billing", kind="function",
             name=f"charge_{i}", file="billing.py", line_start=i)
        for i in range(4)])
    srv = build_server(store)
    with closing(store):
        out = _call(srv, "search_code", {"query": "charge", "limit": -1})
        assert out["nodes"] == []
        assert out["total"] == 4 and out["truncated"] is True
