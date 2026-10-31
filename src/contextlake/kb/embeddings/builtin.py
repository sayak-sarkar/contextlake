"""Built-in, CPU-only embeddings — no Ollama daemon and no API key required.

Two interchangeable engines, each auto-downloading a small model to the cache
dir (default ``~/.contextlake/models``) on first use:

- ``model2vec`` (default): static potion embeddings, numpy inference, ~30MB. Very
  fast on CPU at scale. Needs the ``kb-local`` extra.
- ``fastembed``: ONNX bge-small, higher quality, about 67MB. Needs the ``kb-fastembed``
  extra, which installs onnxruntime, tokenizers, huggingface_hub and numpy. The default model
  runs on those directly (``onnx_bge.py``) and gives the vectors the fastembed library gives.
  The fastembed library is not installed and not imported for it, because fastembed requires
  Pillow below 13. Any other model id with this engine still needs ``pip install fastembed``.

The model is loaded **lazily** on the first ``embed()`` call, so importing and
constructing this class is cheap and never touches the network — which keeps the
provider factory and its tests offline/CI-safe.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from . import onnx_bge
from .base import Embedder

DEFAULT_CACHE_DIR = "~/.contextlake/models"

# Engine -> (default model id, the extra that provides it). All permissively
# licensed: potion-base-8M (MIT), bge-small-en-v1.5 (MIT).
_ENGINE_DEFAULT_MODEL = {
    "model2vec": "minishlab/potion-base-8M",
    "fastembed": "BAAI/bge-small-en-v1.5",
}
_ENGINE_EXTRA = {"model2vec": "kb-local", "fastembed": "kb-fastembed"}

# What the default fastembed model imports. This is the kb-fastembed extra.
_ONNX_MODULES = ("onnxruntime", "tokenizers", "huggingface_hub", "numpy")


def engine_modules(engine: str, model_id: str | None = None) -> tuple[str, ...]:
    """The top-level modules ``engine`` imports to run ``model_id``.

    ``fastembed`` with its default model needs no fastembed library, so probing for that
    module would call a working install broken. Any other fastembed model still needs it.
    """
    if engine != "fastembed":
        return ("model2vec",)
    return _ONNX_MODULES if onnx_bge.is_default_model(model_id) else ("fastembed",)


def engine_installed(engine: str, model_id: str | None = None) -> bool:
    """True if every module ``engine_modules`` names can be found. Imports none of them."""
    for name in engine_modules(engine, model_id):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):   # a broken or partial install counts as absent
            return False
    return True


def install_command(engine: str, model_id: str | None = None) -> str:
    """The ``pip install`` line that supplies ``engine_modules``."""
    if engine_modules(engine, model_id) == ("fastembed",):
        return "pip install fastembed"
    return f"pip install 'contextlake[{_ENGINE_EXTRA.get(engine, 'kb-local')}]'"


def model_folder(engine: str, model_id: str | None, cache_dir) -> Path:
    """The folder ``engine`` reads ``model_id`` from, worked out the way the loader does.

    The default fastembed model is read from ``cache_dir`` itself, in fastembed's layout
    (``onnx_bge.cached_folder``). Every other engine and model reads the Hub cache folder,
    which ``HF_HUB_CACHE`` or ``HF_HOME`` can move (``model_cache.hub_dir``).
    """
    if engine == "fastembed" and onnx_bge.is_default_model(model_id):
        return Path(cache_dir)
    from ..model_cache import hub_dir

    return hub_dir(cache_dir)


def model_cached(engine: str, model_id: str, cache_dir) -> bool:
    """True if ``engine`` finds ``model_id`` on disk, so a load needs no network.

    ``doctor`` and the ``serve`` banner both ask here, so they give one answer. The default
    fastembed model is found only as a complete copy (``onnx_bge.cached_folder``), the same
    test its loader uses. Other models need a Hub cache folder (``model_cache.is_cached``).
    Imports nothing heavy.
    """
    if engine == "fastembed" and onnx_bge.is_default_model(model_id):
        return onnx_bge.cached_folder(cache_dir) is not None
    from ..model_cache import is_cached

    return is_cached(cache_dir, model_id)


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
        from .._util import hush_hf_hub
        hush_hf_hub()   # the model is downloaded, not uploaded — quiet HF's noisy notices
        if self.engine == "model2vec":
            try:
                from model2vec import StaticModel
            except ImportError as e:
                raise self._missing_extra_error(e) from e
            self._model = self._load_model2vec(StaticModel)
        elif onnx_bge.is_default_model(self.model_id):   # fastembed's default model
            self._model = self._load_onnx_bge()
        else:   # another fastembed model: the library itself
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise ImportError(
                    f"The model {self.model_id!r} needs the fastembed library. The "
                    "'kb-fastembed' extra does not install it, because fastembed requires "
                    "Pillow below 13, and the newest Pillow below 13 has security advisories "
                    f"fixed only in 13. The default model {onnx_bge.MODEL_ID!r} does not "
                    "need it. Install it with: pip install fastembed"
                ) from e
            self._model = self._load_fastembed(TextEmbedding)
        return self._model

    def _load_onnx_bge(self):
        """The default fastembed model on onnxruntime. Disk first, then the network.

        Reads the cache folders fastembed writes under `cache_dir`, so a model it already
        downloaded is found. The network is used only when no complete copy is on disk and
        offline mode is off; offline, the error names the model and the folder.
        """
        try:
            import huggingface_hub  # noqa: F401
            import numpy  # noqa: F401
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        except ImportError as e:
            raise self._missing_extra_error(e) from e
        return onnx_bge.load(self.cache_dir)

    def _load_model2vec(self, static_model):
        """Load from a FOLDER, so model2vec never contacts the Hub for a model on disk.

        Given a repo id, `from_pretrained` defaults to `force_download=True` and asks
        the Hub on every load. Given a folder that exists, it reads the folder.
        """
        if Path(self.model_id).exists():
            return static_model.from_pretrained(self.model_id)
        from ..model_cache import fetch_snapshot

        folder = fetch_snapshot(self.model_id)
        try:
            return static_model.from_pretrained(folder)
        except Exception:
            # A partial copy from an interrupted download: complete it once online.
            from ... import netguard

            if netguard.offline():
                raise
            return static_model.from_pretrained(fetch_snapshot(self.model_id, complete=True))

    def _load_fastembed(self, text_embedding):
        """The fastembed library, for a model other than the default one.

        fastembed's own cache first (`local_files_only=True`), then the network.

        fastembed keeps its own cache layout under `cache_dir`, so the lookup stays inside
        fastembed: moving it to a hub snapshot path would download every user's model
        again. Without the flag it asks the Hub on every load.
        """
        kw = {"model_name": self.model_id, "cache_dir": str(self.cache_dir)}
        try:
            return text_embedding(**kw, local_files_only=True)
        except TypeError:
            pass   # a fastembed too old for the flag: load as it always did
        except Exception:
            from ... import netguard

            if netguard.offline():
                raise
        return text_embedding(**kw)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        if self.engine == "model2vec":
            vecs = model.encode(texts)  # numpy array (n, dim)
        else:
            vecs = list(model.embed(texts, batch_size=self.batch_size))  # list of arrays
        return [[float(x) for x in row] for row in vecs]
