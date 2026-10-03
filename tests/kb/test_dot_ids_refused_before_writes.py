"""Three writers check an id before they write anything, not when `write_shard` refuses it.

`shard_path` now refuses an id with a `.` or `..` segment, or an absolute id, because
`team/../other` names repo `other`'s own files. These three callers wrote store rows or
nodes first and called `write_shard` last, so a refused id left half a write behind and,
for ingest and wiki, ended the whole run with an unhandled error:

- `kb ingest` builds `@ingest:<source name>` from a name in the config;
- the wiki stores `@wiki:<repo id>` for a repo an older version indexed under such an id;
- the dashboard's Sync and Add buttons index a repo whose id comes from a remote URL.
"""

from __future__ import annotations

import subprocess

import pytest

from contextlake.cli import main
from contextlake.kb.cmds.wiki import _store_wiki_partition
from contextlake.kb.dashboard import mutations as mut
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore


def _store(tmp_path):
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    return store, store_dir


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_ingest_skips_a_source_whose_name_is_not_plain_and_runs_the_rest(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\nstep one\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n[embeddings]\nenabled = false\n'
        f'[[sources]]\nname = "a/../b"\ntype = "files"\npath = "{docs}"\n'
        f'[[sources]]\nname = "good"\ntype = "files"\npath = "{docs}"\n')

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--config", str(cfg)])

    out = capsys.readouterr().out
    assert e.value.code == 1, "a refused source is a failed source"
    assert "a/../b: refused" in out
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    try:
        repos = {r.id for r in store.list_repos()}
        assert store.get_node("@ingest:good:guide.md") is not None, "the good source ran"
        assert not any("a/../b" in n.id for n in store.search("guide", limit=50))
        assert "@ingest:a/../b" not in repos
    finally:
        store.close()


def test_wiki_store_refuses_a_dot_id_before_clearing_anything(tmp_path):
    store, store_dir = _store(tmp_path)
    try:
        before = _files(store_dir)
        n = _store_wiki_partition(store, store_dir, "team/../other", "# x\n\n## A\n\nbody\n",
                                  "x.md", "h1")
        assert n == 0
        assert _files(store_dir) == before
        assert store.list_repos() == []
    finally:
        store.close()


def test_dashboard_sync_refuses_a_dot_id(tmp_path):
    store, store_dir = _store(tmp_path)
    try:
        got = mut.sync_repo(store, store_dir, "evil.example/../..")
        assert got["ok"] is False
        assert "not a plain repo id" in got["error"]
    finally:
        store.close()


def test_dashboard_add_clones_but_does_not_index_a_dot_id(tmp_path, monkeypatch):
    """The id comes from the clone's remote, which its owner wrote."""
    store, store_dir = _store(tmp_path)

    def fake_run(cmd, **_kw):
        (tmp_path / "ws" / "repo" / ".git").mkdir(parents=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mut.subprocess, "run", fake_run)
    monkeypatch.setattr("contextlake.kb.repo_identity.resolve_repo_id",
                        lambda _path: "evil.example/../..")
    try:
        before = _files(store_dir)
        got = mut.add_repo(store, store_dir, tmp_path / "ws", "https://example.com/x/repo.git")
        assert got["ok"] is False
        assert "not indexed" in got["error"]
        assert store.list_repos() == [], "no ghost row for a repo that was not indexed"
        assert _files(store_dir) == before
    finally:
        store.close()
