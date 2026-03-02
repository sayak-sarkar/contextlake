"""The LLM-client interface and a config-driven factory.

Providers turn a prompt (plus an optional system instruction) into text. Local
and optional; the rest of the wiki tier stays provider-agnostic.
"""

from __future__ import annotations

import importlib.util
import urllib.request
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
    if provider == "openai":
        from .openai import OpenAILlm

        return OpenAILlm(
            model=cfg.model or "gpt-4o-mini",
            base_url=getattr(cfg, "base_url", "https://api.openai.com/v1"),
            api_key_env=getattr(cfg, "api_key_env", "OPENAI_API_KEY"),
        )
    if provider == "builtin":
        return _build_builtin_llm(cfg)
    if provider == "auto":
        return _resolve_auto_llm(cfg)
    raise ValueError(f"unknown llm provider: {provider!r}")


def _build_builtin_llm(cfg):
    """Construct the built-in CPU LLM. The actionable missing-extra error is
    raised lazily, at first generate()."""
    from .builtin import BuiltinLlm

    kw = {}
    if getattr(cfg, "model", None):
        kw["repo_id"] = cfg.model
    if getattr(cfg, "model_file", None):
        kw["filename"] = cfg.model_file
    if getattr(cfg, "cache_dir", None):
        kw["cache_dir"] = cfg.cache_dir
    return BuiltinLlm(**kw)


def _ollama_reachable(base_url: str, timeout: float = 1.5) -> bool:
    """True if a local Ollama daemon answers quickly (so 'auto' never hangs)."""
    try:
        url = base_url.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=timeout):  # noqa: S310 - local URL
            return True
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def _resolve_auto_llm(cfg) -> LlmClient | None:
    """Resolve provider="auto": a reachable local Ollama, else the built-in LLM if
    llama-cpp-python is importable, else None (graceful skip). Never raises."""
    base_url = getattr(cfg, "base_url", "http://127.0.0.1:11434")
    if _ollama_reachable(base_url):
        from .ollama import OllamaLlm

        return OllamaLlm(model=getattr(cfg, "model", None) or "llama3.1", base_url=base_url)
    if importlib.util.find_spec("llama_cpp") is not None:
        return _build_builtin_llm(cfg)
    return None
