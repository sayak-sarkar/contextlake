"""CLI integration tests for the knowledge-layer verbs (the Phase 2.0 DoD)."""

from pathlib import Path

import pytest

from gitlab_sync.cli import main
from gitlab_sync.kb.store.sqlite_store import SqliteStore

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "fixtures" / "sample-graph.json"


def _kb_config(tmp_path) -> Path:
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    return cfg


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def test_index_then_query_round_trip(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0

    # the store on disk is populated
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    assert store.get_node("demo_app_orderservice").name == "OrderService"
    assert store.stats().edges == 1
    store.close()

    # query finds it (cited)
    capsys.readouterr()
    assert _run(["query", "OrderService", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "OrderService" in out and "demo/app" in out


def test_index_without_source_just_initializes(tmp_path):
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg)]) == 0
    assert (tmp_path / "kb" / "index.sqlite").exists()


def test_query_without_text_is_usage_error(tmp_path):
    cfg = _kb_config(tmp_path)
    assert _run(["query", "--config", str(cfg)]) == 2


def test_doctor_reports_ok(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out.lower()
    assert "doctor" in out
    assert "fts5" in out
    assert code == 0  # git + fts5 present in the test environment
