"""Tests for the Zendesk connector: URL classification, association, and the wiring.

The connector makes no network call, which is the one thing that makes it different from
every sibling here. There is no mock MCP to spawn and no verification pass to fake --
`test_the_connector_reaches_no_network_at_all` asserts that as behaviour rather than
leaving it as a claim in a docstring.
"""

import re

import pytest

from contextlake.kb.connectors.zendesk import (
    associate_tickets,
    classify_zendesk_link,
    subdomain_of,
    ticket_node,
    title_of,
)
from contextlake.kb.model import EXTERNAL_REPO

TICKET = "https://acme.zendesk.com/agent/tickets/4821"
API_TICKET = "https://acme.zendesk.com/api/v2/tickets/4821.json"
ARTICLE = "https://acme.zendesk.com/hc/en-us/articles/360001234567-How-to-rotate-a-key"


# --- classification ------------------------------------------------------------------

def test_a_ticket_url_classifies_to_an_issue():
    assert classify_zendesk_link(TICKET) == ("issue", "acme:ticket:4821")


def test_a_help_centre_url_classifies_to_a_document():
    assert classify_zendesk_link(ARTICLE) == ("document", "acme:article:360001234567")


def test_the_agent_url_and_the_api_url_are_the_same_ticket():
    """A runbook pastes the API URL and a human pastes the agent URL. They name one
    ticket, so they must not become two nodes that never meet."""
    assert classify_zendesk_link(TICKET) == classify_zendesk_link(API_TICKET)


def test_the_subdomain_is_part_of_the_key():
    """Ticket numbers restart at 1 per instance. Without the instance in the key, two
    companies' support histories merge into one node."""
    a = classify_zendesk_link("https://acme.zendesk.com/agent/tickets/1")
    b = classify_zendesk_link("https://globex.zendesk.com/agent/tickets/1")
    assert a != b
    assert ticket_node(*a).id != ticket_node(*b).id


def test_a_bare_host_names_no_instance_and_is_refused():
    """`zendesk.com/agent/tickets/1` points at nothing. Keying a node on the empty
    string would put a node in the graph that resolves nowhere."""
    assert subdomain_of("https://zendesk.com/agent/tickets/1") is None
    assert classify_zendesk_link("https://zendesk.com/agent/tickets/1") is None


def test_a_zendesk_url_that_names_neither_is_refused():
    assert classify_zendesk_link("https://acme.zendesk.com/hc/en-us/categories/200") is None


def test_an_article_title_comes_from_the_slug():
    assert title_of(ARTICLE) == "How to rotate a key"


def test_a_ticket_has_no_title_because_the_url_carries_none():
    """The subject lives behind the API. A node that invented one would be stating
    something the graph cannot support."""
    assert title_of(TICKET) is None
    node = ticket_node(*classify_zendesk_link(TICKET), url=TICKET, title=title_of(TICKET))
    assert "title" not in node.attrs


# --- association ---------------------------------------------------------------------

def test_association_emits_the_relation_each_kind_earns():
    nodes, edges = associate_tickets("team/app", links=[TICKET, ARTICLE])
    rel = {e.dst.split("_")[1]: e.relation for e in edges}
    assert rel["issue"] == "discussed_in"        # a ticket is a conversation
    assert rel["document"] == "documented_by"    # an article documents it
    assert all(n.repo == EXTERNAL_REPO for n in nodes if n.kind != "repo")


def test_association_ignores_urls_it_does_not_claim():
    nodes, edges = associate_tickets(
        "team/app", links=["https://example.com/tickets/1", "https://acme.slack.com/x"])
    assert edges == []
    assert [n.kind for n in nodes] == ["repo"], "only the repo node, nothing external"


def test_association_dedupes_the_same_ticket_linked_twice():
    _, edges = associate_tickets("team/app", links=[TICKET, TICKET, API_TICKET])
    assert len(edges) == 1, "one ticket, one edge, however many URLs name it"


def test_hosts_are_configurable_for_a_vanity_domain():
    """An instance served from support.example.com is still Zendesk. The default claims
    only *.zendesk.com, because claiming an arbitrary host would take links that belong
    to another connector."""
    url = "https://support.example.com/agent/tickets/9"
    assert associate_tickets("team/app", links=[url])[1] == []
    _, edges = associate_tickets("team/app", links=[url], site_hosts=("example.com",))
    assert len(edges) == 1


def test_the_connector_reaches_no_network_at_all():
    """The distinguishing property, asserted rather than documented. Every socket entry
    point raises; association must still produce the same graph."""
    import socket

    def _blocked(*a, **k):
        raise OSError("the Zendesk connector must not open a socket")

    saved = (socket.getaddrinfo, socket.create_connection, socket.socket)
    socket.getaddrinfo, socket.create_connection, socket.socket = (
        _blocked, _blocked, _blocked)
    try:
        nodes, edges = associate_tickets("team/app", links=[TICKET, ARTICLE])
    finally:
        socket.getaddrinfo, socket.create_connection, socket.socket = saved
    assert len(edges) == 2 and len(nodes) == 3


# --- wiring --------------------------------------------------------------------------

def test_the_link_pattern_finds_both_shapes_without_trailing_punctuation():
    """A markdown-linked or sentence-final URL must not drag `)` or `.` into the key."""
    from contextlake.kb.cmds.connect import _DEFAULT_LINK_PATTERNS

    found = re.findall(
        _DEFAULT_LINK_PATTERNS["zendesk.com"],
        f"see [t]({TICKET}) and {ARTICLE}.")
    assert found == [TICKET, ARTICLE]


def test_a_configured_zendesk_source_actually_produces_an_enricher():
    """A connector nothing routes to is unreachable however well it works.

    An earlier version of this test asserted the dispatch's SOURCE TEXT contained
    `s.type == "zendesk"`. It passed while the feature was unreachable: `cmd_connect`
    filters `cfg.sources` against its own hardcoded tuple of connector types before the
    dispatch ever runs, and "zendesk" was not in it. Reading the branch proves the branch
    exists, not that anything reaches it -- so this builds the enricher list instead.
    """
    from contextlake.kb.cmds.connect import _build_enrichers
    from contextlake.kb.config import SourceCfg

    src = SourceCfg(type="zendesk", name="support")
    enrichers, names = _build_enrichers([src], store=None)
    assert names == ["support"], "the zendesk branch produced no enricher"
    nodes, edges = enrichers[0]("team/app", {}, [TICKET], {})
    assert [e.relation for e in edges] == ["discussed_in"]


def test_the_connector_type_filter_admits_zendesk():
    """The gate the test above was blind to, pinned on its own. `cmd_connect` drops any
    source whose type is not in this tuple, so a type missing from it is silently
    inert however complete the connector is."""
    import inspect

    from contextlake.kb.cmds import connect as mod

    body = inspect.getsource(mod.cmd_connect)
    filter_lines = [ln for ln in body.splitlines() if "s.type in (" in ln]
    assert filter_lines, "the connector-type filter moved; this guard is now blind"
    # Asserted against the FILTER LINE only. The first version of this fell back to
    # `or "zendesk" in body`, and "zendesk" also appears in the guidance message two
    # lines below, so the guard passed with the type removed from the filter -- an
    # unanchored match on a question that needed an exact one.
    assert any('"zendesk"' in ln for ln in filter_lines), (
        "zendesk is not in cmd_connect's connector-type filter, so a configured "
        "zendesk source is dropped before the dispatch ever sees it")


def test_the_enricher_takes_hosts_not_a_connector():
    """Its siblings take a connector object holding an MCP endpoint. There is nothing to
    connect to here, and an empty connector would imply otherwise."""
    import inspect

    from contextlake.kb.connectors.orchestrate import enrich_repo_zendesk

    params = list(inspect.signature(enrich_repo_zendesk).parameters)
    assert params[0] == "hosts"


@pytest.mark.parametrize("url,expected", [
    ("https://acme.zendesk.com/agent/tickets/4821", ("issue", "acme:ticket:4821")),
    ("https://acme.zendesk.com/tickets/4821", ("issue", "acme:ticket:4821")),
    ("https://ACME.zendesk.com/agent/tickets/7", ("issue", "acme:ticket:7")),
])
def test_ticket_url_variants_all_resolve(url, expected):
    assert classify_zendesk_link(url) == expected
