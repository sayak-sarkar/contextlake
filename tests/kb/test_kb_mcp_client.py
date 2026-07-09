"""Tests for the MCP client, against a spawned mock MCP server (no network)."""

import sys

import contextlake.kb.mcp_client as mcp_client
from contextlake.kb.mcp_client import call_tool, list_tools

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


class _FakeHttpResult:
    structuredContent = {"result": "ok"}
    content = None


class _FakeHttpSession:
    """Stub session standing in for ``mcp.ClientSession`` in the http branch."""

    def __init__(self, read, write):
        self.read = read
        self.write = write

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, tool, arguments):
        assert tool == "search"
        assert arguments == {"q": "a"}
        return _FakeHttpResult()


class _FakeStreamableHttpCm:
    """Stub async context manager standing in for ``streamablehttp_client``."""

    def __init__(self, url):
        self.url = url

    async def __aenter__(self):
        return ("read-stream", "write-stream", "extra-stream")

    async def __aexit__(self, *exc_info):
        return False


def test_call_tool_http(monkeypatch):
    monkeypatch.setattr(mcp_client, "streamablehttp_client", _FakeStreamableHttpCm)
    monkeypatch.setattr(mcp_client, "ClientSession", _FakeHttpSession)
    result = call_tool(url="http://example.test/mcp", tool="search", arguments={"q": "a"})
    assert result == "ok"
