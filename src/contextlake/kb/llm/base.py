"""The LLM-client interface and a config-driven factory.

Providers turn a prompt (plus an optional system instruction) into text. Local
and optional; the rest of the wiki tier stays provider-agnostic.
"""

from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod

from .._util import ollama_has_model, ollama_reachable


class LlmClient(ABC):
    name: str = "llm"

    @abstractmethod
    def generate(self, prompt: str, *, system: str | None = None) -> str:
        """Return the model's completion for ``prompt``."""


def default_api_key_env(provider: str) -> str:
    """The env var holding the API key when the config left ``api_key_env`` unset.

    Resolved at read time (here and in ``cmd_doctor``), not at ``LlmCfg``
    construction: ``apply_llm_overrides`` (the ``--llm PROVIDER`` CLI flag) sets
    ``cfg.llm.provider`` by plain attribute assignment on an already-built
    ``LlmCfg``, and pydantic v2 does not re-run validators on assignment.
    """
    return "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"


def default_base_url(provider: str) -> str:
    """The API endpoint to use when the config left ``base_url`` unset.

    Resolved at read time for the same reason as ``default_api_key_env``: the
    ``--llm PROVIDER`` flag reassigns ``cfg.llm.provider`` on an already-built
    ``LlmCfg`` and pydantic v2 does not re-run validators on assignment. A
    construction-time default is worse than useless here -- a single declared
    default (previously the Ollama URL) silently wins for *every* provider, so
    ``provider = "anthropic"`` with no explicit ``base_url`` would POST to the
    local Ollama port instead of the Anthropic API.
    """
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "anthropic":
        return "https://api.anthropic.com"
    return "http://127.0.0.1:11434"   # ollama, and auto's ollama probe


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
            base_url=getattr(cfg, "base_url", None) or default_base_url(provider),
            timeout=getattr(cfg, "timeout", 300),
        )
    if provider == "openai":
        from .openai import OpenAILlm

        return OpenAILlm(
            model=cfg.model or "gpt-4o-mini",
            base_url=getattr(cfg, "base_url", None) or default_base_url(provider),
            api_key_env=getattr(cfg, "api_key_env", None) or default_api_key_env("openai"),
            timeout=getattr(cfg, "timeout", 300),
        )
    if provider == "anthropic":
        from .anthropic import AnthropicLlm

        return AnthropicLlm(
            model=cfg.model or "claude-opus-4-8",
            base_url=getattr(cfg, "base_url", None) or default_base_url(provider),
            api_key_env=getattr(cfg, "api_key_env", None) or default_api_key_env("anthropic"),
            max_tokens=getattr(cfg, "max_tokens", 4096),
            timeout=getattr(cfg, "timeout", 300),
        )
    if provider == "cli":
        from .cli import CliLlm

        return CliLlm(
            command=getattr(cfg, "command", None) or "claude",
            args=getattr(cfg, "args", None),
            timeout=getattr(cfg, "timeout", 300),
        )
    if provider == "builtin":
        return _build_builtin_llm(cfg)
    if provider == "auto":
        return _resolve_auto_llm(cfg)
    raise ValueError(f"unknown llm provider: {provider!r}")


def build_review_llm(cfg, llm: LlmClient) -> LlmClient:
    """The client the wiki council reviews with.

    Returns ``llm`` itself -- the generator -- unless ``[llm] review_provider`` is
    set, so an unconfigured run behaves exactly as it always has: one client for
    both roles. When it IS set it wins unconditionally, including over a
    generation provider that is already a real backend, which also allows the
    inverse split (generate with a strong model, review with a cheap one).

    Deliberately never inferred from the environment. A stray ANTHROPIC_API_KEY or
    a `claude` on PATH is not consent to bill a pay-per-token account for
    pages x council_size review calls -- the same reasoning ``cli.CliLlm`` applies
    to its own provider.
    """
    provider = (getattr(cfg, "review_provider", None) or "").lower()
    if not provider:
        return llm
    # model, api_key_env and base_url must be re-resolved rather than inherited:
    # all three were resolved for the GENERATION provider, and carrying them over
    # would e.g. send a builtin GGUF repo id as an Anthropic model name.
    review_cfg = cfg.model_copy(update={
        "provider": provider,
        "model": getattr(cfg, "review_model", None),
        "api_key_env": None,
        "base_url": None,
    })
    # `or llm`: only reachable for review_provider = "auto" resolving to nothing.
    return build_llm(review_cfg) or llm


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


def _resolve_auto_llm(cfg) -> LlmClient | None:
    """Resolve provider="auto": a reachable local Ollama that actually HAS the
    target model pulled, else the built-in LLM if llama-cpp-python is
    importable, else None (graceful skip). Never raises.

    Same fix as embeddings' _resolve_auto_embedder, same reason: the daemon
    being reachable says nothing about whether THIS model exists there. A very
    plausible real setup for this tier specifically -- Ollama pulled for one
    chat model (e.g. `qwen2.5:3b`) but not the "auto" default (`llama3.1`) --
    would otherwise still get picked and fail on first real generate() call.
    """
    base_url = getattr(cfg, "base_url", None) or default_base_url("ollama")
    model = getattr(cfg, "model", None) or "llama3.1"
    if ollama_reachable(base_url) and ollama_has_model(base_url, model):
        from .ollama import OllamaLlm

        return OllamaLlm(model=model, base_url=base_url, timeout=getattr(cfg, "timeout", 300))
    if importlib.util.find_spec("llama_cpp") is not None:
        return _build_builtin_llm(cfg)
    return None
