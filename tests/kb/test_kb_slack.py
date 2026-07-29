"""Tests for the Slack connector: pure URL classification + association, env
plumbing, and best-effort verification against a spawned mock MCP server."""

import os
import sys

from contextlake.kb.connectors.slack import (
    SlackConnector,
    associate_slack,
    classify_slack_link,
    slack_node,
)
from contextlake.kb.model import Confidence

_MOCK_SERVER = """
from mcp.server.mcpserver import MCPServer
m = MCPServer("mock-slack")

@m.tool()
def conversations_info(channel: str) -> dict:
    return {"channel": channel, "name": "general"}

m.run()
"""


def _server(tmp_path):
    p = tmp_path / "mock_slack.py"
    p.write_text(_MOCK_SERVER)
    return [str(p)]


# --- pure URL classification ------------------------------------------------

def test_classify_slack_link_message():
    kind, key = classify_slack_link(
        "https://acme.slack.com/archives/C0123ABCD/p1234567890123456")
    assert kind == "message" and key == "C0123ABCD-1234567890.123456"


def test_classify_slack_link_channel():
    assert classify_slack_link("https://acme.slack.com/archives/C0123ABCD") == (
        "channel", "C0123ABCD")
    assert classify_slack_link("https://acme.slack.com/archives/C0123ABCD?x=1") == (
        "channel", "C0123ABCD")


def test_classify_slack_link_not_a_reference():
    assert classify_slack_link("https://acme.slack.com/customize/emoji") is None


def test_slack_node_id_stable_and_attrs():
    a = slack_node("channel", "C1", url="https://acme.slack.com/archives/C1")
    b = slack_node("channel", "C1")
    assert a.id == b.id and a.kind == "channel" and a.name == "C1"
    assert a.attrs["url"].endswith("/archives/C1")
    assert b.attrs == {}


# --- association -------------------------------------------------------------

def test_associate_slack_claims_and_classifies():
    nodes, edges = associate_slack(
        "group/app",
        links=[
            "https://acme.slack.com/archives/C0123ABCD/p1234567890123456",
            "https://example.atlassian.net/browse/PROJ-1",  # foreign host, ignored
        ],
    )
    messages = [n for n in nodes if n.kind == "message"]
    assert len(messages) == 1 and messages[0].name == "C0123ABCD-1234567890.123456"
    assert len(edges) == 1
    assert edges[0].relation == "discussed_in"
    assert edges[0].confidence == Confidence.INFERRED


def test_associate_slack_channel_link_uses_referenced_in():
    nodes, edges = associate_slack(
        "group/app", links=["https://acme.slack.com/archives/C0123ABCD"])
    channels = [n for n in nodes if n.kind == "channel"]
    assert len(channels) == 1
    assert edges[0].relation == "referenced_in"


def test_associate_slack_dedupes():
    nodes, edges = associate_slack(
        "group/app",
        links=[
            "https://acme.slack.com/archives/C1",
            "https://acme.slack.com/archives/C1?x=1",  # same channel
        ],
    )
    assert sum(1 for n in nodes if n.kind == "channel") == 1
    assert sum(1 for n in nodes if n.kind == "repo") == 1
    assert len(edges) == 1


# --- connector plumbing -------------------------------------------------------

def test_spawn_with_command_and_auth_dir():
    c = SlackConnector("s", mcp_command="slack-mcp --stdio", auth_dir="~/auth/slack")
    cmd, args, env = c._spawn()
    assert cmd == "slack-mcp" and args == ["--stdio"]
    assert env["MCP_REMOTE_CONFIG_DIR"] == os.path.expanduser("~/auth/slack")


def test_spawn_defaults_to_mcp_remote():
    cmd, args, env = SlackConnector("s", mcp_url="https://mcp.example/slack")._spawn()
    assert cmd == "npx" and "mcp-remote@latest" in args
    assert "https://mcp.example/slack" in args and env is None


def test_verify_false_without_mcp():
    assert SlackConnector("s").verify("C1") is False


def test_verify_true_via_mock(tmp_path):
    c = SlackConnector("s", mcp_command="placeholder")
    c._spawn = lambda: (sys.executable, _server(tmp_path), None)
    assert c.verify("C1") is True


def test_verify_uses_configured_tool_name(tmp_path):
    c = SlackConnector("s", mcp_command="placeholder", verify_tool="conversations_info")
    c._spawn = lambda: (sys.executable, _server(tmp_path), None)
    assert c.verify("C1") is True
