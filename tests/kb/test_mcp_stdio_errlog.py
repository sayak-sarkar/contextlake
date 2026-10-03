"""A stdio MCP server must start even if stderr was swapped when the client loaded.

`mcp.client.stdio.stdio_client` declares `errlog: TextIO = sys.stderr`. Python
evaluates that default once, when the module is first imported, so a process that
imported it while `sys.stderr` was a text buffer kept the buffer. The spawned server
needs a real file descriptor for its stderr, so every later call failed with the bare
word `fileno`, the whole of the message the operator saw.

Found as a test-order failure: run first, `test_keys_cmd.py` imported the client
inside a test whose stderr was pytest's capture buffer, and two `test_source_cmd.py`
tests then failed. Each file passed alone.

Driven in a fresh interpreter, because the defect is about what happens at FIRST
import. In this process the client is already imported, so a test here would be
measuring whichever test imported it first.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_MOCK_SERVER = """
from mcp.server.mcpserver import MCPServer
m = MCPServer("mock")


@m.tool()
def echo(text: str) -> str:
    "reached the server"
    return text


@m.resource("note://one")
def one() -> str:
    return "reached the server"


m.run()
"""

_PROBE = """
import io, sys
sys.stderr = io.StringIO()   # a host whose stderr is not a file when the client loads
from contextlake.kb.mcp_client import call_tool, list_tools
from contextlake.kb.sources.mcp import McpSource
server, at_call, entry = sys.argv[1:4]
if at_call == "restored":
    sys.stderr = sys.__stderr__
try:
    if entry == "call_tool":
        print(repr(call_tool(sys.executable, [server], "echo",
                             {"text": "reached the server"}, timeout=60)))
    elif entry == "list_tools":
        print([t.description for t in list_tools(sys.executable, [server], timeout=60).tools])
    else:
        src = McpSource(command=sys.executable, args=[server], timeout=60)
        print([d.text for d in src.iter_documents()], src.failures)
except BaseException as e:
    print("FAILED:", type(e).__name__, e)
"""


@pytest.mark.parametrize("entry", ["call_tool", "list_tools", "mcp_source"])
@pytest.mark.parametrize("at_call", ["restored", "still-swapped"])
def test_a_stdio_server_starts_whatever_stderr_was_at_import(tmp_path, at_call, entry):
    server = tmp_path / "mock_server.py"
    server.write_text(_MOCK_SERVER)

    done = subprocess.run([sys.executable, "-c", _PROBE, str(server), at_call, entry],
                          capture_output=True, text=True, timeout=120)

    assert "reached the server" in done.stdout, (done.stdout, done.stderr[-2000:])
