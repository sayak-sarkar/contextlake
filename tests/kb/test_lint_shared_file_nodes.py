"""`kb lint` reports file nodes that stand for more than one file.

File node ids are ``make_id(repo_id, rel_path)``, and ``make_id`` casefolds and folds every
run of non-word characters to ``_``. So ``a-b.py``, ``a_b.py`` and ``A_B.py`` in one repo
share ONE node, and so do the repos ``grp/a-b`` and ``grp/a/b``. The store keeps one row per
id, so the other files are missing from the graph. The id scheme is a known limitation and
this check changes no id. It tells a user whether their own store is affected.

What this file pins:

- the finding names the repo and every colliding path, from a store built by the real
  ``kb index`` (a workspace with one colliding repo and one clean repo);
- the clean repo is never named, and a repo that exercises many file-node producers
  (python, sql, xml config, package.json, C++ header and source) is not a false positive;
- the cross-repo shape is measurable and is labelled cross-repo;
- each guard in the query has a case that only it stops: the ``kind = 'file'`` filter (a C++
  class is contained from two files and is not a file node), the ``relation = 'contains'``
  filter (a connector edge from a file node names a URL), and the node-row clause (the node
  row names a file that has no edges of its own);
- an unanswerable check reads as null, never as 0;
- the finding is advisory: the exit code stays 0.

Precondition for every test: HOME is monkeypatched and the config names a store under
``tmp_path``, so no ambient store or ambient ``kb.toml`` is read.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date

import pytest

from contextlake import cli
from contextlake.kb.cmds import lint as lint_mod
from contextlake.kb.cmds.lint import SHARED_FILE_NODES_DOC, shared_file_nodes
from contextlake.kb.commands import cmd_index, cmd_lint
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.store.sqlite_store import SqliteStore

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture
def logs():
    """Capture contextlake log messages straight off the named logger."""
    logger = logging.getLogger("contextlake")
    saved = logger.handlers[:]
    logger.handlers.clear()
    messages: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield messages
    logger.handlers[:] = saved


def _repo(path, files):
    path.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(body, encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "c"]):
        subprocess.run(["git", *args], cwd=path, env=_ENV, check=True, capture_output=True)


def _config(tmp_path):
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n[embeddings]\nenabled = false\n')
    return store_dir, cfg


def _index_workspace(cfg, ws):
    assert cmd_index(cli.build_parser().parse_args(
        ["kb", "index", "--config", str(cfg), "--workspace", str(ws), "--no-docs"])) == 0


def _index_source(cfg, path, repo_id):
    assert cmd_index(cli.build_parser().parse_args(
        ["kb", "index", "--config", str(cfg), "--source", str(path), "--repo", repo_id,
         "--no-docs"])) == 0


def _lint_json(cfg, capsys):
    capsys.readouterr()
    rc = cmd_lint(cli.build_parser().parse_args(["kb", "lint", "--config", str(cfg), "--json"]))
    return rc, json.loads(capsys.readouterr().out)


def _repo_ids(store_dir):
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        return {r.id for r in store.list_repos()}
    finally:
        store.close()


def _prefilter(store):
    """The rows the SQL step flags, before the per-node paths are read.

    The second step rebuilds each node's path set in Python and drops a node with fewer
    than two, so a loose SQL step still gives the right ANSWER. It does not give the right
    COST: every extra flagged node is another read. Asserting the SQL step is exact is what
    keeps that cost bounded, and it is the only assertion a mistake in the SQL filters
    (kind, relation, repo in the pair) cannot hide behind the second step from."""
    return store.conn.execute(lint_mod._SHARED_FILE_NODES_SQL).fetchall()


# --- a store built by the real `kb index` -----------------------------------------------

def test_colliding_names_in_one_repo_are_named_and_the_clean_repo_is_not(
        tmp_path, monkeypatch, capsys, logs):
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "dup", {"a-b.py": "def one():\n    return 1\n",
                       "a_b.py": "def two():\n    return 2\n",
                       "A_B.py": "def three():\n    return 3\n"})
    _repo(ws / "clean", {"m.py": "def ok():\n    return 1\n",
                         "n.py": "def ok2():\n    return 2\n"})
    store_dir, cfg = _config(tmp_path)
    _index_workspace(cfg, ws)
    ids = _repo_ids(store_dir)
    dup_id = next(i for i in ids if i.startswith("dup"))
    clean_id = next(i for i in ids if i.startswith("clean"))

    rc, payload = _lint_json(cfg, capsys)

    assert rc == 0, "advisory: a shared file node must not change the exit code"
    assert payload["shared_file_nodes"] == 1
    assert payload["shared_file_node_repos"] == [dup_id]
    [finding] = payload["shared_file_nodes_sample"]
    assert finding["repos"] == [dup_id]
    assert finding["cross_repo"] is False
    assert {f["path"] for f in finding["files"]} == {"a-b.py", "a_b.py", "A_B.py"}
    assert {f["repo"] for f in finding["files"]} == {dup_id}
    assert clean_id not in json.dumps(payload["shared_file_nodes_sample"])

    # The text a person reads: repo, every path, what it means, where to read more.
    logs.clear()
    rc = cmd_lint(cli.build_parser().parse_args(["kb", "lint", "--config", str(cfg)]))
    assert rc == 0
    lines = [m for m in logs if "shared file node" in m and "Lint:" not in m]
    assert len(lines) == 1, lines
    line = lines[0]
    assert dup_id in line
    for path in ("a-b.py", "a_b.py", "A_B.py"):
        assert path in line
    assert "share one node" in line and "2 of them are missing from the graph" in line
    assert "known id limitation" in line and SHARED_FILE_NODES_DOC in line
    assert clean_id not in line
    summary = next(m for m in logs if "Lint:" in m)
    assert "1 shared file node(s)" in summary


def test_a_repo_that_exercises_many_file_node_producers_is_not_reported(
        tmp_path, monkeypatch, capsys):
    """False-positive guard on the real producers: code, sql, xml config, a manifest
    and a C++ header and source that declare the same class (a C/C++ external-linkage
    symbol drops the file from its id, so its `contains` edges come from two files)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "rich", {
        "m.py": "def helper():\n    return 1\n",
        "n.py": "def helper():\n    return 2\n",
        "schema.sql": "CREATE TABLE orders (id INT);\n",
        "app.config": '<configuration><appSettings><add key="Timeout" value="5"/>'
                      "</appSettings></configuration>\n",
        "package.json": '{"name": "x", "scripts": {"build": "tsc", "test": "jest"}}\n',
        "widget.hpp": "class Widget {\n public:\n  void draw();\n};\n",
        "widget.cpp": '#include "widget.hpp"\nvoid Widget::draw() {}\n'
                      "class Widget;\n",
    })
    store_dir, cfg = _config(tmp_path)
    _index_workspace(cfg, ws)

    rc, payload = _lint_json(cfg, capsys)

    assert rc == 0
    # The store holds the producers this test claims to cover.
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        kinds = {r[0] for r in store.conn.execute("SELECT DISTINCT kind FROM nodes")}
        files = {r[0] for r in store.conn.execute(
            "SELECT name FROM nodes WHERE kind = 'file'")}
    finally:
        store.close()
    assert {"file"} <= kinds
    assert {"m.py", "n.py", "schema.sql", "package.json", "widget.hpp"} <= files, files
    assert payload["shared_file_nodes"] == 0
    assert payload["shared_file_nodes_sample"] == []
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        assert _prefilter(store) == []
    finally:
        store.close()


def test_repos_whose_ids_differ_only_in_punctuation_are_reported_as_cross_repo(
        tmp_path, monkeypatch, capsys, logs):
    """`grp/a-b` and `grp/a/b` fold to one id, so the second index takes the file node
    from the first. Both repos' `contains` edges still point at it, and each carries its
    own repo_id: that is what makes this measurable from the tables."""
    monkeypatch.setenv("HOME", str(tmp_path))
    first, second = tmp_path / "src1", tmp_path / "src2"
    _repo(first, {"app.py": "def alpha():\n    return 1\n"})
    _repo(second, {"app.py": "def beta():\n    return 2\n"})
    store_dir, cfg = _config(tmp_path)
    _index_source(cfg, first, "example.invalid/grp/a-b")
    _index_source(cfg, second, "example.invalid/grp/a/b")

    rc, payload = _lint_json(cfg, capsys)

    assert rc == 0
    assert payload["shared_file_nodes"] == 1
    [finding] = payload["shared_file_nodes_sample"]
    assert finding["cross_repo"] is True
    assert finding["repos"] == ["example.invalid/grp/a-b", "example.invalid/grp/a/b"]
    assert {(f["repo"], f["path"]) for f in finding["files"]} == {
        ("example.invalid/grp/a-b", "app.py"), ("example.invalid/grp/a/b", "app.py")}
    assert payload["shared_file_node_repos"] == finding["repos"]
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        assert len(_prefilter(store)) == 1
    finally:
        store.close()

    logs.clear()
    assert cmd_lint(cli.build_parser().parse_args(["kb", "lint", "--config", str(cfg)])) == 0
    [line] = [m for m in logs if "shared file node:" in m]
    assert "across repos" in line
    assert "example.invalid/grp/a-b:app.py" in line and "example.invalid/grp/a/b:app.py" in line
    assert "1 of them is missing from the graph" in line


# --- each guard in the query, against a store written row by row ------------------------

def _store(tmp_path):
    return SqliteStore(tmp_path / "index.sqlite")


def _node(node_id, repo, kind, file):
    return Node(id=node_id, repo=repo, kind=kind, name=file or node_id, file=file)


def _contains(src, dst, source_file, relation="contains"):
    return Edge(src=src, dst=dst, relation=relation, confidence=Confidence.EXTRACTED,
                provenance=Provenance(source_file=source_file, source_line=1,
                                      verified_at=date(2026, 10, 3)))


def test_a_file_with_one_path_behind_it_is_not_reported(tmp_path):
    store = _store(tmp_path)
    try:
        store.upsert_nodes("r", [_node("f", "r", "file", "a.py"),
                                 _node("s", "r", "function", "a.py")])
        store.upsert_edges("r", [_contains("f", "s", "a.py")])
        assert shared_file_nodes(store) == []
        assert _prefilter(store) == []
    finally:
        store.close()


def test_the_node_row_can_name_a_file_that_has_no_edges_of_its_own(tmp_path):
    """The walk keeps the LAST file, so the node row may name a file with no definitions
    while the only `contains` edges come from the other one. Edges alone show one path."""
    store = _store(tmp_path)
    try:
        store.upsert_nodes("r", [_node("f", "r", "file", "a-b.py"),
                                 _node("s", "r", "function", "a_b.py")])
        store.upsert_edges("r", [_contains("f", "s", "a_b.py")])
        [found] = shared_file_nodes(store)
        assert found["files"] == [{"repo": "r", "path": "a-b.py"}, {"repo": "r", "path": "a_b.py"}]
        assert found["cross_repo"] is False
    finally:
        store.close()


def test_a_class_contained_from_two_files_is_not_a_file_node(tmp_path):
    """Guard: `kind = 'file'`. A C++ class declared in a header and defined in a source
    file has one id and `contains` edges from two source files. It is not a file node."""
    store = _store(tmp_path)
    try:
        store.upsert_nodes("r", [_node("cls", "r", "class", "w.hpp"),
                                 _node("m1", "r", "method", "w.hpp"),
                                 _node("m2", "r", "method", "w.cpp")])
        store.upsert_edges("r", [_contains("cls", "m1", "w.hpp"), _contains("cls", "m2", "w.cpp")])
        assert shared_file_nodes(store) == []
        assert _prefilter(store) == []
    finally:
        store.close()


def test_a_connector_edge_that_names_a_url_is_not_read_as_a_second_file(tmp_path):
    """Guard: `relation = 'contains'`. A connector can attach an edge to a file node with
    the URL of the ticket as its source_file. That URL is not a file in the repo."""
    store = _store(tmp_path)
    try:
        store.upsert_nodes("r", [_node("f", "r", "file", "a.py"),
                                 _node("s", "r", "function", "a.py"),
                                 _node("t", "r", "ticket", None)])
        store.upsert_edges("r", [_contains("f", "s", "a.py"),
                                 _contains("f", "t", "https://example.invalid/browse/T-1",
                                           relation="tracked_by")])
        assert shared_file_nodes(store) == []
        assert _prefilter(store) == []
    finally:
        store.close()


def test_a_shared_node_found_through_another_repos_edges_is_cross_repo(tmp_path):
    """The second repo's index re-owns the node row. The first repo's edge keeps its own
    repo_id, so the pair set holds both repos even though only one edge points at it."""
    store = _store(tmp_path)
    try:
        store.upsert_nodes("a", [_node("f", "a", "file", "x.py"),
                                 _node("sa", "a", "function", "x.py")])
        store.upsert_edges("a", [_contains("f", "sa", "x.py")])
        store.upsert_nodes("b", [_node("f", "b", "file", "x.py")])
        [found] = shared_file_nodes(store)
        assert found["cross_repo"] is True and found["repos"] == ["a", "b"]
    finally:
        store.close()


# --- unavailable is not zero, and the finding is advisory --------------------------------

def test_a_store_that_cannot_answer_reports_none_not_an_empty_list():
    class NoConn:
        pass

    class Refuses:
        class conn:  # noqa: N801 - stands in for a connection attribute
            @staticmethod
            def execute(*_a, **_k):
                raise RuntimeError("database is locked")

    assert shared_file_nodes(NoConn()) is None
    assert shared_file_nodes(Refuses()) is None


def test_lint_says_could_not_check_and_emits_null_when_the_check_is_unavailable(
        tmp_path, monkeypatch, capsys, logs):
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo", {"m.py": "def ok():\n    return 1\n"})
    _store_dir, cfg = _config(tmp_path)
    _index_workspace(cfg, ws)
    monkeypatch.setattr(lint_mod, "shared_file_nodes", lambda _store: None)

    rc, payload = _lint_json(cfg, capsys)
    assert rc == 0
    assert payload["shared_file_nodes"] is None
    assert payload["shared_file_node_repos"] == [] and payload["shared_file_nodes_sample"] == []

    logs.clear()
    assert cmd_lint(cli.build_parser().parse_args(["kb", "lint", "--config", str(cfg)])) == 0
    assert any("shared file nodes: could not be checked" in m for m in logs)
    assert not any("shared file node(s)" in m for m in logs)


def test_a_store_with_nothing_indexed_carries_the_new_keys_too(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_dir, cfg = _config(tmp_path)
    rc, payload = _lint_json(cfg, capsys)
    assert rc == 0
    assert payload["shared_file_nodes"] == 0
    assert payload["shared_file_node_repos"] == [] and payload["shared_file_nodes_sample"] == []
