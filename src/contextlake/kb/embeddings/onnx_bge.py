"""The default fastembed model, run directly on onnxruntime.

``engine = "fastembed"`` with the default model ``BAAI/bge-small-en-v1.5`` used to import the
fastembed library. fastembed requires Pillow below 13, so the ``kb-fastembed`` extra pinned a
Pillow with known security advisories, and contextlake never calls Pillow on this path. This
module runs the same ONNX file with onnxruntime, tokenizers, huggingface_hub and numpy.

What matches fastembed 0.9.0, which ``tests/kb/test_onnx_bge.py`` checks against saved vectors:

- the model: Hub repo ``Qdrant/bge-small-en-v1.5-onnx-Q``, file ``model_optimized.onnx``, 384 dims
- the tokenizer: 512-token truncation, right padding to the longest text in the batch
- the pooling: the first token (CLS) of the first model output, then L2 normalisation
- the cache: the folders fastembed reads and writes under ``cache_dir``, so a model already
  downloaded by fastembed is found and not fetched again

What differs: onnxruntime runs on the CPU provider only. fastembed picks CUDA when
onnxruntime-gpu is installed.

Every import of numpy, onnxruntime, tokenizers and huggingface_hub happens inside a function.
``builtin.py`` imports this module in installs that have none of them.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

MODEL_ID = "BAAI/bge-small-en-v1.5"
REPO_ID = "Qdrant/bge-small-en-v1.5-onnx-Q"
MODEL_FILE = "model_optimized.onnx"
DIM = 384

# A folder is a usable copy when it holds these. config.json and special_tokens_map.json are
# read when present, as fastembed does.
REQUIRED = (MODEL_FILE, "tokenizer.json", "tokenizer_config.json")
# The allow-list fastembed sends to the Hub, so a download here fetches the same files.
_PATTERNS = ["config.json", "tokenizer.json", "tokenizer_config.json",
             "special_tokens_map.json", "preprocessor_config.json", MODEL_FILE]


def is_default_model(model_id: str | None) -> bool:
    """True for the one model this module runs. fastembed matches model names without case."""
    return (model_id or MODEL_ID).lower() == MODEL_ID.lower()


# --- where the model is on disk ------------------------------------------------------------

def _complete(folder: Path) -> bool:
    return all((folder / name).is_file() for name in REQUIRED)


def _snapshot_of(repo_dir: Path) -> Path | None:
    """The snapshot a Hub cache folder points at: ``refs/main``, else the newest folder."""
    snapshots = repo_dir / "snapshots"
    try:
        ref = (repo_dir / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        ref = ""
    # A ref is a commit name. Anything with a path in it is not read as one.
    if ref and Path(ref).name == ref and (snapshots / ref).is_dir():
        return snapshots / ref
    try:
        found = [p for p in snapshots.iterdir() if p.is_dir()]
    except OSError:
        return None
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def cached_folder(cache_dir) -> Path | None:
    """A complete copy of the model under ``cache_dir``, or ``None``. No network, no import.

    Three layouts, in the order fastembed 0.9.0 tries them:

    1. the Hub cache folder ``models--Qdrant--bge-small-en-v1.5-onnx-Q``;
    2. the same folder spelled with other letter case, which older fastembed releases wrote;
    3. ``<cache_dir>/bge-small-en-v1.5``, the folder of fastembed's archive download.
    """
    root = Path(cache_dir)
    want = "models--" + REPO_ID.replace("/", "--")
    names = [want]
    try:
        names += sorted(p.name for p in root.iterdir()
                        if p.is_dir() and p.name != want and p.name.lower() == want.lower())
    except OSError:
        pass
    for name in names:
        snapshot = _snapshot_of(root / name)
        if snapshot is not None and _complete(snapshot):
            return snapshot
    archive = root / MODEL_ID.split("/")[-1]
    return archive if _complete(archive) else None


def download(cache_dir) -> Path:
    """Fetch the model into ``cache_dir`` the way fastembed does."""
    from huggingface_hub import snapshot_download

    folder = Path(snapshot_download(repo_id=REPO_ID, cache_dir=str(cache_dir),
                                    allow_patterns=_PATTERNS))
    missing = [name for name in REQUIRED if not (folder / name).is_file()]
    if missing:
        raise ValueError(f"{REPO_ID} in {folder} has no {', '.join(missing)}")
    return folder


def locate(cache_dir) -> Path:
    """The model folder: the copy on disk first, the network only when it is allowed."""
    found = cached_folder(cache_dir)
    if found is not None:
        return found
    from ... import netguard

    if netguard.offline():
        raise ValueError(f"{netguard.refuse(f'downloading the model {MODEL_ID}')} "
                         f"It is not in {cache_dir}.")
    try:
        return download(cache_dir)
    except Exception as e:
        raise ValueError(f"Could not load model {MODEL_ID} from {cache_dir} "
                         f"or download it: {e}") from e


# --- the tokenizer -------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _limit(value) -> int | None:
    """``value`` when it can be a truncation limit. transformers writes 1e30 for "unknown"."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= sys.maxsize else None


def load_tokenizer(folder: Path):
    """The tokenizer as fastembed 0.9.0 builds it from the files in ``folder``."""
    from tokenizers import AddedToken, Tokenizer

    tokenizer_config = _read_json(folder / "tokenizer_config.json")
    config = _read_json(folder / "config.json")
    limits = [v for v in (_limit(tokenizer_config.get("model_max_length")),
                          _limit(tokenizer_config.get("max_length"))) if v is not None]
    if not limits:
        raise ValueError(f"Could not find a maximum context length in {folder}. Set a "
                         "positive model_max_length in tokenizer_config.json.")

    tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
    truncation = tokenizer.truncation or {}
    tokenizer.enable_truncation(
        max_length=min(limits),
        direction=tokenizer_config.get("truncation_side") or truncation.get("direction", "right"),
    )
    for value in _read_json(folder / "special_tokens_map.json").values():
        for token in value if isinstance(value, list) else [value]:
            if isinstance(token, str):
                tokenizer.add_special_tokens([token])
            elif isinstance(token, dict):
                tokenizer.add_special_tokens([AddedToken(**token)])

    padding = tokenizer.padding or {}
    pad_token = padding.get("pad_token") or tokenizer_config.get("pad_token")
    if pad_token is None:
        raise ValueError(f"Could not find a pad token for {folder}")
    pad_id = padding.get("pad_id", config.get("pad_token_id"))
    if pad_id is None:
        pad_id = tokenizer.token_to_id(pad_token)
    if pad_id is None:
        raise ValueError(f"Could not find an id for the pad token {pad_token!r} in {folder}")
    tokenizer.enable_padding(
        direction=tokenizer_config.get("padding_side") or padding.get("direction", "right"),
        pad_id=pad_id,
        pad_type_id=padding.get("pad_type_id", 0),
        pad_token=pad_token,
        pad_to_multiple_of=padding.get("pad_to_multiple_of"),
        length=None,
    )
    return tokenizer


# --- the model -----------------------------------------------------------------------------

def pool_cls(output):
    """The CLS vector of each text: the first token of a (batch, tokens, dim) output."""
    if output.ndim == 3:
        return output[:, 0]
    if output.ndim == 2:
        return output
    raise ValueError(f"Unsupported embedding shape: {output.shape}")


def l2_normalize(vectors, eps: float = 1e-12):
    """Scale each row to length 1. A row shorter than ``eps`` is divided by ``eps``."""
    import numpy as np

    norm = np.maximum(np.linalg.norm(vectors, ord=2, axis=1, keepdims=True), eps)
    return vectors / norm


class BgeOnnx:
    """``TextEmbedding.embed`` of fastembed for the default model, on an onnxruntime session."""

    def __init__(self, tokenizer, session):
        self.tokenizer = tokenizer
        self.session = session
        self._inputs = {node.name for node in session.get_inputs()}

    def embed(self, texts: list[str], batch_size: int = 256) -> Iterator:
        """One normalised vector per text, in order. Batches only change the padding."""
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        for start in range(0, len(texts), batch_size):
            yield from self._embed_batch(texts[start:start + batch_size])

    def _embed_batch(self, texts: list[str]):
        import numpy as np

        encoded = self.tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        feed = {"input_ids": input_ids}
        if "attention_mask" in self._inputs:
            feed["attention_mask"] = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(input_ids)
        output = self.session.run(None, feed)[0]
        return l2_normalize(pool_cls(output))


def open_model(folder: Path) -> BgeOnnx:
    """Start an onnxruntime session on the model file in ``folder``."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(str(folder / MODEL_FILE), sess_options=options,
                                   providers=["CPUExecutionProvider"])
    return BgeOnnx(load_tokenizer(folder), session)


def load(cache_dir) -> BgeOnnx:
    """The model, found on disk or fetched, ready to embed."""
    return open_model(locate(cache_dir))
