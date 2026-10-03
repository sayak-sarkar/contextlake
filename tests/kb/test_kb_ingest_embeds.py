"""`kb ingest` embeds what it stores, and fails loudly when the embedder does.

Until this file, CI never ran `cmd_ingest`'s embed branch. CI has no embedding engine,
so `build_embedder` returns None there and the branch is skipped. The dev machine has
model2vec, so the same tests took the branch there by downloading a model from the Hub,
and a network failure failed six of them at once (flake #17). A fake embedder runs the
branch on every machine, with no network.
"""

from __future__ import annotations

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
