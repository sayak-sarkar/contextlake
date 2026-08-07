"""Figma connector: link repos to the design files they reference.

A Figma URL in a repo's docs is an explicit, trustworthy reference, so association
needs no verification pass (unlike issue keys harvested from branch names). The
connector claims ``figma.com`` URLs, classifies them to a stable file key, and
emits ``repo --designed_in--> design`` edges. If a Figma MCP is configured, real
metadata (a name and/or top structural frame/page names) is fetched best-effort
and merged in, on top of the URL-slug title that's always the fallback.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote

from ..embeddings.index import EMBEDDABLE_KINDS
from ..ids import make_id
from ..mcp_client import call_tool
from ..model import EXTERNAL_REPO, Confidence, Node
from ..resilience import note_unavailable
from .common import claims, link_edge, repo_node

DEFAULT_HOSTS = ("figma.com",)

# Figma file/design/proto/board URLs carry a stable key and the file name as a
# slug: /<kind>/<KEY>/<File-Name-Slug>?node-id=<NODE>. The slug is the reliable
# human name — the live MCP get_metadata returns XML structure (not a file name)
# and is gated on edit access, so the URL itself is the source of truth.
_FILE_RX = re.compile(r"/(?:file|design|proto|board)/([A-Za-z0-9]+)")
_SLUG_RX = re.compile(r"/(?:file|design|proto|board)/[A-Za-z0-9]+/([^/?#]+)")
_NODE_RX = re.compile(r"[?&]node-id=([A-Za-z0-9:%_-]+)")

# Figma's own get_metadata response is XML text (gated on edit access); some
# third-party Figma MCP servers instead return a simplified dict. Both are
# tolerated: a bare ``name`` attribute out of the dict, or the first few
# ``name="..."`` attributes out of the XML (top-level frames/pages), so a
# reachable design contributes real structural content beyond the URL slug.
_XML_NAME_RX = re.compile(r'\bname="([^"]+)"')

__all__ = [
    "DEFAULT_HOSTS", "FigmaConnector", "associate_designs", "classify_figma_link",
    "design_node", "match_frame_names_to_symbols", "parse_metadata", "title_of",
]


def parse_metadata(result, *, max_names: int = 8) -> dict:
    """A design's real name + top structural node names out of a
    ``get_metadata`` result (no network). Tolerates a dict or an XML string;
    returns ``{}`` for anything else, so a malformed/unexpected response never
    breaks enrichment."""
    if isinstance(result, dict):
        name = result.get("name")
        return {"name": name} if name else {}
    if isinstance(result, str):
        names = _XML_NAME_RX.findall(result)[:max_names]
        return {"structure": names} if names else {}
    return {}


def match_frame_names_to_symbols(
    store, repo_id: str, design_attrs: dict
) -> list[tuple[str, Confidence]]:
    """Design/frame/component names (:func:`parse_metadata`'s ``name``/``structure``
    output) matched case-insensitively against this repo's existing symbol node
    names -- a frame named "Payer" matches a class named ``Payer``. A name match
    is inferred, not a hard fact like GitLab's diff (see
    ``gitlab.match_files_to_nodes``), so every match is ``Confidence.AMBIGUOUS``.

    ``Store.nodes_by_name`` is an exact, BINARY-collation lookup -- making it
    case-insensitive would silently break GitLab's file-path matching, where
    case *is* significant. The ``Store`` ABC also has no "all nodes for a repo"
    scan (see ``visualize.payload.repo_subgraph`` for the same gap), so this
    drops to the same raw-SQL escape hatch that function already uses,
    restricted to symbol-shaped kinds via ``EMBEDDABLE_KINDS``.
    """
    candidate_names = []
    if design_attrs.get("name"):
        candidate_names.append(design_attrs["name"])
    candidate_names.extend(design_attrs.get("structure", []))
    wanted = {n.lower() for n in candidate_names if n}
    if not wanted:
        return []
    kind_placeholders = ",".join("?" * len(EMBEDDABLE_KINDS))
    name_placeholders = ",".join("?" * len(wanted))
    rows = store.conn.execute(
        f"""
        SELECT node_id FROM nodes
        WHERE repo_id = ? AND kind IN ({kind_placeholders}) AND LOWER(name) IN ({name_placeholders})
        ORDER BY node_id
        """,  # noqa: S608 - placeholders only; values bound
        (repo_id, *EMBEDDABLE_KINDS, *wanted),
    ).fetchall()
    return [(row[0], Confidence.AMBIGUOUS) for row in rows]


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
    return Node(id=make_id("figma", "design", key), repo=EXTERNAL_REPO, kind="design",
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
    from config, so the connector stays generic and hard-codes no credentials or
    account specifics."""

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

    def fetch_metadata(self, file_key: str, *, node_id: str | None = None):
        """Best-effort: the design's raw ``get_metadata`` result, or ``None`` if
        unreachable/unconfigured. Never raises: enrichment must not break the
        association graph. See :func:`parse_metadata` for turning this into
        real structural content (Figma's own response is XML, gated on edit
        access; some third-party servers return a simplified dict instead).

        Best-effort still means *observable*: the reason is logged rather than
        swallowed, because a returned ``None`` is otherwise indistinguishable
        from "this design has no metadata" (see
        :func:`resilience.note_unavailable`). Repeated failures trip the shared
        per-server circuit breaker in :func:`mcp_client.call_tool`, so a dead
        MCP costs the run a few timeouts, not one per design."""
        if not (self.mcp_url or self.mcp_command):
            return None
        cmd, args, env = self._spawn()
        payload = {"fileKey": file_key}
        if node_id:
            payload["nodeId"] = node_id
        try:
            return call_tool(cmd, args, "get_metadata", payload, timeout=self.timeout, env=env)
        except Exception as e:  # noqa: BLE001 - enrichment is best-effort
            note_unavailable(f"figma design {file_key}", e)
            return None

    def verify(self, file_key: str, *, node_id: str | None = None) -> bool:
        """Best-effort liveness check: is this design file reachable via the MCP?

        Returns ``False`` when no MCP is configured or the call fails. Never
        raises: verification must not break the association graph.
        """
        return bool(self.fetch_metadata(file_key, node_id=node_id))
