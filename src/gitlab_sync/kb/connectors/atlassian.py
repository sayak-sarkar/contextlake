"""Atlassian (Jira + Confluence) connector via the hosted Rovo MCP.

Each configured site is an independently-authenticated session reached through
``mcp-remote``. Per-site OAuth caches are isolated via ``MCP_REMOTE_CONFIG_DIR``
so multiple orgs/identities coexist. The connector fetches items and the
module-level helpers map them into provenance-stamped graph nodes/edges.
"""

from __future__ import annotations

import os
from datetime import date

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


def link_edge(repo_id: str, ext: Node, relation: str, source_file: str,
              verified_at: date | None = None) -> Edge:
    """A repo -> external-knowledge edge (e.g. tracked_by / documented_by)."""
    return Edge(
        src=make_id("repo", repo_id), dst=ext.id, relation=relation,
        confidence=Confidence.INFERRED,
        provenance=Provenance(source_file=source_file, verified_at=verified_at or date.today()),
    )
