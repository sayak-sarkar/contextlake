"""Atlassian (Jira + Confluence) connector via the hosted Rovo MCP.

Each configured site is an independently-authenticated session reached through
``mcp-remote``. Per-site OAuth caches are isolated via ``MCP_REMOTE_CONFIG_DIR``
so multiple orgs/identities coexist. The connector fetches items and the
module-level helpers map them into provenance-stamped graph nodes/edges.
"""

from __future__ import annotations

import os
import re
from datetime import date
from urllib.parse import urlparse

from ..ids import make_id
from ..mcp_client import call_tool
from ..model import Confidence, Edge, Node, Provenance

DEFAULT_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"


class AtlassianConnector:
    def __init__(self, name: str, *, mcp_url: str = DEFAULT_MCP_URL,
                 auth_dir: str | None = None, timeout: float = 120):
        self.name = name
        self.mcp_url = mcp_url
        self.auth_dir = auth_dir
        self.timeout = timeout

    def _spawn(self) -> tuple[str, list[str], dict | None]:
        env = None
        if self.auth_dir:
            env = dict(os.environ)
            env["MCP_REMOTE_CONFIG_DIR"] = os.path.expanduser(self.auth_dir)
        return "npx", ["-y", "mcp-remote@latest", self.mcp_url], env

    def discover_sites(self) -> dict[str, str]:
        """Map accessible Atlassian site URLs to their cloudIds (live)."""
        cmd, args, env = self._spawn()
        res = call_tool(cmd, args, "getAccessibleAtlassianResources", {},
                        timeout=self.timeout, env=env)
        return {s["url"]: s["id"] for s in res if isinstance(s, dict) and s.get("url")}

    def search(self, query: str) -> list:
        """Rovo search across Jira + Confluence for a query (live)."""
        cmd, args, env = self._spawn()
        res = call_tool(cmd, args, "search", {"query": query}, timeout=self.timeout, env=env)
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("results") or res.get("result") or []
        return []


# --- pure graph mapping (no network) ---------------------------------------

def repo_node(repo_id: str) -> Node:
    return Node(id=make_id("repo", repo_id), repo=repo_id, kind="repo", name=repo_id)


def external_node(kind: str, key: str, *, title: str | None = None,
                  url: str | None = None, site: str | None = None) -> Node:
    attrs = {k: v for k, v in {"title": title, "url": url, "site": site}.items() if v}
    return Node(id=make_id("atlassian", kind, key), repo="(external)", kind=kind,
                name=key, attrs=attrs)


def link_edge(repo_id: str, ext: Node, relation: str, source_file: str, *,
              confidence: Confidence = Confidence.INFERRED,
              verified_at: date | None = None) -> Edge:
    """A repo -> external-knowledge edge (e.g. tracked_by / documented_by)."""
    return Edge(
        src=make_id("repo", repo_id), dst=ext.id, relation=relation,
        confidence=confidence,
        provenance=Provenance(source_file=source_file, verified_at=verified_at or date.today()),
    )


# --- URL claiming + classification (generic seam, reused by Figma) ---------

_BROWSE_RX = re.compile(r"/browse/([A-Z][A-Z0-9]+-\d+)")
_PAGE_NUM_RX = re.compile(r"/pages/(\d+)")
_TINY_RX = re.compile(r"/wiki/x/([A-Za-z0-9]+)")


def host_of(url: str) -> str | None:
    try:
        return urlparse(url).netloc.lower() or None
    except ValueError:
        return None


def claims(url: str, site_hosts) -> bool:
    """Whether ``url`` belongs to one of this connector's Atlassian sites."""
    h = host_of(url)
    return bool(h and any(h == s.lower() or h.endswith("." + s.lower()) for s in site_hosts))


def classify_link(url: str) -> tuple[str, str] | None:
    """Map an Atlassian URL to (kind, key): an issue browse link or a Confluence
    page (numeric id or tiny-link id). Host filtering is the caller's job."""
    m = _BROWSE_RX.search(url)
    if m:
        return "issue", m.group(1)
    m = _PAGE_NUM_RX.search(url) or _TINY_RX.search(url)
    if m:
        return "page", m.group(1)
    return None


def associate(repo_id: str, *, issue_keys=(), links=(), site_hosts=()):
    """Build candidate repo->external nodes/edges from reference signals (no network).

    Issue keys found in git refs are AMBIGUOUS candidates (the key regex can match
    non-issues like UTF-8); explicit doc URLs claimed for this connector's sites are
    INFERRED. A later live JQL pass confirms keys and enriches metadata.
    """
    nodes: dict[str, Node] = {}
    repo = repo_node(repo_id)
    nodes[repo.id] = repo
    edges: dict[tuple[str, str, str], Edge] = {}

    def _add(node: Node, relation: str, source: str, confidence: Confidence) -> None:
        nodes.setdefault(node.id, node)
        edges.setdefault(
            (repo.id, node.id, relation),
            link_edge(repo_id, node, relation, source, confidence=confidence),
        )

    for key in issue_keys:
        _add(external_node("issue", key), "tracked_by", "git:refs", Confidence.AMBIGUOUS)

    for url in links:
        if not claims(url, site_hosts):
            continue
        c = classify_link(url)
        if not c:
            continue
        kind, key = c
        relation = "tracked_by" if kind == "issue" else "documented_by"
        _add(external_node(kind, key, url=url), relation, "docs", Confidence.INFERRED)

    return list(nodes.values()), list(edges.values())
