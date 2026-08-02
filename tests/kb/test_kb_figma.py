"""Tests for the Figma connector: pure URL classification + association, env
plumbing, and best-effort metadata fetch against a spawned mock MCP server."""

import os
import sys

import contextlake.kb.connectors.orchestrate as orch
from contextlake.kb.connectors.figma import (
    FigmaConnector,
    associate_designs,
    classify_figma_link,
    design_node,
    match_frame_names_to_symbols,
    parse_metadata,
    title_of,
)
from contextlake.kb.model import Confidence, Node
from contextlake.kb.store.sqlite_store import SqliteStore

_MOCK_SERVER = """
from mcp.server.mcpserver import MCPServer
m = MCPServer("mock-figma")

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


def test_fetch_metadata_none_without_mcp():
    assert FigmaConnector("f").fetch_metadata("ABC123") is None


def test_fetch_metadata_via_mock(tmp_path):
    c = FigmaConnector("f", mcp_command="placeholder")
    c._spawn = lambda: (sys.executable, _server(tmp_path), None)
    assert c.fetch_metadata("ABC123") == {"name": "Design System", "key": "ABC123"}


# --- deeper enrichment: real metadata, not just a reachability flag ---------

def test_parse_metadata_from_dict():
    assert parse_metadata({"name": "Design System", "key": "X"}) == {"name": "Design System"}
    assert parse_metadata({"key": "X"}) == {}  # no name -> nothing to add


def test_parse_metadata_from_xml_string():
    xml = '<frame name="Login"><group name="Form"/></frame><frame name="Dashboard"/>'
    assert parse_metadata(xml) == {"structure": ["Login", "Form", "Dashboard"]}


def test_parse_metadata_xml_bounded_by_max_names():
    xml = "".join(f'<node name="N{i}"/>' for i in range(20))
    assert len(parse_metadata(xml, max_names=8)["structure"]) == 8


def test_parse_metadata_unrecognized_shape_is_empty():
    assert parse_metadata(None) == {}
    assert parse_metadata(42) == {}


# --- match_frame_names_to_symbols: name-match designs to code --------------

def test_match_frame_names_to_symbols_matches_case_insensitively():
    store = SqliteStore(":memory:")
    try:
        store.upsert_nodes("team/api", [
            Node(id="team_api_payer", repo="team/api", kind="class", name="Payer", file="pay.py"),
        ])
        matches = match_frame_names_to_symbols(
            store, "team/api", {"structure": ["payer", "Unrelated"]})
        assert matches == [("team_api_payer", Confidence.AMBIGUOUS)]
    finally:
        store.close()


def test_match_frame_names_to_symbols_handles_missing_attrs():
    store = SqliteStore(":memory:")
    try:
        assert match_frame_names_to_symbols(store, "team/api", {}) == []
    finally:
        store.close()


def test_match_frame_names_to_symbols_ignores_non_symbol_kinds():
    store = SqliteStore(":memory:")
    try:
        store.upsert_nodes("team/api", [
            Node(id="team_api_payer_py", repo="team/api", kind="file", name="Payer", file="Payer"),
        ])
        assert match_frame_names_to_symbols(store, "team/api", {"name": "Payer"}) == []
    finally:
        store.close()


def test_match_frame_names_to_symbols_scopes_to_repo():
    store = SqliteStore(":memory:")
    try:
        store.upsert_nodes("other/repo", [
            Node(id="other_payer", repo="other/repo", kind="class", name="Payer", file="pay.py"),
        ])
        assert match_frame_names_to_symbols(store, "team/api", {"name": "Payer"}) == []
    finally:
        store.close()


# --- enrich_repo_figma: designs link to matching code symbols --------------

class _FigmaMetaStub:
    hosts = ("figma.com",)

    def __init__(self, meta):
        self._meta = meta

    def fetch_metadata(self, file_key, **kw):
        return self._meta


def test_enrich_repo_figma_links_design_to_matching_symbol():
    store = SqliteStore(":memory:")
    try:
        store.upsert_nodes("team/api", [
            Node(id="team_api_payer", repo="team/api", kind="class", name="Payer", file="pay.py"),
        ])
        conn = _FigmaMetaStub({"name": "Payer"})
        nodes, edges = orch.enrich_repo_figma(
            conn, "team/api", store, links=["https://www.figma.com/design/Xy9/Flow"])
        design = next(n for n in nodes if n.kind == "design")
        symbol_edges = [
            e for e in edges if e.relation == "designed_in" and e.src == "team_api_payer"
        ]
        assert len(symbol_edges) == 1
        assert symbol_edges[0].dst == design.id
        assert symbol_edges[0].confidence == Confidence.AMBIGUOUS
        # the repo-level fallback edge from associate_designs is not duplicated
        repo_edges = [e for e in edges if e.src == "repo_team_api"]
        assert len(repo_edges) == 1
    finally:
        store.close()
