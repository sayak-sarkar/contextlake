"""Built-in, CPU-only embeddings — no Ollama daemon and no API key required.

Two interchangeable engines, each auto-downloading a small model to the cache
dir (default ``~/.contextlake/models``) on first use:

- ``model2vec`` (default): static potion embeddings, numpy inference, ~30MB. Very
  fast on CPU at scale. Needs the ``kb-local`` extra.
- ``fastembed``: ONNX bge-small via onnxruntime, higher quality, ~90MB. Needs the
  ``kb-fastembed`` extra.

The model is loaded **lazily** on the first ``embed()`` call, so importing and
constructing this class is cheap and never touches the network — which keeps the
provider factory and its tests offline/CI-safe.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import Embedder

DEFAULT_CACHE_DIR = "~/.contextlake/models"

# Engine -> (default model id, the extra that provides it). All permissively
# licensed: potion-base-8M (MIT), bge-small-en-v1.5 (MIT).
_ENGINE_DEFAULT_MODEL = {
    "model2vec": "minishlab/potion-base-8M",
    "fastembed": "BAAI/bge-small-en-v1.5",
}
_ENGINE_EXTRA = {"model2vec": "kb-local", "fastembed": "kb-fastembed"}


class BuiltinEmbedder(Embedder):
    """A local CPU embedder backed by ``model2vec`` (default) or ``fastembed``."""

    name = "builtin"

    def __init__(self, *, engine: str = "model2vec", model: str | None = None,
                 cache_dir: str | None = None, batch_size: int = 64):
        engine = (engine or "model2vec").lower()
        if engine not in _ENGINE_DEFAULT_MODEL:
            raise ValueError(
                f"unknown builtin embedder engine: {engine!r} "
                f"(expected one of {sorted(_ENGINE_DEFAULT_MODEL)})"
            )
        self.engine = engine
        self.model_id = model or _ENGINE_DEFAULT_MODEL[engine]
        self.cache_dir = Path(os.path.expanduser(cache_dir or DEFAULT_CACHE_DIR))
        self.batch_size = batch_size
        self._model = None  # loaded lazily on first embed()

    @property
    def identity(self) -> str:
        """Stable identity string for the vector-store dimension guard."""
        return f"builtin:{self.engine}:{self.model_id}"

    def _missing_extra_error(self, exc: Exception) -> ImportError:
        extra = _ENGINE_EXTRA[self.engine]
        return ImportError(
            f"The built-in '{self.engine}' embedder needs the '{extra}' extra. "
            f"Install it with: pip install 'contextlake[{extra}]'"
        )

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        # Route HuggingFace downloads to our cache dir, but defer to an existing
        # HF_HOME (e.g. the prebuilt Docker image bakes models into one).
        os.environ.setdefault("HF_HOME", str(self.cache_dir))
        if self.engine == "model2vec":
            try:
                from model2vec import StaticModel
            except ImportError as e:
                raise self._missing_extra_error(e) from e
            self._model = StaticModel.from_pretrained(self.model_id)
        else:  # fastembed
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise self._missing_extra_error(e) from e
            self._model = TextEmbedding(
                model_name=self.model_id, cache_dir=str(self.cache_dir)
            )
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        if self.engine == "model2vec":
            vecs = model.encode(texts)  # numpy array (n, dim)
        else:
            vecs = list(model.embed(texts, batch_size=self.batch_size))  # list of arrays
        return [[float(x) for x in row] for row in vecs]
