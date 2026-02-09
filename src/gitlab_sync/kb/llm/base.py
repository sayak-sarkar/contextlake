"""The LLM-client interface and a config-driven factory.

Providers turn a prompt (plus an optional system instruction) into text. Local
and optional; the rest of the wiki tier stays provider-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LlmClient(ABC):
    name: str = "llm"

    @abstractmethod
    def generate(self, prompt: str, *, system: str | None = None) -> str:
        """Return the model's completion for ``prompt``."""


def build_llm(cfg) -> LlmClient | None:
    """Construct an LlmClient from an LlmCfg, or None when disabled.

    Raises ValueError for an enabled-but-unknown provider so misconfiguration is
    surfaced rather than silently producing no output.
    """
    if not getattr(cfg, "enabled", False):
        return None
    provider = (getattr(cfg, "provider", "") or "").lower()
    if provider == "ollama":
        from .ollama import OllamaLlm

        return OllamaLlm(
            model=cfg.model or "llama3.1",
            base_url=getattr(cfg, "base_url", "http://127.0.0.1:11434"),
        )
    raise ValueError(f"unknown llm provider: {provider!r}")
