"""The embedding-provider interface and a config-driven factory.

Providers are local-first and entirely optional. A provider turns text into
fixed-length vectors; the rest of the knowledge layer stays provider-agnostic.
"""

from __future__ import annotations

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
    raise ValueError(f"unknown embeddings provider: {provider!r}")
