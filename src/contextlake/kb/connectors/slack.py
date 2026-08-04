"""Slack connector: link repos to the channels/messages that discuss them.

A Slack permalink in a repo's docs (a channel link or a message permalink) is an
explicit, trustworthy reference, so association needs no verification pass (the
same reasoning as the Figma connector). The connector claims ``slack.com`` URLs,
classifies them to a channel id (and, for message permalinks, a timestamp), and
emits ``repo --discussed_in/referenced_in--> slack`` edges. Optional liveness
verification is fetched best-effort over a configured Slack MCP.

There is no single spec-mandated MCP tool name across Slack MCP servers (unlike
Figma's first-party ``get_metadata``), so the verification tool name is left
configurable (``verify_tool``) with a reasonable default rather than assumed.
"""

from __future__ import annotations

import os
import re

from ..ids import make_id
from ..mcp_client import call_tool
from ..model import EXTERNAL_REPO, Node
from ..resilience import note_unavailable
from .common import claims, link_edge, repo_node

DEFAULT_HOSTS = ("slack.com",)
DEFAULT_VERIFY_TOOL = "conversations_info"
DEFAULT_HISTORY_TOOL = "conversations_history"

# A Slack permalink is https://<workspace>.slack.com/archives/<CHANNEL>[/p<TS16>].
# The message timestamp is embedded with no dot as 16 digits; the real Slack
# ``ts`` re-inserts it 6 digits from the end (e.g. ``p1234567890123456`` ->
# ``1234567890.123456``).
_MESSAGE_RX = re.compile(r"/archives/([A-Z0-9]+)/p(\d{16})\b")
_CHANNEL_RX = re.compile(r"/archives/([A-Z0-9]+)(?:[/?#]|$)")

__all__ = [
    "DEFAULT_HISTORY_TOOL", "DEFAULT_HOSTS", "DEFAULT_VERIFY_TOOL", "SlackConnector",
    "associate_slack", "classify_slack_link", "slack_node",
]


def classify_slack_link(url: str) -> tuple[str, str] | None:
    """Map a slack.com URL to (kind, key): a message permalink or a channel link."""
    m = _MESSAGE_RX.search(url)
    if m:
        channel, raw_ts = m.group(1), m.group(2)
        return "message", f"{channel}-{raw_ts[:-6]}.{raw_ts[-6:]}"
    m = _CHANNEL_RX.search(url)
    if m:
        return "channel", m.group(1)
    return None


def slack_node(kind: str, key: str, *, url: str | None = None) -> Node:
    attrs = {"url": url} if url else {}
    return Node(id=make_id("slack", kind, key), repo=EXTERNAL_REPO, kind=kind,
                name=key, attrs=attrs)


def associate_slack(repo_id: str, *, links=(), site_hosts=DEFAULT_HOSTS):
    """Build repo->slack nodes/edges from slack.com links in docs (no network)."""
    nodes: dict[str, Node] = {}
    repo = repo_node(repo_id)
    nodes[repo.id] = repo
    edges: dict[tuple[str, str, str], object] = {}
    for url in links:
        if not claims(url, site_hosts):
            continue
        c = classify_slack_link(url)
        if not c:
            continue
        kind, key = c
        node = slack_node(kind, key, url=url)
        nodes.setdefault(node.id, node)
        relation = "discussed_in" if kind == "message" else "referenced_in"
        edges.setdefault(
            (repo.id, node.id, relation),
            link_edge(repo_id, node, relation, "docs"),
        )
    return list(nodes.values()), list(edges.values())


class SlackConnector:
    """Talks to a configured Slack MCP. Host(s), the MCP endpoint/command, and the
    verification tool name come from config, so the connector stays generic and
    hard-codes no credentials, workspace, or server-specific assumptions."""

    def __init__(self, name: str, *, mcp_url: str | None = None,
                 mcp_command: str | None = None, hosts=DEFAULT_HOSTS,
                 verify_tool: str = DEFAULT_VERIFY_TOOL,
                 history_tool: str = DEFAULT_HISTORY_TOOL,
                 auth_dir: str | None = None, timeout: float = 120):
        self.name = name
        self.mcp_url = mcp_url
        self.mcp_command = mcp_command
        self.hosts = tuple(hosts)
        self.verify_tool = verify_tool
        self.history_tool = history_tool
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

    def verify(self, channel: str) -> bool:
        """Best-effort liveness check: is this channel reachable via the MCP?

        Returns ``False`` when no MCP is configured or the call fails. Never
        raises: verification must not break the association graph. The reason is
        logged rather than swallowed (see :func:`resilience.note_unavailable`) --
        an unreachable Slack MCP and a channel that genuinely doesn't exist both
        return ``False``, and only the log distinguishes them.
        """
        if not (self.mcp_url or self.mcp_command):
            return False
        cmd, args, env = self._spawn()
        try:
            res = call_tool(cmd, args, self.verify_tool, {"channel": channel},
                            timeout=self.timeout, env=env)
        except Exception as e:  # noqa: BLE001 - verification is best-effort
            note_unavailable(f"slack channel {channel}", e)
            return False
        return bool(res)

    def fetch_messages(self, channel: str, *, limit: int = 50) -> list[str]:
        """Raw message text bodies for a channel (live, best-effort).

        Returns ``[]`` when no MCP is configured or the call fails/returns an
        unexpected shape. Never raises: fetching must not break the
        association graph, same contract as :meth:`verify`.
        """
        if not (self.mcp_url or self.mcp_command):
            return []
        cmd, args, env = self._spawn()
        try:
            result = call_tool(cmd, args, self.history_tool, {"channel": channel, "limit": limit},
                               timeout=self.timeout, env=env)
        except Exception as e:  # noqa: BLE001 - fetching is best-effort
            note_unavailable(f"slack history for {channel}", e)
            return []
        if not isinstance(result, dict):
            return []
        messages = result.get("messages", [])
        return [m["text"] for m in messages if isinstance(m, dict) and m.get("text")]
