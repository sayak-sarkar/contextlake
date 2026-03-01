"""The embedding-provider interface and a config-driven factory.

Providers are local-first and entirely optional. A provider turns text into
fixed-length vectors; the rest of the knowledge layer stays provider-agnostic.
"""

from __future__ import annotations

import importlib.util
import urllib.request
from abc import ABC, abstractmethod


class Embedder(ABC):
    """Turns text into vectors. Implementations carry their own model/runtime."""

    name: str = "embedder"

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""


def build_embedder(cfg) -> Embedder | None:
    """Construct an Embedder from an EmbeddingsCfg, or None when disabled.

    Raises ValueError for an enabled-but-unknown provider so misconfiguration is
    surfaced rather than silently producing no embeddings.
    """
    if not getattr(cfg, "enabled", False):
        return None
    provider = (getattr(cfg, "provider", "") or "").lower()
    if provider == "ollama":
        from .ollama import OllamaEmbedder

        return OllamaEmbedder(
            model=cfg.model or "nomic-embed-text",
            base_url=getattr(cfg, "base_url", "http://127.0.0.1:11434"),
            batch_size=getattr(cfg, "batch_size", 64),
        )
    if provider == "openai":
        from .openai import OpenAIEmbedder

        return OpenAIEmbedder(
            model=cfg.model or "text-embedding-3-small",
            base_url=getattr(cfg, "base_url", "https://api.openai.com/v1"),
            api_key_env=getattr(cfg, "api_key_env", "OPENAI_API_KEY"),
            batch_size=getattr(cfg, "batch_size", 64),
        )
    if provider == "builtin":
        return _build_builtin_embedder(cfg)
    if provider == "auto":
        return _resolve_auto_embedder(cfg)
    raise ValueError(f"unknown embeddings provider: {provider!r}")


def _build_builtin_embedder(cfg):
    """Construct the built-in CPU embedder (model2vec/fastembed). Raises the
    actionable missing-extra error lazily, at first embed()."""
    from .builtin import BuiltinEmbedder

    return BuiltinEmbedder(
        engine=getattr(cfg, "engine", "model2vec"),
        model=getattr(cfg, "model", None),
        cache_dir=getattr(cfg, "cache_dir", None),
        batch_size=getattr(cfg, "batch_size", 64),
    )


def _ollama_reachable(base_url: str, timeout: float = 1.5) -> bool:
    """True if a local Ollama daemon answers quickly (so 'auto' never hangs)."""
    try:
        url = base_url.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=timeout):  # noqa: S310 - local URL
            return True
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def _resolve_auto_embedder(cfg) -> Embedder | None:
    """Resolve provider="auto": a reachable local Ollama, else the built-in
    embedder if its extra is importable, else None (graceful skip). Never raises."""
    base_url = getattr(cfg, "base_url", "http://127.0.0.1:11434")
    if _ollama_reachable(base_url):
        from .ollama import OllamaEmbedder

        return OllamaEmbedder(
            model=getattr(cfg, "model", None) or "nomic-embed-text",
            base_url=base_url,
            batch_size=getattr(cfg, "batch_size", 64),
        )
    engine = (getattr(cfg, "engine", "model2vec") or "model2vec").lower()
    module = "fastembed" if engine == "fastembed" else "model2vec"
    # find_spec locates the package without importing the heavy module.
    if importlib.util.find_spec(module) is not None:
        return _build_builtin_embedder(cfg)
    return None
