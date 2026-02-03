"""Tests for the Figma connector: pure URL classification + association, env
plumbing, and best-effort metadata fetch against a spawned mock MCP server."""

import os
import sys

from gitlab_sync.kb.connectors.figma import (
    FigmaConnector,
    associate_designs,
    classify_figma_link,
    design_node,
    title_of,
)
from gitlab_sync.kb.model import Confidence

_MOCK_SERVER = """
from mcp.server.fastmcp import FastMCP
m = FastMCP("mock-figma")

@m.tool()
def get_metadata(fileKey: str) -> dict:
    return {"name": "Design System", "key": fileKey}

m.run()
"""


def _server(tmp_path):
    p = tmp_path / "mock_figma.py"
    p.write_text(_MOCK_SERVER)
    return [str(p)]


# --- pure URL classification ----------------------------------------------

def test_classify_figma_link_forms():
    assert classify_figma_link("https://www.figma.com/file/ABC123/My-App") == "ABC123"
    assert classify_figma_link("https://www.figma.com/design/Xy9/Flow") == "Xy9"
    assert classify_figma_link("https://www.figma.com/proto/Pr0t0/Demo") == "Pr0t0"
    assert classify_figma_link("https://www.figma.com/board/Bo4rd/Jam") == "Bo4rd"
    assert classify_figma_link("https://www.figma.com/files/recent") is None


def test_title_of_from_slug():
    assert title_of("https://www.figma.com/design/Xy9/Design-System?node-id=1-2") == "Design System"
    assert title_of("https://www.figma.com/file/K/My%20App") == "My App"
    assert title_of("https://www.figma.com/files/recent") is None


def test_design_node_id_stable_and_attrs():
    a = design_node("ABC123", url="https://www.figma.com/file/ABC123/x")
    b = design_node("ABC123")
    assert a.id == b.id and a.kind == "design" and a.name == "ABC123"
    assert a.attrs["url"].endswith("/file/ABC123/x")
    assert b.attrs == {}


# --- association -----------------------------------------------------------

def test_associate_designs_claims_and_classifies():
    nodes, edges = associate_designs(
        "group/app",
        links=[
            "https://www.figma.com/design/Xy9/Flow?node-id=12%3A34",
            "https://example.atlassian.net/browse/PROJ-1",  # foreign host, ignored
        ],
    )
    designs = [n for n in nodes if n.kind == "design"]
    assert len(designs) == 1 and designs[0].name == "Xy9"
    assert designs[0].attrs["title"] == "Flow"  # human name from the URL slug
    assert designs[0].attrs["node_id"] == "12%3A34"
    assert len(edges) == 1
    assert edges[0].relation == "designed_in"
    assert edges[0].confidence == Confidence.INFERRED


def test_associate_designs_dedupes():
    nodes, edges = associate_designs(
        "group/app",
        links=[
            "https://www.figma.com/file/K/A",
            "https://www.figma.com/file/K/A?node-id=1-2",  # same key
        ],
    )
    assert sum(1 for n in nodes if n.kind == "design") == 1
    assert sum(1 for n in nodes if n.kind == "repo") == 1
    assert len(edges) == 1


# --- connector plumbing ----------------------------------------------------

def test_spawn_with_command_and_auth_dir():
    c = FigmaConnector("f", mcp_command="figma-mcp --stdio", auth_dir="~/auth/figma")
    cmd, args, env = c._spawn()
    assert cmd == "figma-mcp" and args == ["--stdio"]
    assert env["MCP_REMOTE_CONFIG_DIR"] == os.path.expanduser("~/auth/figma")


def test_spawn_defaults_to_mcp_remote():
    cmd, args, env = FigmaConnector("f", mcp_url="https://mcp.example/figma")._spawn()
    assert cmd == "npx" and "mcp-remote@latest" in args
    assert "https://mcp.example/figma" in args and env is None


def test_verify_false_without_mcp():
    assert FigmaConnector("f").verify("ABC123") is False


def test_verify_true_via_mock(tmp_path):
    c = FigmaConnector("f", mcp_command="placeholder")
    c._spawn = lambda: (sys.executable, _server(tmp_path), None)
    assert c.verify("ABC123") is True
