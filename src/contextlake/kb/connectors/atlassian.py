"""Atlassian (Jira + Confluence) connector via the hosted Rovo MCP.

Each configured site is an independently-authenticated session reached through
``mcp-remote``. Per-site OAuth caches are isolated via ``MCP_REMOTE_CONFIG_DIR``
so multiple orgs/identities coexist. The connector fetches items and the
module-level helpers map them into provenance-stamped graph nodes/edges.
"""

from __future__ import annotations

import json
import os
import re

from .._util import chunks
from ..ids import make_id
from ..mcp_client import call_tool
from ..model import EXTERNAL_REPO, Confidence, Edge, Node
from .common import claims, edge_from, host_of, link_edge, repo_node

__all__ = [
    "AtlassianConnector", "DEFAULT_MCP_URL", "DEFAULT_SCOPES", "associate",
    "associate_symbols", "claims", "classify_link", "external_node", "host_of",
    "issue_summary", "link_edge", "parse_search_issues", "parse_sites", "repo_node",
]

DEFAULT_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"

# Read-only product scopes, requested explicitly.
#
# `mcp-remote` resolves its OAuth scope as: an explicit scope, else the server's
# advertised `scopes_supported`, else its own default. Atlassian's SSE endpoint
# advertises none, so leaving this to the default asked for `openid email profile`
# -- enough to identify the person, and nothing about Jira or Confluence. The token
# then legitimately saw zero sites, and the connector reported "0 site(s)
# reachable", which reads as a permissions problem on the user's account rather
# than a request that never asked for anything.
#
# `offline_access` is not optional: without a refresh token every run re-opens the
# browser for authorization, which is unusable unattended.
DEFAULT_SCOPES = (
    "read:jira-work read:page:confluence read:space:confluence "
    "read:confluence-user search:confluence offline_access"
)


class AtlassianConnector:
    def __init__(self, name: str, *, mcp_url: str = DEFAULT_MCP_URL,
                 auth_dir: str | None = None, timeout: float = 120,
                 scopes: str | None = None):
        self.name = name
        self.mcp_url = mcp_url
        self.auth_dir = auth_dir
        self.timeout = timeout
        self.scopes = (scopes or DEFAULT_SCOPES).strip()

    def _spawn(self) -> tuple[str, list[str], dict | None]:
        env = None
        if self.auth_dir:
            env = dict(os.environ)
            env["MCP_REMOTE_CONFIG_DIR"] = os.path.expanduser(self.auth_dir)
        args = ["-y", "mcp-remote@latest", self.mcp_url]
        if self.scopes:
            args += ["--static-oauth-client-metadata",
                     json.dumps({"scope": self.scopes})]
        # Deliberately NOT --silent, though the bridge is noisy (it narrates
        # transport negotiation, lockfiles and every JSON-RPC frame onto our
        # console). Its `--silent` gates the same `log()` that prints
        #     "Please authorize this client by visiting: <url>"
        # so silencing the noise also silences the one line a first-time user
        # cannot proceed without. Auto-opening a browser is the only fallback and
        # is unreliable under WSL and over SSH. Noise beats an unauthorizable run.
        return "npx", args, env

    def discover_sites(self) -> dict[str, str]:
        """Map accessible Atlassian site URLs to their cloudIds (live).

        Raises :class:`~..mcp_client.McpToolError` if the server reports the call
        failed, and :class:`ValueError` if the payload is not a site list. Both used
        to come back as an empty mapping, so a tool error, a renamed tool, a changed
        response shape and a genuinely empty site list were one indistinguishable
        "0 site(s) reachable" -- pointing at the only cause that was not true.
        """
        cmd, args, env = self._spawn()
        res = call_tool(cmd, args, "getAccessibleAtlassianResources", {},
                        timeout=self.timeout, env=env)
        return parse_sites(res)

    def search(self, query: str) -> list:
        """Rovo search across Jira + Confluence for a query (live)."""
        cmd, args, env = self._spawn()
        res = call_tool(cmd, args, "search", {"query": query}, timeout=self.timeout, env=env)
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("results") or res.get("result") or []
        return []

    def verify_issues(self, cloud_id: str, keys, batch: int = 100) -> dict[str, dict]:
        """Confirm and enrich candidate issue keys via batched JQL (live).

        Returns ``{key: {key, summary, status, url}}`` for keys that exist on the
        site. JQL ``key in (...)`` silently drops unknown keys, so this both prunes
        regex false-positives and routes keys to their owning site for free.
        """
        ordered = list(dict.fromkeys(k for k in keys if k))
        found: dict[str, dict] = {}
        cmd, args, env = self._spawn()
        for chunk in chunks(ordered, batch):
            jql = "key in (" + ", ".join(chunk) + ")"
            res = call_tool(
                cmd, args, "searchJiraIssuesUsingJql",
                {"cloudId": cloud_id, "jql": jql, "maxResults": min(batch, 100),
                 "fields": ["summary", "status"]},
                timeout=self.timeout, env=env,
            )
            for node in parse_search_issues(res):
                s = issue_summary(node)
                if s["key"]:
                    found[s["key"]] = s
        return found


# --- pure parsing of fetched payloads (no network) -------------------------

def parse_sites(result) -> dict[str, str]:
    """``{site_url: cloud_id}`` out of a getAccessibleAtlassianResources result.

    Tolerant of the envelope the way :func:`parse_search_issues` already is: the
    list may arrive bare or wrapped under ``resources``/``values``/``result``. What
    it will NOT do is treat an unrecognised payload as "no sites" -- that silent
    conflation is the whole defect. An empty *list* means no sites; anything that is
    not a list of site-shaped dicts raises, naming what arrived instead.
    """
    payload = result
    if isinstance(payload, dict):
        for key in ("resources", "values", "result", "sites", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            # A single site object, rather than a list of them.
            payload = [payload] if payload.get("url") else None
    if not isinstance(payload, list):
        got = type(result).__name__
        detail = f": {result[:200]!r}" if isinstance(result, str) else ""
        raise ValueError(
            f"expected a list of Atlassian sites, got {got}{detail}")
    sites = {s["url"]: s.get("id", "") for s in payload
             if isinstance(s, dict) and s.get("url")}
    if payload and not sites:
        raise ValueError(
            f"{len(payload)} item(s) returned but none carried a site url; "
            f"first item: {payload[0]!r:.200}")
    return sites


def parse_search_issues(result) -> list:
    """Issue nodes out of a searchJiraIssuesUsingJql result (Rovo or REST shape)."""
    if isinstance(result, dict):
        issues = result.get("issues")
        if isinstance(issues, dict):
            return issues.get("nodes") or []
        if isinstance(issues, list):
            return issues
    return result if isinstance(result, list) else []


def issue_summary(node: dict) -> dict:
    """Flatten a Jira issue node to {key, summary, status, url} (tolerant)."""
    fields = node.get("fields") or {}
    status = fields.get("status")
    return {
        "key": node.get("key"),
        "summary": fields.get("summary"),
        "status": status.get("name") if isinstance(status, dict) else status,
        "url": node.get("webUrl") or node.get("self"),
    }


# --- pure graph mapping (no network) ---------------------------------------

def external_node(kind: str, key: str, *, title: str | None = None,
                  url: str | None = None, site: str | None = None) -> Node:
    attrs = {k: v for k, v in {"title": title, "url": url, "site": site}.items() if v}
    return Node(id=make_id("atlassian", kind, key), repo=EXTERNAL_REPO, kind=kind,
                name=key, attrs=attrs)


# --- URL classification (Atlassian-specific; host claiming is in common) ----

_BROWSE_RX = re.compile(r"/browse/([A-Z][A-Z0-9]+-\d+)")
_PAGE_NUM_RX = re.compile(r"/pages/(\d+)")
_TINY_RX = re.compile(r"/wiki/x/([A-Za-z0-9]+)")


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


def associate_symbols(symbol_keys: dict[str, str],
                      *, source: str = "symbol") -> tuple[list[Node], list[Edge]]:
    """Build symbol -> issue nodes/edges from per-symbol candidate keys (no
    network). ``symbol_keys`` is ``{symbol_node_id: issue_key}`` (see
    :mod:`symbol_refs`) -- a bare-key regex match, same trust tier as a
    branch-name-derived key, so every edge is AMBIGUOUS pending the same live
    JQL verification the repo-level candidates already go through.
    """
    nodes: dict[str, Node] = {}
    edges: dict[tuple[str, str, str], Edge] = {}
    for symbol_id, key in symbol_keys.items():
        node = external_node("issue", key)
        nodes.setdefault(node.id, node)
        edges.setdefault(
            (symbol_id, node.id, "tracked_by"),
            edge_from(symbol_id, node, "tracked_by", source, confidence=Confidence.AMBIGUOUS),
        )
    return list(nodes.values()), list(edges.values())
