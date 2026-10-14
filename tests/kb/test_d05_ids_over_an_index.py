"""D05 and F1 over a REAL `kb index`: one node per file, no dangling edge, clean upgrade.

A parity test that hands each producer the same string proves nothing: the producers take
their paths from the walk, and an exact id splits one file into two nodes the moment two of
them spell its path differently. So this indexes one fixture repo that several producers
touch (Python, SQL, `package.json`, an XML config, Terraform) and checks the store.
"""

from __future__ import annotations

import os
import subprocess
from argparse import Namespace

import pytest

from contextlake.kb.cmds.index import cmd_index
from contextlake.kb.cmds.lint import shared_file_nodes
from contextlake.kb.ids import make_id
from contextlake.kb.model import PACKAGES_REPO
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = "grp/app"
_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

FILES = {
    "app/a-b.py": 'import requests\n\n\ndef list_orders():\n'
                  '    return requests.get("http://billing/api/charges")\n',
    "app/a_b.py": "def helper():\n    return 1\n",
    # Unquoted SQL names ignore case (`sql._norm_name` folds them), so the D05 case here is
    # punctuation: `order_items` and the quoted `"order-items"` are two tables.
    "db/schema.sql": 'CREATE TABLE order_items (id INT);\nCREATE TABLE "order-items" (id INT);\n',
    "package.json": '{"name": "app", "dependencies": '
                    '{"@acme/billing-js": "1.0.0", "acme-billing-js": "2.0.0"}}\n',
    "app.config": '<configuration><appSettings><add key="Timeout" value="30"/>'
                  "</appSettings></configuration>\n",
    "main.tf": 'resource "aws_s3_bucket" "logs" {}\n',
}


def _case_sensitive(path) -> bool:
    probe = path / "CaseProbe"
    probe.write_text("x")
    try:
        return not (path / "caseprobe").exists()
    finally:
        probe.unlink()


def _make_repo(root):
    root.mkdir(parents=True)
    files = dict(FILES)
    if _case_sensitive(root):
        files["app/A_B.py"] = "def other():\n    return 2\n"
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    for args in (["init", "-q", "."], ["add", "-A"], ["commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=root, env=_ENV, check=True, capture_output=True)
    return files


def _index(tmp_path, src, *, force=False):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{(tmp_path / "store").as_posix()}"\n\n'
                   "[embeddings]\nenabled = false\n")
    rc = cmd_index(Namespace(config=str(cfg), workspace=None, source=str(src), args=[],
                             repos=None, repo=REPO, force=force, bundle=False, watch=False,
                             interval=0, path=None))
    assert rc == 0
    return SqliteStore(tmp_path / "store" / "index.sqlite")


def _dangling(store) -> list[tuple[str, str]]:
    rows = store.conn.execute(
        "SELECT src, dst FROM edges WHERE src NOT IN (SELECT node_id FROM nodes) "
        "OR dst NOT IN (SELECT node_id FROM nodes)").fetchall()
    return [(r[0], r[1]) for r in rows]


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    files = _make_repo(tmp_path / "app")
    store = _index(tmp_path, tmp_path / "app")
    yield store, files
    store.close()


def test_every_file_has_its_own_node(indexed):
    store, files = indexed
    rows = store.conn.execute(
        "SELECT name, COUNT(*) FROM nodes WHERE repo_id=? AND kind='file' GROUP BY name",
        (REPO,)).fetchall()
    counts = {r[0]: r[1] for r in rows}
    for rel in files:
        if rel.endswith((".py", ".sql")):
            assert counts.get(rel) == 1, (rel, counts)


def test_no_edge_points_at_a_missing_node(indexed):
    store, _files = indexed
    assert _dangling(store) == []


def test_two_npm_packages_stay_two_nodes(indexed):
    store, _files = indexed
    names = {r[0] for r in store.conn.execute(
        "SELECT name FROM nodes WHERE repo_id=? AND kind='package'", (PACKAGES_REPO,))}
    assert {"@acme/billing-js", "acme-billing-js"} <= names


def test_tables_differing_in_punctuation_stay_two_nodes(indexed):
    store, _files = indexed
    tables = store.conn.execute(
        "SELECT COUNT(DISTINCT node_id) FROM nodes WHERE repo_id=? AND kind='table'",
        (REPO,)).fetchone()[0]
    assert tables == 2


def test_kb_lint_finds_no_shared_file_node(indexed):
    store, _files = indexed
    assert shared_file_nodes(store) == []


def _pre13(mp):
    """Make this build write what parser 12 wrote: folded ids and the version stamp "12"."""
    import contextlake.kb.adr
    import contextlake.kb.flow.data
    import contextlake.kb.flow.events
    import contextlake.kb.flow.http
    import contextlake.kb.flow.state
    import contextlake.kb.flow.web
    import contextlake.kb.hcl
    import contextlake.kb.manifest
    import contextlake.kb.parse
    import contextlake.kb.sql
    import contextlake.kb.xml_cfg
    import contextlake.kb.xsd
    import contextlake.kb.xsl

    folded_file = lambda repo_id, rel_path: make_id(repo_id, rel_path)  # noqa: E731
    folded_name = lambda repo_id, rel_path, *names: make_id(repo_id, rel_path, *names)  # noqa: E731
    for mod in (contextlake.kb.parse, contextlake.kb.manifest, contextlake.kb.flow.data,
                contextlake.kb.flow.events, contextlake.kb.flow.http,
                contextlake.kb.flow.state, contextlake.kb.flow.web):
        mp.setattr(mod, "file_node_id", folded_file)
    for mod in (contextlake.kb.hcl, contextlake.kb.xsl, contextlake.kb.xsd, contextlake.kb.sql,
                contextlake.kb.xml_cfg, contextlake.kb.adr, contextlake.kb.manifest,
                contextlake.kb.flow.web):
        mp.setattr(mod, "scoped_id", folded_name)
    mp.setattr(contextlake.kb.manifest, "package_id",
               lambda eco, name: make_id("pkg", eco, name))
    mp.setattr(contextlake.kb.parse, "PARSER_VERSION", "12")


def _index_by(tmp_path, how):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{(tmp_path / "store").as_posix()}"\n\n'
                   "[embeddings]\nenabled = false\n")
    common = dict(config=str(cfg), args=[], repos=None, force=False, bundle=False,
                  watch=False, interval=0, path=None, workers=1)
    if how == "source":
        rc = cmd_index(Namespace(workspace=None, source=str(tmp_path / "ws" / "app"),
                                 repo=REPO, **common))
    else:
        rc = cmd_index(Namespace(workspace=str(tmp_path / "ws"), source=None, repo=None,
                                 **common))
    assert rc == 0
    return SqliteStore(tmp_path / "store" / "index.sqlite")


@pytest.mark.parametrize("how", ["source", "workspace"])
def test_a_parser_12_store_upgrades_with_a_plain_kb_index(tmp_path, monkeypatch, how):
    """The upgrade the CHANGELOG promises: an unchanged commit, a store written by parser
    12, and `kb index` WITHOUT --force. The version change must make it re-index, the old
    ids must go (the repo partition is replaced; the old package node sits in the shared
    `(packages)` partition and must be pruned) and no edge may dangle."""
    from contextlake.kb.state import indexed_parser_version

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _make_repo(tmp_path / "ws" / "app")
    with monkeypatch.context() as old:
        _pre13(old)
        store = _index_by(tmp_path, how)
        (repo_id,) = [r.id for r in store.list_repos()]
        stale = {make_id(repo_id, "app/a-b.py"),
                 make_id(repo_id, "db/schema.sql", "table", "order_items"),
                 make_id("pkg", "npm", "@acme/billing-js")}
        present = {nid for nid in stale if store.get_node(nid) is not None}
        assert present == stale, "control: the parser-12 store holds the folded ids"
        assert indexed_parser_version(store, tmp_path / "store", repo_id) == "12"
        store.close()

    store = _index_by(tmp_path, how)
    try:
        assert indexed_parser_version(store, tmp_path / "store", repo_id) == "13"
        assert [nid for nid in stale if store.get_node(nid) is not None] == []
        assert _dangling(store) == []
    finally:
        store.close()
