"""The embedding-provider interface and a config-driven factory.

Providers are local-first and entirely optional. A provider turns text into
fixed-length vectors; the rest of the knowledge layer stays provider-agnostic.
"""

from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod

from .._util import ollama_has_model, ollama_reachable


class Embedder(ABC):
    """Turns text into vectors. Implementations carry their own model/runtime."""

    name: str = "embedder"

    @property
    def identity(self) -> str:
        """Stable ``provider:model`` string used by the vector-store guard to
        detect a store being re-embedded with a different model. Subclasses may
        override for a more specific identity."""
        model = getattr(self, "model", None) or getattr(self, "model_id", "")
        return f"{self.name}:{model}"

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""


def embedder_runtime_state(embedder) -> tuple[bool | None, str]:
    """Whether ``embedder`` could actually answer a query, and why not.

    ``build_embedder`` returns a *configured candidate*: constructing one is
    deliberately cheap and never touches the network, so a non-None return says
    the config named a provider, not that the provider works here. The built-in
    embedders only import their engine and fetch their model on the first
    ``embed()``, which is why a caller can report a capability as available and
    still fail on the first real query.

    Three states, mirroring how ``doctor`` reports the wiki LLM tier:

    - ``True``  usable right now.
    - ``None``  usable only with network: the engine is installed but its model
      is not in the local cache, so the first query has to fetch it.
    - ``False`` unusable here: the engine's optional extra is not installed, and
      no amount of network fixes that without an install.

    Probed the same import-free, offline way ``doctor`` probes (``find_spec``
    plus a look at the model cache) rather than by calling ``embed()``. A real
    call is the only *certain* answer, but it downloads the model from the Hub
    inside server startup and spends the remote providers' circuit-breaker
    budget before any client has asked for anything -- paying a network round
    trip, and possibly a large download, to print one accurate banner line.

    Remote providers (ollama, openai) hold no local model, so there is nothing
    to check without a request; they report ``True`` and their reachability
    surfaces where it always did, on the query that needs them.
    """
    if embedder is None:
        return False, "no embedder configured"
    engine = getattr(embedder, "engine", None)
    model_id = getattr(embedder, "model_id", None)
    cache_dir = getattr(embedder, "cache_dir", None)
    # Duck-typed rather than isinstance(BuiltinEmbedder): importing the class
    # here would drag the builtin module into every caller, and a test double
    # carrying the same three attributes deserves the same answer.
    if not (engine and model_id and cache_dir):
        return True, ""
    from .builtin import _ENGINE_EXTRA

    if importlib.util.find_spec(engine) is None:
        extra = _ENGINE_EXTRA.get(engine, "kb-local")
        return False, (f"the {engine!r} engine is not installed "
                       f"(pip install 'contextlake[{extra}]')")
    from pathlib import Path

    hub = Path(cache_dir).expanduser() / "hub"
    if not (hub / ("models--" + model_id.replace("/", "--"))).is_dir():
        return None, (f"model {model_id} is not downloaded yet; the first query "
                      "fetches it, and fails if this machine is offline")
    return True, ""


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


def _resolve_auto_embedder(cfg) -> Embedder | None:
    """Resolve provider="auto": a reachable local Ollama that actually HAS the
    target embedding model pulled, else the built-in embedder if its extra is
    importable, else None (graceful skip). Never raises.

    The daemon being reachable at all is not enough to commit to Ollama: a very
    common real setup is Ollama running for chat models (e.g. just `qwen2.5`)
    with no embedding model ever pulled, which "auto" used to pick anyway --
    reachability checks /api/tags for *a* response, not whether the specific
    model this call is about to request exists, so every embed() call failed
    with Ollama's own real 404 ("model ... not found, try pulling it first")
    instead of "auto" falling through to a provider that actually works.
    """
    base_url = getattr(cfg, "base_url", "http://127.0.0.1:11434")
    model = getattr(cfg, "model", None) or "nomic-embed-text"
    if ollama_reachable(base_url) and ollama_has_model(base_url, model):
        from .ollama import OllamaEmbedder

        return OllamaEmbedder(model=model, base_url=base_url,
                              batch_size=getattr(cfg, "batch_size", 64))
    engine = (getattr(cfg, "engine", "model2vec") or "model2vec").lower()
    module = "fastembed" if engine == "fastembed" else "model2vec"
    # find_spec locates the package without importing the heavy module.
    if importlib.util.find_spec(module) is not None:
        return _build_builtin_embedder(cfg)
    return None
