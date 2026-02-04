"""Tests for the embeddings provider abstraction (no live model)."""

import pytest

import gitlab_sync.kb.embeddings.ollama as ollama_mod
from gitlab_sync.kb.config import EmbeddingsCfg
from gitlab_sync.kb.embeddings import build_embedder
from gitlab_sync.kb.embeddings.ollama import OllamaEmbedder


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

    monkeypatch.setattr(ollama_mod, "_post_json", fake_post)
    emb = OllamaEmbedder(model="m", base_url="http://x:11434/")
    vecs = emb.embed(["alpha", "beta"])

    assert vecs == [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]]
    assert [c[1] for c in calls] == ["alpha", "beta"]
    assert calls[0][0] == "http://x:11434/api/embeddings"  # trailing slash trimmed
