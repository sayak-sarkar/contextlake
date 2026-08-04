"""Tests for `contextlake doctor` -- the stale-parser-version shard check."""

import importlib.util
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


def test_doctor_flags_non_cpp_shard_with_stale_parser_version(tmp_path, capsys):
    """The language a repo is written in is not the gate.

    This check used to fire only for shards holding C/C++ nodes, from when the
    only parser change was C++ method/class linkage. PARSER_VERSION has since
    moved for reasons that change output in every language, so a Python repo
    carrying an old stamp is exactly as stale, and was the case nothing reported.
    """
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)

    shard = GraphShard(repo="demo/py", parser_version="1",
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
    assert "demo/py" in out
    assert "older parser" in out
    assert code == 0  # advisory, as above


def test_doctor_stale_detail_keeps_the_cpp_linkage_note_for_cpp_shards(tmp_path, capsys):
    """C/C++ is no longer the gate, but it is still a useful detail."""
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)

    write_shard(store_dir, GraphShard(
        repo="demo/py", parser_version="1",
        nodes=[Node(id="n1", repo="demo/py", kind="file", name="a.py", lang="python")]))
    write_shard(store_dir, GraphShard(
        repo="demo/cc", parser_version="1",
        nodes=[Node(id="n2", repo="demo/cc", kind="file", name="a.cpp", lang="cpp")]))

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="demo/py", path=str(tmp_path / "demo" / "py")))
    store.upsert_repo(Repo(id="demo/cc", path=str(tmp_path / "demo" / "cc")))
    store.close()

    cfg = _kb_config(tmp_path, store_dir)
    _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "2 repo(s) indexed with an older parser" in out
    assert "1 of them hold C/C++ code" in out
    assert "method/class linkage" in out


def _disabled_llm_doctor(tmp_path, capsys, monkeypatch, *, runtime_present: bool) -> str:
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    real = importlib.util.find_spec

    def fake(name, *a, **kw):
        if name == "llama_cpp":
            return object() if runtime_present else None
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    _run(["doctor", "--config", str(_kb_config(tmp_path, store_dir))])
    return capsys.readouterr().out


def test_doctor_distinguishes_llm_off_in_config_from_runtime_absent(
        tmp_path, capsys, monkeypatch):
    """"You turned it off" and "it was never installed" have different fixes.

    Both used to print the single word "disabled", which is the least useful
    thing to read when working out why wiki generation is unavailable.
    """
    installed = _disabled_llm_doctor(tmp_path / "a", capsys, monkeypatch, runtime_present=True)
    assert "not enabled in config" in installed
    assert "llama-cpp-python" not in installed

    absent = _disabled_llm_doctor(tmp_path / "b", capsys, monkeypatch, runtime_present=False)
    assert "not enabled in config" in absent
    assert "llama-cpp-python) is not installed" in absent
    assert "--fix llm-local" in absent
