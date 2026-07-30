"""Tests for `contextlake doctor` -- the stale-parser-version shard check."""

from pathlib import Path

import pytest

from contextlake.cli import main
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def _kb_config(tmp_path, store_dir: Path) -> Path:
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    return cfg


def test_doctor_flags_shard_with_stale_parser_version(tmp_path, capsys):
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)

    shard = GraphShard(repo="demo/old", parser_version="0",
                        nodes=[Node(id="n1", repo="demo/old", kind="file", name="a.cpp",
                                    lang="cpp")])
    write_shard(store_dir, shard)

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="demo/old", path=str(tmp_path / "demo" / "old")))
    store.close()

    cfg = _kb_config(tmp_path, store_dir)
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "demo/old" in out
    assert "re-index" in out
    # advisory, like the other per-repo/per-source checks in this section of
    # doctor: a stale parser stamp reflects prior indexing state, not something
    # broken in the local environment right now, so it never fails the verdict.
    assert code == 0


def test_doctor_does_not_flag_shard_at_current_parser_version(tmp_path, capsys):
    from contextlake.kb.parse import PARSER_VERSION

    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)

    shard = GraphShard(repo="demo/new", parser_version=PARSER_VERSION,
                        nodes=[Node(id="n1", repo="demo/new", kind="file", name="a.cpp",
                                    lang="cpp")])
    write_shard(store_dir, shard)

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="demo/new", path=str(tmp_path / "demo" / "new")))
    store.close()

    cfg = _kb_config(tmp_path, store_dir)
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "up to date with the current parser" in out
    assert code == 0


def test_doctor_does_not_flag_non_cpp_shard_with_stale_parser_version(tmp_path, capsys):
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)

    # A stale parser_version on a shard with no C/C++ nodes is irrelevant -- the
    # bugs Tasks 1-4 fixed are C/C++-specific, so only C/C++ shards are worth
    # re-indexing over this.
    shard = GraphShard(repo="demo/py", parser_version="0",
                        nodes=[Node(id="n1", repo="demo/py", kind="file", name="a.py",
                                    lang="python")])
    write_shard(store_dir, shard)

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="demo/py", path=str(tmp_path / "demo" / "py")))
    store.close()

    cfg = _kb_config(tmp_path, store_dir)
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "up to date with the current parser" in out
    assert code == 0
