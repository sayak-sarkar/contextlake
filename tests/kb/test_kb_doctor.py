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


def test_redact_scrubs_doctors_report_and_leaves_the_default_alone(tmp_path, capsys):
    """doctor writes its aligned report with print, not through the logger, so it
    sat outside redaction entirely: `--redact doctor` printed the absolute store
    path and every config path in full -- on the one command whose whole output is
    what a person pastes into a bug report."""
    from argparse import Namespace

    from contextlake import observability
    from contextlake.kb.cmds.doctor import cmd_doctor
    from contextlake.logging_setup import setup_logging

    store = tmp_path / "private-store"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store}"\n', encoding="utf-8")
    args = Namespace(config=str(cfg), fix=False)

    observability.reset_redactions()
    try:
        # default: the console keeps the real paths, which is what makes them
        # actionable for the person reading their own terminal
        setup_logging(redact=None)
        cmd_doctor(args)
        assert str(store) in capsys.readouterr().out

        setup_logging(redact=True)
        cmd_doctor(args)
        out = capsys.readouterr().out
        assert str(store) not in out
        assert str(cfg) not in out
        assert "<store>" in out and "<config>" in out
    finally:
        observability.reset_redactions()
        setup_logging()


def test_doctor_writes_its_report_to_the_log_file(tmp_path, capsys):
    """Measured at zero lines: doctor renders its report itself rather than
    through the logger (the console formatter's right-edge clock would wreck the
    alignment), and nothing carried it into --log-file -- on the one command
    whose entire output is what a person attaches to a bug report."""
    from argparse import Namespace

    from contextlake import observability
    from contextlake.kb.cmds.doctor import cmd_doctor
    from contextlake.logging_setup import setup_logging

    store = tmp_path / "store"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store}"\n', encoding="utf-8")
    log_file = tmp_path / "run.log"

    observability.reset_redactions()
    try:
        # redact=False so console and file say the same thing and the parity
        # assertion below is about layout, not about scrubbing.
        setup_logging(log_file=str(log_file), redact=False)
        cmd_doctor(Namespace(config=str(cfg), fix=False))
        console = capsys.readouterr().out
        recorded = log_file.read_text(encoding="utf-8")
    finally:
        observability.reset_redactions()
        setup_logging()

    assert "doctor" in recorded
    assert "SQLite FTS5 available" in recorded
    assert "git on PATH" in recorded
    # The console report is unchanged: no clock, no reflow, every line still there.
    for line in (ln for ln in console.splitlines() if ln.strip()):
        assert line in recorded, line


def test_doctor_report_in_the_log_file_is_redacted(tmp_path, capsys):
    """The audit file is the artifact that gets attached to a bug report, so it
    scrubs by default even while the console keeps the real paths."""
    from argparse import Namespace

    from contextlake import observability
    from contextlake.kb.cmds.doctor import cmd_doctor
    from contextlake.logging_setup import setup_logging

    store = tmp_path / "private-store"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store}"\n', encoding="utf-8")
    log_file = tmp_path / "run.log"

    observability.reset_redactions()
    try:
        setup_logging(log_file=str(log_file))
        cmd_doctor(Namespace(config=str(cfg), fix=False))
        assert str(store) in capsys.readouterr().out
        recorded = log_file.read_text(encoding="utf-8")
    finally:
        observability.reset_redactions()
        setup_logging()

    assert str(store) not in recorded
    assert "<store>" in recorded
