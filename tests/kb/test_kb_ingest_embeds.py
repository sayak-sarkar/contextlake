"""`kb ingest` embeds what it stores, and fails loudly when the embedder does.

Until this file, CI never ran `cmd_ingest`'s embed branch. CI has no embedding engine,
so `build_embedder` returns None there and the branch is skipped. The dev machine has
model2vec, so the same tests took the branch there by downloading a model from the Hub,
and a network failure failed six of them at once (flake #17). A fake embedder runs the
branch on every machine, with no network.
"""

from __future__ import annotations

import json
import types

import pytest

from contextlake.cli import main
from contextlake.kb import embeddings
from contextlake.kb.embeddings.store import build_vector_store


class _FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        return [[1.0, float(i)] for i, _ in enumerate(texts)]


class _DeadEmbedder:
    name = "dead"

    def embed(self, texts):
        raise ConnectionError("embedder unreachable")


def _ingest(tmp_path, monkeypatch, embedder):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\nstep one\n")
    (docs / "faq.md").write_text("# FAQ\nq and a\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: embedder)

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", str(docs), "--config", str(cfg)])
    return e.value.code


def _vector_count(store_dir):
    vs = build_vector_store(store_dir / "embeddings.sqlite")
    try:
        return vs.count_repo("@ingest:cli")
    finally:
        vs.close()


def test_ingest_embeds_every_document_it_stores(tmp_path, monkeypatch, capsys):
    assert _ingest(tmp_path, monkeypatch, _FakeEmbedder()) == 0

    assert _vector_count(tmp_path / "kb") == 2
    assert "2 embedded into the semantic store" in capsys.readouterr().out


def test_an_embedder_that_fails_makes_ingest_exit_nonzero_and_say_so(
        tmp_path, monkeypatch, capsys):
    assert _ingest(tmp_path, monkeypatch, _DeadEmbedder()) == 1

    out = capsys.readouterr().out
    assert "EMBED INCOMPLETE: 0 of 2" in out
    assert "Ingest incomplete" in out


class _JsonResponse:
    """A 200 answer carrying ``payload`` as JSON, shaped like what the api source reads."""

    class headers:                                           # noqa: N801 - mimics a message
        @staticmethod
        def get_content_charset():
            return "utf-8"

        @staticmethod
        def get(_name, default=None):
            return default

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_records_that_repeat_or_lack_an_id_are_all_stored_and_counted(
        tmp_path, monkeypatch, capsys):
    """Four records, two sharing the id d1 and two with a null id, ran as `4 document(s)`
    and `4 embedded` while the store held two nodes and two vectors: the later record of
    each pair replaced the earlier one, and nothing said so."""
    import sqlite3

    import contextlake.kb.sources.api as apimod

    monkeypatch.setenv("HOME", str(tmp_path))
    records = [
        {"id": "d1", "title": "First", "text": "one"},
        {"id": "d1", "title": "Second", "text": "two"},
        {"id": None, "title": "Third", "text": "three"},
        {"id": None, "title": "Fourth", "text": "four"},
    ]
    monkeypatch.setattr(apimod, "_OPENER", types.SimpleNamespace(
        open=lambda req, timeout=None: _JsonResponse(records)))
    monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: _FakeEmbedder())
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", "http://items.invalid/all", "--source-type", "api",
              "--config", str(cfg)])

    out = capsys.readouterr().out
    assert e.value.code == 0
    db = sqlite3.connect(tmp_path / "kb" / "index.sqlite")
    try:
        rows = db.execute("SELECT node_id, name FROM nodes WHERE repo_id = '@ingest:cli' "
                          "ORDER BY name").fetchall()
    finally:
        db.close()
    assert sorted(rows) == [("@ingest:cli:2", "Third"), ("@ingest:cli:3", "Fourth"),
                            ("@ingest:cli:d1", "First"), ("@ingest:cli:d1#2", "Second")]
    assert _vector_count(tmp_path / "kb") == 4
    assert "cli: 1 document(s) repeated the id of an earlier one" in out
    assert "cli: 4 document(s), 4 embedded" in out
    assert "Ingest complete: 4 document(s) aggregated, 4 embedded" in out


def test_a_suffixed_id_never_takes_one_a_later_record_really_has():
    from contextlake.kb.cmds.ingest import _distinct_id

    taken: set[str] = set()
    assert [_distinct_id(i, taken) for i in ("a", "a", "a#2", "a", "b")] == [
        "a", "a#2", "a#2#2", "a#3", "b"]
