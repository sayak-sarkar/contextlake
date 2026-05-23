"""Tests for the embeddings provider abstraction (no live model)."""

import sys
import types

import pytest

import contextlake.kb.embeddings.base as base_mod
import contextlake.kb.embeddings.ollama as ollama_mod
from contextlake.kb.config import EmbeddingsCfg
from contextlake.kb.embeddings import build_embedder
from contextlake.kb.embeddings.builtin import BuiltinEmbedder
from contextlake.kb.embeddings.ollama import OllamaEmbedder


def _fake_model2vec(monkeypatch, *, with_class=True):
    """Install a fake `model2vec` module so embed() needs no download.

    with_class=False simulates the extra being absent (import of StaticModel fails)."""
    mod = types.ModuleType("model2vec")
    if with_class:
        class FakeStatic:
            @classmethod
            def from_pretrained(cls, model_id):
                return cls()

            def encode(self, texts):
                return [[0.1, 0.2, 0.3] for _ in texts]

        mod.StaticModel = FakeStatic
    monkeypatch.setitem(sys.modules, "model2vec", mod)


def test_build_embedder_disabled_returns_none():
    assert build_embedder(EmbeddingsCfg(enabled=False)) is None


def test_build_embedder_ollama():
    cfg = EmbeddingsCfg(enabled=True, provider="ollama", model="my-model",
                        base_url="http://host:1234")
    emb = build_embedder(cfg)
    assert isinstance(emb, OllamaEmbedder)
    assert emb.model == "my-model" and emb.base_url == "http://host:1234"


def test_build_embedder_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown embeddings provider"):
        build_embedder(EmbeddingsCfg(enabled=True, provider="nope"))


def test_ollama_embed_posts_per_text(monkeypatch):
    calls = []

    def fake_post(url, payload, timeout):
        calls.append((url, payload["prompt"]))
        return {"embedding": [1.0, 2.0, 3.0]}

    monkeypatch.setattr(ollama_mod, "post_json", fake_post)
    emb = OllamaEmbedder(model="m", base_url="http://x:11434/")
    vecs = emb.embed(["alpha", "beta"])

    assert vecs == [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]]
    assert [c[1] for c in calls] == ["alpha", "beta"]
    assert calls[0][0] == "http://x:11434/api/embeddings"  # trailing slash trimmed


# --- built-in embedder ------------------------------------------------------

def test_default_provider_is_auto():
    assert EmbeddingsCfg().provider == "auto"


def test_build_embedder_builtin_constructs_lazily():
    # The factory builds the provider without importing/loading the heavy model.
    cfg = EmbeddingsCfg(enabled=True, provider="builtin")
    emb = build_embedder(cfg)
    assert isinstance(emb, BuiltinEmbedder)
    assert emb.engine == "model2vec"
    assert emb.model_id == "minishlab/potion-base-8M"
    assert emb._model is None  # nothing loaded/downloaded yet
    assert emb.identity == "builtin:model2vec:minishlab/potion-base-8M"


def test_builtin_engine_and_model_overrides():
    cfg = EmbeddingsCfg(enabled=True, provider="builtin", model="custom/model")
    cfg.engine = "fastembed"
    emb = build_embedder(cfg)
    assert emb.engine == "fastembed" and emb.model_id == "custom/model"


def test_builtin_unknown_engine_raises():
    with pytest.raises(ValueError, match="unknown builtin embedder engine"):
        BuiltinEmbedder(engine="bogus")


def test_builtin_embed_missing_extra_raises_actionable(monkeypatch):
    _fake_model2vec(monkeypatch, with_class=False)  # extra "present" but no StaticModel
    emb = BuiltinEmbedder(engine="model2vec")
    with pytest.raises(ImportError, match=r"kb-local"):
        emb.embed(["x"])


def test_builtin_embed_model2vec_mocked(monkeypatch):
    _fake_model2vec(monkeypatch, with_class=True)
    emb = BuiltinEmbedder(engine="model2vec")
    vecs = emb.embed(["a", "b"])
    assert vecs == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert emb.embed([]) == []


# --- the "auto" resolver ----------------------------------------------------

def test_auto_prefers_reachable_ollama(monkeypatch):
    monkeypatch.setattr(base_mod, "ollama_reachable", lambda *a, **k: True)
    emb = build_embedder(EmbeddingsCfg(enabled=True, provider="auto"))
    assert isinstance(emb, OllamaEmbedder)


def test_auto_falls_back_to_builtin(monkeypatch):
    monkeypatch.setattr(base_mod, "ollama_reachable", lambda *a, **k: False)
    monkeypatch.setattr(
        base_mod.importlib.util, "find_spec",
        lambda name: object() if name == "model2vec" else None,
    )
    emb = build_embedder(EmbeddingsCfg(enabled=True, provider="auto"))
    assert isinstance(emb, BuiltinEmbedder) and emb.engine == "model2vec"


def test_auto_returns_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(base_mod, "ollama_reachable", lambda *a, **k: False)
    monkeypatch.setattr(base_mod.importlib.util, "find_spec", lambda name: None)
    assert build_embedder(EmbeddingsCfg(enabled=True, provider="auto")) is None  # no raise


# --- dimension/identity guard ----------------------------------------------

def test_embedder_identity_default_and_override():
    assert OllamaEmbedder(model="nomic").identity == "ollama:nomic"
    assert BuiltinEmbedder(engine="model2vec").identity.startswith("builtin:model2vec:")


def test_guard_store_identity_records_then_matches(tmp_path):
    from contextlake.kb.embeddings.store import VectorStore, guard_store_identity

    vs = VectorStore(tmp_path / "e.sqlite")
    guard_store_identity(vs, "builtin:model2vec:m", 256)  # empty store -> records
    guard_store_identity(vs, "builtin:model2vec:m", 256)  # same again -> no raise
    vs.close()


def test_guard_store_identity_rejects_dim_change(tmp_path):
    from contextlake.kb.embeddings.store import VectorStore, guard_store_identity

    vs = VectorStore(tmp_path / "e.sqlite")
    guard_store_identity(vs, "builtin:model2vec:m", 256)
    with pytest.raises(ValueError, match="dimension"):
        guard_store_identity(vs, "builtin:model2vec:m", 384)
    vs.close()


def test_guard_store_identity_rejects_model_change(tmp_path):
    from contextlake.kb.embeddings.store import VectorStore, guard_store_identity

    vs = VectorStore(tmp_path / "e.sqlite")
    guard_store_identity(vs, "ollama:nomic", 256)
    with pytest.raises(ValueError, match="embedder"):
        guard_store_identity(vs, "builtin:model2vec:m", 256)
    vs.close()
