"""Figma connector: link repos to the design files they reference.

A Figma URL in a repo's docs is an explicit, trustworthy reference, so association
needs no verification pass (unlike issue keys harvested from branch names). The
connector claims ``figma.com`` URLs, classifies them to a stable file key, and
emits ``repo --designed_in--> design`` edges. Optional metadata enrichment (the
human-readable file name) is fetched best-effort over a configured Figma MCP.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote

from ..ids import make_id
from ..mcp_client import call_tool
from ..model import Node
from .common import claims, link_edge, repo_node

DEFAULT_HOSTS = ("figma.com",)

# Figma file/design/proto/board URLs carry a stable key and the file name as a
# slug: /<kind>/<KEY>/<File-Name-Slug>?node-id=<NODE>. The slug is the reliable
# human name — the live MCP get_metadata returns XML structure (not a file name)
# and is gated on edit access, so the URL itself is the source of truth.
_FILE_RX = re.compile(r"/(?:file|design|proto|board)/([A-Za-z0-9]+)")
_SLUG_RX = re.compile(r"/(?:file|design|proto|board)/[A-Za-z0-9]+/([^/?#]+)")
_NODE_RX = re.compile(r"[?&]node-id=([A-Za-z0-9:%_-]+)")

__all__ = [
    "DEFAULT_HOSTS", "FigmaConnector", "associate_designs", "classify_figma_link",
    "design_node", "title_of",
]


def classify_figma_link(url: str) -> str | None:
    """The stable Figma file key from a figma.com URL, or None if not a file URL."""
    m = _FILE_RX.search(url)
    return m.group(1) if m else None


def title_of(url: str) -> str | None:
    """The human file name from a Figma URL slug (``/design/KEY/My-App`` -> ``My App``)."""
    m = _SLUG_RX.search(url)
    if not m:
        return None
    return unquote(m.group(1)).replace("-", " ").strip() or None


def design_node(key: str, *, url: str | None = None, title: str | None = None,
                node_id: str | None = None) -> Node:
    attrs = {k: v for k, v in {"url": url, "title": title, "node_id": node_id}.items() if v}
    return Node(id=make_id("figma", "design", key), repo="(external)", kind="design",
                name=key, attrs=attrs)


def associate_designs(repo_id: str, *, links=(), site_hosts=DEFAULT_HOSTS):
    """Build repo->design nodes/edges from figma.com links in docs (no network)."""
    nodes: dict[str, Node] = {}
    repo = repo_node(repo_id)
    nodes[repo.id] = repo
    edges: dict[tuple[str, str, str], object] = {}
    for url in links:
        if not claims(url, site_hosts):
            continue
        key = classify_figma_link(url)
        if not key:
            continue
        m = _NODE_RX.search(url)
        node = design_node(key, url=url, title=title_of(url),
                           node_id=m.group(1) if m else None)
        nodes.setdefault(node.id, node)
        edges.setdefault(
            (repo.id, node.id, "designed_in"),
            link_edge(repo_id, node, "designed_in", "docs"),
        )
    return list(nodes.values()), list(edges.values())


class FigmaConnector:
    """Talks to a configured Figma MCP. Host(s) and the MCP endpoint/command come
    from config, so the connector stays generic and hard-codes no organization or
    Figma-account specifics."""

    def __init__(self, name: str, *, mcp_url: str | None = None,
                 mcp_command: str | None = None, hosts=DEFAULT_HOSTS,
                 auth_dir: str | None = None, timeout: float = 120):
        self.name = name
        self.mcp_url = mcp_url
        self.mcp_command = mcp_command
        self.hosts = tuple(hosts)
        self.auth_dir = auth_dir
        self.timeout = timeout

    def _spawn(self) -> tuple[str, list[str], dict | None]:
        env = None
        if self.auth_dir:
            env = dict(os.environ)
            env["MCP_REMOTE_CONFIG_DIR"] = os.path.expanduser(self.auth_dir)
        if self.mcp_command:
            parts = self.mcp_command.split()
            return parts[0], parts[1:], env
        return "npx", ["-y", "mcp-remote@latest", self.mcp_url or ""], env

    def verify(self, file_key: str, *, node_id: str | None = None) -> bool:
        """Best-effort liveness check: is this design file reachable via the MCP?

        Returns ``False`` when no MCP is configured or the call fails (Figma's
        ``get_metadata`` is gated on edit access and returns XML, so this only
        confirms reachability — the file name comes from the URL slug, not here).
        Never raises: verification must not break the association graph.
        """
        if not (self.mcp_url or self.mcp_command):
            return False
        cmd, args, env = self._spawn()
        payload = {"fileKey": file_key}
        if node_id:
            payload["nodeId"] = node_id
        try:
            res = call_tool(cmd, args, "get_metadata", payload, timeout=self.timeout, env=env)
        except Exception:  # noqa: BLE001 - verification is best-effort
            return False
        return bool(res)
