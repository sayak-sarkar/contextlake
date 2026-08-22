"""Zendesk connector: link repos to the support tickets and help articles about them.

A Zendesk URL in a repo's docs is an explicit, trustworthy reference, so association
needs no verification pass -- the same reasoning the Figma and Slack connectors give.
The connector claims ``*.zendesk.com`` URLs, classifies them to a ticket id or a Help
Center article id, and emits ``repo --discussed_in--> issue`` / ``repo --documented_by-->
document`` edges.

**This connector makes no network call at all, and that is deliberate rather than
unfinished.** Every other connector here talks to a configured MCP to verify what it
found; Zendesk's API needs a per-instance API token, and the association it would buy is
already stated by the link itself. Reading the ticket body would add a subject line and a
status, at the cost of credentials, an egress exception, and a connector that degrades on
a network contextlake otherwise does not need. So the association is the whole feature,
which also means Zendesk is the one connector that works inside the offline boundary
rather than as an opt-in exception to it. Fetching ticket bodies is a later decision with
a real trade-off, not an oversight.

The subdomain is part of every node id. Two Zendesk instances both have a ticket 1, and
collapsing them would merge one company's support history into another's.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from ..ids import make_id
from ..model import EXTERNAL_REPO, Node
from .common import claims, host_of, link_edge, repo_node

DEFAULT_HOSTS = ("zendesk.com",)

# `/agent/tickets/123` is what a human copies out of the agent UI; `/api/v2/tickets/123`
# is what a script or a runbook pastes. Both name the same ticket and both resolve to the
# same id, because `search` finds `/tickets/123` wherever it sits in the path -- an
# explicit `(?:api/v2/)?` alternative was written here first and removing it changed
# nothing, which is how it was found to be decoration rather than logic.
_TICKET_RX = re.compile(r"/(?:agent/)?tickets/(\d+)")
# Help Center: `/hc/en-us/articles/360001234567-How-to-rotate-a-key`
_ARTICLE_RX = re.compile(r"/hc/[^/]+/articles/(\d+)")
_ARTICLE_SLUG_RX = re.compile(r"/hc/[^/]+/articles/\d+[-/]([^/?#]+)")

__all__ = [
    "DEFAULT_HOSTS",
    "associate_tickets",
    "classify_zendesk_link",
    "subdomain_of",
    "ticket_node",
    "title_of",
]


def subdomain_of(url: str) -> str | None:
    """The Zendesk instance name from ``https://acme.zendesk.com/...`` -> ``acme``.

    ``None`` for a bare ``zendesk.com`` with no instance, which is not a link to anything
    and must not become a node keyed on the empty string.
    """
    host = host_of(url)
    if not host:
        return None
    head = host.split(".")[0]
    return head or None if host.count(".") >= 2 else None


def classify_zendesk_link(url: str) -> tuple[str, str] | None:
    """``(kind, key)`` for a Zendesk URL, or ``None`` when it names neither.

    ``kind`` is ``"issue"`` for a ticket and ``"document"`` for a Help Center article --
    both already in the kind registry, so a Zendesk node groups with Jira issues and with
    generated documents rather than inventing a category of its own.

    The key carries the subdomain (``acme:ticket:42``), because ticket numbers restart at
    1 per instance.
    """
    sub = subdomain_of(url)
    if not sub:
        return None
    m = _ARTICLE_RX.search(url)
    if m:
        return "document", f"{sub}:article:{m.group(1)}"
    m = _TICKET_RX.search(url)
    if m:
        return "issue", f"{sub}:ticket:{m.group(1)}"
    return None


def title_of(url: str) -> str | None:
    """The human title from a Help Center slug, or ``None``.

    A ticket URL carries no title -- the subject lives behind the API -- so a ticket node
    is named by its key and says nothing it cannot support.
    """
    m = _ARTICLE_SLUG_RX.search(url)
    if not m:
        return None
    return unquote(m.group(1)).replace("-", " ").strip() or None


def ticket_node(kind: str, key: str, *, url: str | None = None,
                title: str | None = None, site: str | None = None) -> Node:
    attrs = {k: v for k, v in {"url": url, "title": title, "site": site}.items() if v}
    return Node(id=make_id("zendesk", kind, key), repo=EXTERNAL_REPO, kind=kind,
                name=key, attrs=attrs)


#: Which relation each kind gets. A ticket is a conversation about the repo, the same
#: shape Slack's channels have; an article documents it. Both names are already in the
#: graph's vocabulary, so no consumer has to learn a Zendesk-specific relation.
_RELATIONS = {"issue": "discussed_in", "document": "documented_by"}


def associate_tickets(repo_id: str, *, links=(), site_hosts=DEFAULT_HOSTS):
    """Build repo->ticket/article nodes and edges from Zendesk links in docs.

    No network, by design (see the module docstring). Returns ``(nodes, edges)`` the way
    :func:`figma.associate_designs` does, so the orchestrator treats it identically.
    """
    nodes: dict[str, Node] = {}
    repo = repo_node(repo_id)
    nodes[repo.id] = repo
    edges: dict[tuple[str, str, str], object] = {}
    for url in links:
        if not claims(url, site_hosts):
            continue
        classified = classify_zendesk_link(url)
        if not classified:
            continue
        kind, key = classified
        node = ticket_node(kind, key, url=url, title=title_of(url),
                           site=subdomain_of(url))
        nodes.setdefault(node.id, node)
        relation = _RELATIONS[kind]
        edges.setdefault(
            (repo.id, node.id, relation),
            link_edge(repo_id, node, relation, "docs"),
        )
    return list(nodes.values()), list(edges.values())
