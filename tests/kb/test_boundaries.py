"""Boundary and null-value coverage for limit/hops/max_* parameters, plus
"the least amount of data possible" repo shapes (RC-P1-7 / T-2, T-6, T-7).

Covers, at the standard boundary set {0, 1, default-1, default, default+1, a
very large value}:

  - every MCP tool that takes a `limit`/`hops`/`max_hops` (server.py);
  - `extract_subgraph`/`repo_subgraph`'s `hops`/`max_nodes`/`max_fanout`/
    `max_edges` (visualize/payload.py, the graph-export "payload builders");
  - `compute_owners`'s limit clamp (ownership.py, reached via the `who_knows`
    MCP tool);

plus the null/empty shapes named in the task brief:

  - an empty repo (indexed, zero nodes) and a single-file repo;
  - a file exactly at `[kb] max_file_bytes` (parse.py's oversize cutoff is a
    strict `>`, so exactly-at-the-limit must still be indexed);
  - a Node with `file`/`line_start`/`qualified_name` all `None`, flowing
    through the payload builders, every diagram renderer, and the MCP output
    models (`NodeOut`).

No cell is expected to raise; a cell that legitimately can't find anything
(hops=0, an out-of-range clamp, an empty repo) must degrade to an empty/None
result, never an exception. Where a tool call goes over MCP, "no unhandled
exception" is checked via `not result.is_error` -- the MCP SDK converts an
in-process exception into an error *result*, so that (not a raised Python
exception in this test process) is the correct signal to assert on.

Offline/hermetic: `who_knows`'s boundary test monkeypatches
`ownership.subprocess.run` directly (ownership.py imports `subprocess` itself,
not via `contextlake.core`, so the shared `fake_subprocess` fixture doesn't
reach it) -- no real git process is spawned anywhere in this file.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from contextlake.kb import ownership as ownership_mod
from contextlake.kb import visualize as viz
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.parse import index_repo_dir
from contextlake.kb.server import build_server
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.wiki.generate import repo_brief


def _boundaries(default: int, *, floor: int = 0) -> list[int]:
    """{floor, floor+1, default-1, default, default+1, a very large value},
    deduped and sorted -- the standard boundary set this task specifies,
    applied to whatever the real default is (read from the source, not
    reproduced by hand per call site)."""
    candidates = {floor, floor + 1, default - 1, default, default + 1, default * 1000 + 7}
    return sorted(v for v in candidates if v >= floor)


async def _call(server, tool, args):
    from mcp import Client

    async with Client(server) as client:
        return await client.call_tool(tool, args)


def _unwrap(structured):
    if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
        return structured["result"]
    return structured


# ---------------------------------------------------------------------------
# A shared, module-scoped graph big enough to make truncation boundaries
# (limit ~50, hops ~3-6) meaningful, built once and reused read-only by every
# MCP-tool boundary case below.
# ---------------------------------------------------------------------------

_PROV = Provenance(source_file="f.py", source_line=1, verified_at=date(2026, 6, 21))


def _edge(src, dst, relation="calls"):
    return Edge(src=src, dst=dst, relation=relation, confidence=Confidence.EXTRACTED,
               provenance=_PROV)


def _seed_boundary_graph(store: SqliteStore) -> None:
    # A hub called by 60 leaves -- exercises find_callers/get_neighbors limit
    # boundaries around their default of 50 (so 49/50/51 are all meaningfully
    # different truncation states, not all "well under the cap").
    store.upsert_nodes("hub-repo", [Node(id="H", repo="hub-repo", kind="function", name="Hub")]
                       + [Node(id=f"L{i}", repo="hub-repo", kind="function", name=f"Leaf{i}")
                          for i in range(60)])
    store.upsert_edges("hub-repo", [_edge(f"L{i}", "H") for i in range(60)])

    # A 21-node linear call chain c0 -> c1 -> ... -> c20, for blast_radius's
    # `hops` (reverse reach) and shortest_path's `max_hops`.
    chain = [Node(id=f"c{i}", repo="chain-repo", kind="function", name=f"c{i}")
             for i in range(21)]
    store.upsert_nodes("chain-repo", chain)
    store.upsert_edges("chain-repo", [_edge(f"c{i}", f"c{i + 1}") for i in range(20)])

    # A package depended on by 60 consumers, for find_dependents's limit.
    store.upsert_nodes("(packages)", [Node(id="pkg:widgetlib", repo="(packages)",
                                           kind="package", name="widgetlib")])
    store.upsert_nodes("dep-repo", [Node(id=f"consumer{i}", repo="dep-repo", kind="file",
                                        name=f"consumer{i}.py") for i in range(60)])
    store.upsert_edges("dep-repo", [_edge(f"consumer{i}", "pkg:widgetlib", "depends_on")
                                    for i in range(60)])

    # 30 searchable nodes, for search_code's limit.
    store.upsert_nodes("search-repo", [Node(id=f"w{i}", repo="search-repo", kind="class",
                                            name=f"Widget{i}") for i in range(30)])

    # A handful of repos, for list_repos's limit.
    for i in range(5):
        store.upsert_repo(Repo(id=f"team/repo{i}", path=f"/repos/repo{i}"))


@pytest.fixture(scope="module")
def boundary_server(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("boundaries")
    store = SqliteStore(tmp_path / "kb.sqlite")
    _seed_boundary_graph(store)
    server = build_server(store)
    yield server
    store.close()


# --- MCP tool limit/hops boundaries ------------------------------------------
#
# (tool, fixed args, boundary param name, the tool's own default). The default
# ints below are mirrored BY HAND from server.py's tool signatures -- the tool
# functions are closures defined inside build_server(), so `inspect.signature`
# can't reach them without first building a server, and doing that at
# collection time (module import) is more machinery than the payoff justifies.
# `test_param_table_defaults_match_the_live_tool_schemas` below is the actual
# drift guard: it reads each default back out of the live MCP tool schema at
# runtime and fails loudly if this table falls out of sync with server.py.
_PARAM_TABLE = [
    ("get_neighbors", {"node_id": "H"}, "limit", 50),
    ("find_callers", {"node_id": "H"}, "limit", 50),
    ("find_dependents", {"package": "widgetlib"}, "limit", 50),
    ("search_code", {"query": "widget"}, "limit", 20),
    ("list_repos", {}, "limit", 500),
    ("blast_radius", {"node_id": "c20"}, "hops", 3),
    ("blast_radius", {"node_id": "c20", "hops": 25}, "limit", 100),
    ("shortest_path", {"src_id": "c0", "dst_id": "c20"}, "max_hops", 6),
]

_BOUNDARY_CASES = [
    pytest.param(tool, fixed, param, value,
                 id=f"{tool}-{param}={value}")
    for tool, fixed, param, default in _PARAM_TABLE
    for value in _boundaries(default)
]


async def _list_tools(server):
    from mcp import Client

    async with Client(server) as client:
        return await client.list_tools()


def test_param_table_defaults_match_the_live_tool_schemas(boundary_server):
    """The drift guard for `_PARAM_TABLE`'s hand-mirrored defaults: reads each
    tool's REAL default back out of the live MCP tool schema (built straight
    from the `@mcp.tool()`-decorated function signatures in server.py) and
    fails loudly if this file's copy has drifted -- e.g. someone bumps
    `get_neighbors(limit=50)` to `100` without updating this table, which would
    otherwise silently narrow "at the default" to a stale, no-longer-true value
    instead of the actual one."""
    tools = {t.name: t for t in asyncio.run(_list_tools(boundary_server)).tools}
    for tool, _fixed, param, default in _PARAM_TABLE:
        props = tools[tool].input_schema["properties"]
        assert props[param]["default"] == default, (
            f"{tool}'s real default for {param!r} is "
            f"{props[param]['default']!r}, not the {default!r} this table assumes"
        )


@pytest.mark.parametrize("tool,fixed,param,value", _BOUNDARY_CASES)
def test_mcp_tool_param_boundaries_never_raise(boundary_server, tool, fixed, param, value):
    args = dict(fixed)
    args[param] = value
    res = asyncio.run(_call(boundary_server, tool, args))
    assert not res.is_error, f"{tool}({args}) errored: {getattr(res, 'content', res)}"


# --- who_knows / compute_owners's limit clamp --------------------------------

def _fake_git_log_stdout(n_authors: int) -> str:
    """A synthetic `git log --numstat` stream with `n_authors` distinct
    contributors -- more than the clamp's ceiling of 50, so the clamp is
    actually exercised (not vacuously true because there was nothing to cap)."""
    us = "\x1f"
    lines = []
    ts = 1_750_000_000
    for i in range(n_authors):
        lines.append(f"{us}Author{i}{us}author{i}@example.com{us}{ts + i}")
        lines.append("5\t2\tfile.py")
    return "\n".join(lines)


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.parametrize("limit", _boundaries(10))  # who_knows's own default is 10
def test_who_knows_limit_clamp_boundaries_never_raise(monkeypatch, tmp_path, limit):
    """compute_owners clamps its caller's `limit` to `max(1, min(limit, 50))`
    (server.py's who_knows tool) regardless of how far out of range it is --
    covered here across the boundary set, offline (no real `git` spawned)."""
    monkeypatch.setattr(
        ownership_mod.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=_fake_git_log_stdout(60)))

    store = SqliteStore(tmp_path / "kb.sqlite")
    try:
        store.upsert_repo(Repo(id="team/api", path="/fake/repo"))
        server = build_server(store)
        res = asyncio.run(_call(server, "who_knows", {"repo": "team/api", "limit": limit}))
        assert not res.is_error
        out = _unwrap(res.structured_content)
        assert 0 <= len(out["owners"]) <= 50
    finally:
        store.close()


# --- extract_subgraph / repo_subgraph boundaries -----------------------------

@pytest.fixture
def dense_store(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    nodes = [Node(id="H", repo="r", kind="class", name="Hub")] + [
        Node(id=f"n{i}", repo="r", kind="function", name=f"n{i}") for i in range(80)
    ]
    store.upsert_nodes("r", nodes)
    store.upsert_edges("r", [_edge("H", f"n{i}") for i in range(80)])
    yield store
    store.close()


@pytest.mark.parametrize("hops", _boundaries(2))  # extract_subgraph's own default
def test_extract_subgraph_hops_boundaries_never_raise(dense_store, hops):
    nodes, edges = viz.extract_subgraph(dense_store, ["H"], hops=hops, max_nodes=500,
                                        max_fanout=1000)
    assert isinstance(nodes, list) and isinstance(edges, list)


@pytest.mark.parametrize("max_nodes", _boundaries(500))
def test_extract_subgraph_max_nodes_boundaries_never_raise(dense_store, max_nodes):
    nodes, edges = viz.extract_subgraph(dense_store, ["H"], hops=1, max_nodes=max_nodes,
                                        max_fanout=1000)
    assert isinstance(nodes, list) and isinstance(edges, list)


@pytest.mark.parametrize("max_fanout", _boundaries(50))
def test_extract_subgraph_max_fanout_boundaries_never_raise(dense_store, max_fanout):
    nodes, edges = viz.extract_subgraph(dense_store, ["H"], hops=1, max_nodes=500,
                                        max_fanout=max_fanout)
    assert isinstance(nodes, list) and isinstance(edges, list)


@pytest.mark.parametrize("max_nodes", _boundaries(500))
def test_repo_subgraph_max_nodes_boundaries_never_raise(dense_store, max_nodes):
    nodes, edges = viz.repo_subgraph(dense_store, "r", max_nodes=max_nodes)
    assert isinstance(nodes, list) and isinstance(edges, list)


@pytest.mark.parametrize("max_edges", _boundaries(400))
def test_repo_subgraph_max_edges_boundaries_never_raise(dense_store, max_edges):
    nodes, edges = viz.repo_subgraph(dense_store, "r", max_nodes=500, max_edges=max_edges)
    assert isinstance(nodes, list) and isinstance(edges, list)


# --- empty repo / single-file repo -------------------------------------------

def test_empty_repo_never_raises_across_repo_brief_and_mcp(tmp_path):
    """An indexed repo with zero nodes/edges (not an unindexed one -- that's
    already covered by test_kb_server.py's found=False case): a real shard
    exists, it's just empty."""
    write_shard(tmp_path, GraphShard(repo="empty/repo", head_commit="h0", nodes=[], edges=[]))
    store = SqliteStore(tmp_path / "kb.sqlite")
    try:
        store.upsert_repo(Repo(id="empty/repo", path=str(tmp_path), head_commit="h0"))
        reindex_shard(store, tmp_path, "empty/repo")

        brief = repo_brief(tmp_path, "empty/repo")
        assert brief is not None and brief["node_count"] == 0 and brief["edge_count"] == 0

        server = build_server(store)
        res = asyncio.run(_call(server, "get_repo_brief", {"repo": "empty/repo"}))
        assert not res.is_error
        out = _unwrap(res.structured_content)
        assert out["found"] is True and out["node_count"] == 0

        health = asyncio.run(_call(server, "graph_health", {}))
        assert not health.is_error
    finally:
        store.close()


def test_single_file_repo_never_raises_across_repo_brief_and_mcp(tmp_path):
    nodes = [Node(id="only.py", repo="solo/repo", kind="file", name="only.py")]
    write_shard(tmp_path, GraphShard(repo="solo/repo", head_commit="h1", nodes=nodes, edges=[]))
    store = SqliteStore(tmp_path / "kb.sqlite")
    try:
        store.upsert_repo(Repo(id="solo/repo", path=str(tmp_path), head_commit="h1"))
        reindex_shard(store, tmp_path, "solo/repo")

        brief = repo_brief(tmp_path, "solo/repo")
        assert brief is not None and brief["node_count"] == 1

        server = build_server(store)
        res = asyncio.run(_call(server, "get_repo_brief", {"repo": "solo/repo"}))
        assert not res.is_error
        assert _unwrap(res.structured_content)["node_count"] == 1
    finally:
        store.close()


# --- a file exactly at [kb] max_file_bytes -----------------------------------

def _python_file_of_size(path: Path, class_name: str, target_bytes: int) -> None:
    """Write a syntactically valid Python file of exactly `target_bytes` bytes
    (a class definition, padded to size with trailing comment lines)."""
    header = f"class {class_name}:\n    pass\n"
    pad_needed = max(0, target_bytes - len(header.encode()))
    # "# x\n" is 4 bytes; pad with whole lines, then top up with a final
    # partial-length comment so the file lands on the exact byte count.
    full_lines = pad_needed // 4
    remainder = pad_needed - full_lines * 4
    body = header + ("# x\n" * full_lines) + ("#" * remainder)
    data = body.encode()
    assert len(data) == target_bytes, (len(data), target_bytes)
    path.write_bytes(data)


def test_file_exactly_at_max_file_bytes_is_indexed_not_skipped(tmp_path):
    """parse.py's cutoff is `if fpath.stat().st_size > max_file_bytes: skip` --
    strictly greater-than, so a file of EXACTLY max_file_bytes must still be
    indexed. max_file_bytes+1 must be the first size actually skipped."""
    limit = 300
    _python_file_of_size(tmp_path / "at_limit.py", "AtLimit", limit)
    _python_file_of_size(tmp_path / "under_limit.py", "UnderLimit", limit - 1)
    _python_file_of_size(tmp_path / "over_limit.py", "OverLimit", limit + 1)

    shard = index_repo_dir(str(tmp_path), "demo/app", max_file_bytes=limit)
    names = {n.name for n in shard.nodes}
    assert "AtLimit" in names       # == limit -> NOT > limit -> indexed
    assert "UnderLimit" in names
    assert "OverLimit" not in names  # > limit -> skipped


# --- nodes with file/line_start/qualified_name all None ---------------------
#
# Every node below has file=None/line_start=None/qualified_name=None (Node's
# own defaults -- see model.py), but the set as a whole is shaped to actually
# REACH each kind-gated renderer's field accesses rather than hit its early-out
# guidance string: to_class_diagram needs a classifier (class/interface) plus
# an `inherits` edge, to_er_diagram needs table/view nodes plus a `references`
# edge, to_deployment_diagram needs a `resource`-kind node with lang="hcl", and
# to_sequence_diagram needs `meta.seed_ids` plus a `calls` edge from the seed.
# A renderer given only a plain "function" node (as a single node/edge-free
# payload would be) never dereferences the null fields its own gate excludes,
# so testing null-safety meaningfully requires reaching past every gate once.

_NULL_NODE = Node(id="mystery", repo="r", kind="function", name="mystery")
_NULL_STATE_NODE = Node(id="mystery-state", repo="r", kind="state", name="Unknown")
_NULL_IFACE = Node(id="IFace", repo="r", kind="interface", name="IFace")
_NULL_IMPL = Node(id="Impl", repo="r", kind="class", name="Impl")
_NULL_METHOD = Node(id="Impl.run", repo="r", kind="method", name="run")
_NULL_TABLE = Node(id="orders", repo="r", kind="table", name="orders")
_NULL_VIEW = Node(id="customers", repo="r", kind="view", name="customers")
_NULL_RESOURCE = Node(id="aws_s3_bucket.x", repo="r", kind="resource",
                      name="aws_s3_bucket.x", lang="hcl")
_NULL_CALLER = Node(id="caller", repo="r", kind="function", name="caller")
_NULL_CALLEE = Node(id="callee", repo="r", kind="function", name="callee")

_ALL_NULL_NODES = [
    _NULL_NODE, _NULL_STATE_NODE, _NULL_IFACE, _NULL_IMPL, _NULL_METHOD,
    _NULL_TABLE, _NULL_VIEW, _NULL_RESOURCE, _NULL_CALLER, _NULL_CALLEE,
]
_ALL_NULL_EDGES = [
    _edge("Impl", "IFace", "inherits"),
    _edge("Impl", "Impl.run", "contains"),
    _edge("orders", "customers", "references"),
    _edge("caller", "callee", "calls"),
]


def test_null_node_fields_flow_through_payload_and_diagram_builders(tmp_path):
    for node in _ALL_NULL_NODES:
        assert node.file is None and node.line_start is None and node.qualified_name is None

    payload = viz.to_payload(_ALL_NULL_NODES, _ALL_NULL_EDGES,
                             {"seed_ids": ["caller"]})
    assert payload["nodes"][0]["file"] is None
    assert payload["nodes"][0]["qualified_name"] is None
    assert payload["nodes"][0]["line"] is None  # line_start -> "line" in the payload dict

    renderers = [
        viz.to_json, viz.to_dot, viz.to_mermaid, viz.to_graphml, viz.to_cypher,
        viz.to_class_diagram, viz.to_sequence_diagram, viz.to_state_diagram,
        viz.to_er_diagram, viz.to_deployment_diagram,
    ]
    for render in renderers:
        out = render(payload)
        assert isinstance(out, str)

    # Confirm the gated renderers actually did real work, not just their
    # early-out guidance string -- otherwise this test would pass whether or
    # not the null fields were ever dereferenced.
    assert "IFace" in viz.to_class_diagram(payload) and "Impl" in viz.to_class_diagram(payload)
    assert "orders" in viz.to_er_diagram(payload) and "customers" in viz.to_er_diagram(payload)
    assert "aws_s3_bucket" in viz.to_deployment_diagram(payload)
    seq = viz.to_sequence_diagram(payload)
    assert "caller" in seq and "callee" in seq

    elements = viz._cytoscape_elements(payload)
    assert isinstance(elements, list) and len(elements) > 0


def test_null_node_fields_flow_through_the_mcp_output_model(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    try:
        store.upsert_nodes("r", [_NULL_NODE])
        server = build_server(store)
        res = asyncio.run(_call(server, "get_node", {"node_id": "mystery"}))
        assert not res.is_error
        out = _unwrap(res.structured_content)
        assert out["file"] is None and out["qualified_name"] is None
        assert out["line_start"] is None and out["line_end"] is None
    finally:
        store.close()
