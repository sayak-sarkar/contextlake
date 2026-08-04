"""Optional semantic-search tier: pluggable, local-first text embeddings.

Nothing here runs unless ``[embeddings] enabled = true`` in config. The
``Embedder`` interface keeps the provider generic; Ollama ships first.
"""

from .base import Embedder, build_embedder, embedder_runtime_state

__all__ = ["Embedder", "build_embedder", "embedder_runtime_state"]
