"""Tests for the Atlassian connector: pure graph mapping, env plumbing, and
result parsing against a spawned mock MCP server (no network)."""

import os
import sys

from gitlab_sync.kb.connectors.atlassian import (
    AtlassianConnector,
    associate,
    claims,
    classify_link,
    external_node,
    link_edge,
    repo_node,
)
from gitlab_sync.kb.model import Confidence

# Mock MCP server exposing the two tools the connector calls.
_MOCK_SERVER = """
from mcp.server.fastmcp import FastMCP
m = FastMCP("mock-atlassian")

@m.tool()
def getAccessibleAtlassianResources() -> list[dict]:
    return [
        {"url": "https://example.atlassian.net", "id": "cloud-123", "name": "Example"},
        {"id": "no-url-skip"},
    ]

@m.tool()
def search(query: str) -> list[dict]:
    return [{"id": "X-1", "title": "Found " + query}]

m.run()
"""


def _server(tmp_path):
    p = tmp_path / "mock_atlassian.py"
    p.write_text(_MOCK_SERVER)
    return [str(p)]


def _connector(tmp_path):
    c = AtlassianConnector("test")
    c._spawn = lambda: (sys.executable, _server(tmp_path), None)
    return c


# --- live-ish parsing (mock server) ---------------------------------------

def test_discover_sites(tmp_path):
    assert _connector(tmp_path).discover_sites() == {
        "https://example.atlassian.net": "cloud-123"
    }


def test_search(tmp_path):
    assert _connector(tmp_path).search("foo") == [{"id": "X-1", "title": "Found foo"}]


# --- env plumbing ----------------------------------------------------------

def test_spawn_with_auth_dir():
    c = AtlassianConnector("t", auth_dir="~/auth/site-a")
    cmd, args, env = c._spawn()
    assert cmd == "npx"
    assert "mcp-remote@latest" in args
    assert env["MCP_REMOTE_CONFIG_DIR"] == os.path.expanduser("~/auth/site-a")


def test_spawn_without_auth_dir():
    _, _, env = AtlassianConnector("t")._spawn()
    assert env is None


# --- pure graph mapping ----------------------------------------------------

def test_repo_node():
    n = repo_node("group/app")
    assert n.kind == "repo" and n.name == "group/app" and n.repo == "group/app"


def test_external_node_drops_empty_attrs():
    full = external_node("issue", "PROJ-1", title="T", url="https://x/1", site="s")
    assert full.kind == "issue" and full.name == "PROJ-1"
    assert full.attrs == {"title": "T", "url": "https://x/1", "site": "s"}
    assert external_node("page", "P-9").attrs == {}


def test_external_node_id_is_stable():
    assert external_node("issue", "PROJ-1").id == external_node("issue", "PROJ-1").id
    assert external_node("issue", "PROJ-1").id != external_node("page", "PROJ-1").id


def test_link_edge():
    ext = external_node("issue", "PROJ-1")
    e = link_edge("group/app", ext, "tracked_by", "branch:feature/PROJ-1-x")
    assert e.src == repo_node("group/app").id
    assert e.dst == ext.id
    assert e.relation == "tracked_by"
    assert e.confidence == Confidence.INFERRED
    assert e.provenance.source_file == "branch:feature/PROJ-1-x"
    assert e.provenance.verified_at is not None


# --- URL claiming + classification -----------------------------------------

def test_claims_host_and_subdomain():
    hosts = ["example.atlassian.net"]
    assert claims("https://example.atlassian.net/browse/PROJ-1", hosts)
    assert claims("https://example.atlassian.net/wiki/x/Abc", hosts)
    assert not claims("https://www.figma.com/file/abc", hosts)
    assert not claims("not a url", hosts)


def test_classify_link():
    assert classify_link("https://x.atlassian.net/browse/PROJ-12") == ("issue", "PROJ-12")
    assert classify_link("https://x.atlassian.net/wiki/spaces/S/pages/98765/T") == ("page", "98765")
    assert classify_link("https://x.atlassian.net/wiki/x/Fc1bBw") == ("page", "Fc1bBw")
    assert classify_link("https://x.atlassian.net/dashboard") is None


# --- association (candidate graph from reference signals) ------------------

def test_associate_issue_keys_are_ambiguous():
    nodes, edges = associate("group/app", issue_keys=["PROJ-1", "PROJ-2"])
    assert {n.kind for n in nodes} == {"repo", "issue"}
    issue_edges = [e for e in edges if e.relation == "tracked_by"]
    assert len(issue_edges) == 2
    assert all(e.confidence == Confidence.AMBIGUOUS for e in issue_edges)


def test_associate_claims_only_own_links_and_classifies():
    nodes, edges = associate(
        "group/app",
        links=[
            "https://example.atlassian.net/browse/PROJ-9",
            "https://example.atlassian.net/wiki/spaces/S/pages/55/Home",
            "https://www.figma.com/file/abc",  # foreign host, ignored
        ],
        site_hosts=["example.atlassian.net"],
    )
    by_rel = {e.relation for e in edges}
    assert by_rel == {"tracked_by", "documented_by"}
    page = next(n for n in nodes if n.kind == "page")
    assert page.name == "55" and page.attrs["url"].endswith("/pages/55/Home")
    assert all(e.confidence == Confidence.INFERRED for e in edges)


def test_associate_dedupes_repo_and_edges():
    nodes, edges = associate(
        "group/app",
        issue_keys=["PROJ-1"],
        links=["https://example.atlassian.net/browse/PROJ-1"],
        site_hosts=["example.atlassian.net"],
    )
    assert sum(1 for n in nodes if n.kind == "repo") == 1
    assert sum(1 for n in nodes if n.kind == "issue") == 1
    # same (src,dst,relation) collapses to one edge despite two signals
    assert sum(1 for e in edges if e.relation == "tracked_by") == 1
