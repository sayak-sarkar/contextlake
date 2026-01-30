"""Tests for the MCP client, against a spawned mock MCP server (no network)."""

import sys

from gitlab_sync.kb.mcp_client import call_tool, list_tools

# A tiny stdio FastMCP server used as the connection target.
_MOCK_SERVER = """
from mcp.server.fastmcp import FastMCP
m = FastMCP("mock")

@m.tool()
def echo(text: str) -> str:
    return text

@m.tool()
def items() -> list[dict]:
    return [{"key": "A-1"}, {"key": "A-2"}]

m.run()
"""


def _server(tmp_path):
    p = tmp_path / "mock_server.py"
    p.write_text(_MOCK_SERVER)
    return [str(p)]


def test_list_tools(tmp_path):
    names = list_tools(sys.executable, _server(tmp_path))
    assert {"echo", "items"} <= set(names)


def test_call_tool_scalar(tmp_path):
    assert call_tool(sys.executable, _server(tmp_path), "echo", {"text": "hello"}) == "hello"


def test_call_tool_structured(tmp_path):
    result = call_tool(sys.executable, _server(tmp_path), "items")
    assert result == [{"key": "A-1"}, {"key": "A-2"}]
