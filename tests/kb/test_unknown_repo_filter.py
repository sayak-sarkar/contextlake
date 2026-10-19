"""A `--repo` / `repo=` filter that names no repository is an error, not an empty answer.

`kb query --repo nope` printed "No matches" and exited 0; `kb impact` and `kb graph` blamed
the symbol; the MCP `find_dependents` returned `[]` with no note. Since 8.7.0 a repo id is
the `origin` remote or `name@<root commit>`, so the directory name a user types is often
not an id, and G2 and G6 both failed silently this way on 10.0.0rc1 (owner decision,
2026-10-15). The CLI now exits 1 with the ids to use; MCP answers with a note.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp import Client

from contextlake.cli import main
from contextlake.kb.cmds._common import _repo_id_suggestions, _unknown_repo_msg, repo_is_known
from contextlake.kb.embeddings.store import VectorStore
from contextlake.kb.model import Node, Repo
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "examples" / "fixtures" / "sample-graph.json"   # indexes as demo/app

APP = "github.com/acme/app"
TOOL = "tool@0123456789ab"          # a repo with no `origin`
PACKAGES = "(packages)"             # nodes, no repo row


def _node(node_id, repo, name, kind="function"):
    return Node(id=node_id, repo=repo, kind=kind, name=name, qualified_name=name,
                file="a.py", line_start=1, line_end=2, lang="python")


@pytest.fixture
def store(tmp_path):
    st = SqliteStore(tmp_path / "kb.sqlite")
    for rid in (APP, TOOL):
        st.upsert_repo(Repo(id=rid, path=str(tmp_path / rid.replace("/", "__"))))
    st.upsert_nodes(APP, [_node("app_charge", APP, "charge")])
    st.upsert_nodes(TOOL, [_node("tool_charge", TOOL, "charge")])
    st.upsert_nodes(PACKAGES, [_node("pkg_requests", PACKAGES, "requests", kind="package")])
    try:
        yield st
    finally:
        st.close()


# --- the helpers ------------------------------------------------------------------------

def test_a_repo_row_and_a_partition_with_nodes_are_known(store):
    assert repo_is_known(store, APP)
    assert repo_is_known(store, TOOL)
    assert repo_is_known(store, PACKAGES)        # no row, and `--repo (packages)` works
    assert not repo_is_known(store, "nope")
    assert not repo_is_known(store, "app")       # a name is not an id


@pytest.mark.parametrize("typed,expected", [
    ("app", APP),            # the last segment of an origin id
    ("tool", TOOL),          # the name part of name@<root commit>
    ("TOOL", TOOL),          # case does not matter for the name
])
def test_the_name_a_user_types_points_at_the_id(store, typed, expected):
    assert expected in _repo_id_suggestions(store, typed)


def test_the_name_match_is_an_equality_not_a_prefix(store):
    assert TOOL not in _repo_id_suggestions(store, "to")
    assert APP not in _repo_id_suggestions(store, "ap")


def test_with_no_match_the_message_lists_the_ids_and_names_no_missing_command(store):
    msg = _unknown_repo_msg(store, "zzzz")
    assert APP in msg and TOOL in msg
    # It sent the reader to `contextlake kb doctor`, which is not a command.
    assert "doctor" not in msg


def test_an_empty_store_says_so(tmp_path):
    st = SqliteStore(tmp_path / "empty.sqlite")
    try:
        msg = _unknown_repo_msg(st, "zzzz")
    finally:
        st.close()
    assert "holds no repositories" in msg and "doctor" not in msg


def test_a_scoped_key_sees_a_denied_repo_as_unknown_and_never_as_a_suggestion(store):
    from contextlake.kb.scoped_store import (
        ScopedStore,
        open_request_scope,
        reset_request_scope,
    )

    proxy = ScopedStore(store, lambda: ([APP], False))
    token = open_request_scope()
    try:
        assert repo_is_known(proxy, APP)
        assert not repo_is_known(proxy, TOOL)
        msg = _unknown_repo_msg(proxy, "tool")
        assert TOOL not in msg and "tool@" not in msg
        # The same answer as an id that does not exist, apart from the echoed argument,
        # which tells the caller nothing it did not send.
        absent = "no@000000000000"
        assert (_unknown_repo_msg(proxy, TOOL).replace(TOOL, "X")
                == _unknown_repo_msg(proxy, absent).replace(absent, "X"))
        assert "+" not in _unknown_repo_msg(proxy, "zzzz")    # the count is the visible one
    finally:
        reset_request_scope(token)


# --- the CLI ----------------------------------------------------------------------------

def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


@pytest.fixture
def cfg(tmp_path, capsys):
    path = tmp_path / "kb.toml"
    path.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    assert _run(["kb", "index", "--config", str(path), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    return str(path)


@pytest.mark.parametrize("argv", [
    ["kb", "query", "ForecastService", "--repo", "app"],
    ["kb", "impact", "ForecastService", "--repo", "app"],
    ["kb", "graph", "ForecastService", "--repo", "app", "--format", "json"],
])
def test_an_unknown_repo_exits_1_and_names_the_id(cfg, capsys, argv):
    assert _run([*argv, "--config", cfg]) == 1
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "Unknown repo 'app'" in text and "demo/app" in text


@pytest.mark.parametrize("argv", [
    ["kb", "query", "ForecastService", "--repo", "demo/app"],
    ["kb", "impact", "ForecastService", "--repo", "demo/app"],
    ["kb", "graph", "ForecastService", "--repo", "demo/app", "--format", "json"],
])
def test_a_known_repo_is_unchanged(cfg, capsys, argv):
    assert _run([*argv, "--config", cfg]) == 0
    captured = capsys.readouterr()
    assert "Unknown repo" not in captured.out + captured.err


@pytest.mark.parametrize("command", ["query", "impact"])
def test_json_carries_the_error_and_the_suggestions(cfg, capsys, command):
    assert _run(["kb", command, "ForecastService", "--repo", "app", "--json",
                 "--config", cfg]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "unknown_repo", "target": "app", "suggestions": ["demo/app"]}


# `--as-of` reads the stored snapshots, not the index, and it answered "No indexed snapshot"
# for a name that is not an id, so the user went looking for a wrong commit.

def test_an_unknown_repo_with_as_of_gets_the_unknown_repo_answer(cfg, capsys):
    assert _run(["kb", "query", "ForecastService", "--repo", "app", "--as-of", "abc123",
                 "--config", cfg]) == 1
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "Unknown repo 'app'" in text and "demo/app" in text
    assert "No indexed snapshot" not in text


def test_json_with_as_of_carries_the_same_error_and_suggestions(cfg, capsys):
    assert _run(["kb", "query", "ForecastService", "--repo", "app", "--as-of", "abc123",
                 "--json", "--config", cfg]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "unknown_repo", "target": "app", "suggestions": ["demo/app"]}


def test_a_known_repo_with_as_of_keeps_its_own_answers(cfg, capsys):
    assert _run(["kb", "query", "ForecastService", "--repo", "demo/app", "--as-of", "abc123",
                 "--config", cfg]) == 0
    assert "Unknown repo" not in "".join(capsys.readouterr())
    # A commit that was never indexed is still a missing snapshot, not an unknown repo.
    assert _run(["kb", "query", "ForecastService", "--repo", "demo/app", "--as-of", "ffff0000",
                 "--config", cfg]) == 1
    captured = capsys.readouterr()
    assert "No indexed snapshot of 'demo/app'" in captured.out + captured.err
    assert "Unknown repo" not in captured.out + captured.err
    assert _run(["kb", "query", "ForecastService", "--repo", "demo/app", "--as-of", "ffff0000",
                 "--json", "--config", cfg]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "no_snapshot"


def test_a_repo_with_history_and_no_index_row_is_not_unknown_to_as_of(cfg, capsys):
    """A named id keeps its snapshots after a later run replaces its row, so the index
    alone cannot say the repo never existed."""
    from contextlake.kb.store.shards import GraphShard, archive_shard

    archive_shard(Path(cfg).parent / "kb", GraphShard(
        repo="old/name", head_commit="c0ffee00" + "0" * 32, nodes=[], edges=[]))

    assert _run(["kb", "query", "ForecastService", "--repo", "old/name", "--as-of", "ffff0000",
                 "--config", cfg]) == 1
    captured = capsys.readouterr()
    assert "No indexed snapshot of 'old/name'" in captured.out + captured.err
    assert "Unknown repo" not in captured.out + captured.err


def test_as_of_does_not_create_an_index_to_ask_about_an_unknown_repo(tmp_path, capsys):
    """With no index file the store holds no repositories, and `--as-of` says so in the
    words a search gives, without making the file."""
    path = tmp_path / "kb.toml"
    path.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    (tmp_path / "kb").mkdir()
    sentence = ("Unknown repo 'app': this store holds no repositories. "
                "Run `contextlake kb index <path>` first.")

    assert _run(["kb", "query", "x", "--repo", "app", "--as-of", "abc123",
                 "--config", str(path)]) == 1
    captured = capsys.readouterr()
    assert sentence in captured.out + captured.err
    assert "No indexed snapshot" not in captured.out + captured.err
    assert not (tmp_path / "kb" / "index.sqlite").exists()

    assert _run(["kb", "query", "x", "--repo", "app", "--as-of", "abc123", "--json",
                 "--config", str(path)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "unknown_repo", "target": "app", "suggestions": []}
    assert not (tmp_path / "kb" / "index.sqlite").exists()

    # The search without `--as-of` gives the same sentence (and makes the file).
    assert _run(["kb", "query", "x", "--repo", "app", "--config", str(path)]) == 1
    captured = capsys.readouterr()
    assert sentence in captured.out + captured.err


# --- the MCP tools ----------------------------------------------------------------------

class _FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


CALLS = {
    "search_code": {"query": "charge"},
    "find_definition": {"name": "charge"},
    "find_dependents": {"package": "requests"},
    "semantic_search": {"query": "charge"},
    "hybrid_search": {"query": "charge"},
    "ask": {"question": "where is charge defined"},
}


def _call_all(server, repo):
    async def go():
        async with Client(server) as client:
            out = {}
            for tool, args in CALLS.items():
                r = await client.call_tool(tool, {**args, "repo": repo})
                out[tool] = (r.is_error, r.structured_content)
            return out

    return asyncio.run(go())


@pytest.fixture
def server(store, tmp_path):
    vectors = VectorStore(tmp_path / "embeddings.sqlite")
    vectors.upsert([("app_charge", APP, [1.0, 0.0]), ("tool_charge", TOOL, [1.0, 0.0])])
    try:
        yield build_server(store, embedder=_FakeEmbedder(), vector_store=vectors)
    finally:
        vectors.close()


def test_every_repo_filter_tool_answers_an_unknown_id_with_a_note(server):
    for tool, (is_error, out) in _call_all(server, "tool").items():
        assert not is_error, tool
        assert out["note"].startswith("Unknown repo 'tool'"), (tool, out["note"])
        assert TOOL in out["note"], tool
        assert not out.get("nodes"), tool


def test_a_known_id_gets_no_unknown_repo_note(server):
    for tool, (is_error, out) in _call_all(server, APP).items():
        assert not is_error, tool
        assert "Unknown repo" not in (out.get("note") or ""), tool
