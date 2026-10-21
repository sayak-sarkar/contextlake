"""The default fastembed model runs on onnxruntime, with no fastembed and no Pillow.

`engine = "fastembed"` with `BAAI/bge-small-en-v1.5` used to import the fastembed library,
which requires Pillow below 13. `kb/embeddings/onnx_bge.py` runs the same ONNX file directly.

Two kinds of test live here:

- One equivalence test compares real vectors with vectors saved from fastembed 0.9.0. It needs
  the model on disk, so it runs only when `CONTEXTLAKE_TEST_BGE_CACHE` names a folder that
  holds it, and CI skips it.
- The rest need no model. They pin the pooling, the normalisation, the truncation, the
  batching, the order of disk and network, and that fastembed is never imported.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

from contextlake import netguard
from contextlake.kb.embeddings import onnx_bge
from contextlake.kb.embeddings.builtin import (
    BuiltinEmbedder,
    engine_installed,
    engine_modules,
    install_command,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "embeddings"
           / "bge-small-en-v1.5-fastembed-0.9.0.json")
CACHE_ENV = "CONTEXTLAKE_TEST_BGE_CACHE"
FOUR = ("numpy", "onnxruntime", "tokenizers", "huggingface_hub")
ONNX_MODULES = ("onnxruntime", "tokenizers", "huggingface_hub", "numpy")


def _need_the_four():
    for name in FOUR:
        pytest.importorskip(name)


# --- equivalence with fastembed 0.9.0 ----------------------------------------------------

def _reference():
    ref = json.loads(FIXTURE.read_text(encoding="utf-8"))
    long_text = " ".join(f"token{i} alpha beta gamma" for i in range(400))
    ref["texts"] = [long_text if t == "@@LONG@@" else t for t in ref["texts"]]
    return ref


@pytest.mark.skipif(not os.environ.get(CACHE_ENV),
                    reason=f"set {CACHE_ENV} to a cache folder that holds the model")
@pytest.mark.parametrize("batch_size", [3, 1, 64])
def test_vectors_match_fastembed_0_9_0(batch_size, monkeypatch):
    """Every vector is the one fastembed 0.9.0 gave. Batch size 3 is the one the reference
    used; 1 and 64 show that the padding added to a batch does not move a vector."""
    _need_the_four()
    cache = Path(os.environ[CACHE_ENV])
    assert onnx_bge.cached_folder(cache) is not None, f"no complete model copy in {cache}"
    monkeypatch.setenv("HF_HOME", str(cache))   # the loader sets it when unset, and leaks it
    ref = _reference()
    assert len(ref["texts"][6]) > 10000, "the long text must be cut at 512 tokens"

    embedder = BuiltinEmbedder(engine="fastembed", cache_dir=str(cache), batch_size=batch_size)
    got = embedder.embed(ref["texts"])

    assert len(got) == len(ref["vectors"]) == 8
    for text, mine, theirs in zip(ref["texts"], got, ref["vectors"], strict=True):
        assert len(mine) == ref["dim"] == len(theirs)
        dot = sum(a * b for a, b in zip(mine, theirs, strict=True))
        norm = (sum(a * a for a in mine) * sum(b * b for b in theirs)) ** 0.5
        assert dot / norm >= 0.99999, text[:30]
        assert max(abs(a - b) for a, b in zip(mine, theirs, strict=True)) <= 1e-5, text[:30]


# --- pooling and normalisation -----------------------------------------------------------

def test_pool_cls_takes_the_first_token_of_each_text():
    np = pytest.importorskip("numpy")
    out = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    assert onnx_bge.pool_cls(out).tolist() == out[:, 0].tolist()
    assert onnx_bge.pool_cls(out).tolist() != out.mean(axis=1).tolist()


def test_pool_cls_passes_a_pooled_output_through_and_refuses_other_shapes():
    np = pytest.importorskip("numpy")
    flat = np.ones((2, 4), dtype=np.float32)
    assert onnx_bge.pool_cls(flat) is flat
    with pytest.raises(ValueError, match="Unsupported embedding shape"):
        onnx_bge.pool_cls(np.ones(4, dtype=np.float32))


def test_l2_normalize_gives_rows_of_length_one():
    np = pytest.importorskip("numpy")
    rows = onnx_bge.l2_normalize(np.array([[3.0, 4.0], [0.0, 2.0]], dtype=np.float32))
    assert rows.tolist() == [[0.6000000238418579, 0.800000011920929], [0.0, 1.0]]


def test_l2_normalize_keeps_a_zero_row_finite():
    np = pytest.importorskip("numpy")
    rows = onnx_bge.l2_normalize(np.zeros((1, 3), dtype=np.float32))
    assert rows.tolist() == [[0.0, 0.0, 0.0]]


# --- the tokenizer: truncation and padding ------------------------------------------------

def _tiny_tokenizer_folder(tmp_path, *, tokenizer_config=None):
    tokenizers = pytest.importorskip("tokenizers")
    words = ["[PAD]", "[UNK]", "a", "b", "c", "d"]
    tok = tokenizers.Tokenizer(tokenizers.models.WordLevel(
        {w: i for i, w in enumerate(words)}, unk_token="[UNK]"))
    tok.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    folder = tmp_path / "tiny"
    folder.mkdir()
    tok.save(str(folder / "tokenizer.json"))
    config = {"model_max_length": 5, "pad_token": "[PAD]"}
    config.update(tokenizer_config or {})
    (folder / "tokenizer_config.json").write_text(json.dumps(config))
    (folder / "config.json").write_text(json.dumps({"pad_token_id": 0}))
    return folder


def test_tokenizer_cuts_at_the_limit_and_pads_to_the_longest_in_the_batch(tmp_path):
    tok = onnx_bge.load_tokenizer(_tiny_tokenizer_folder(tmp_path))
    cut = tok.encode_batch(["a b c d a b c d"])
    assert len(cut[0].ids) == 5                      # model_max_length is 5
    padded = tok.encode_batch(["a b", "a b c"])
    assert [len(e.ids) for e in padded] == [3, 3]    # the longest text sets the width
    assert padded[0].ids == [2, 3, 0]                # padded on the right with the pad id
    assert padded[0].attention_mask == [1, 1, 0]


def test_tokenizer_takes_the_smaller_of_the_two_limits(tmp_path):
    folder = _tiny_tokenizer_folder(tmp_path, tokenizer_config={"max_length": 3})
    tok = onnx_bge.load_tokenizer(folder)
    assert len(tok.encode("a b c d a b").ids) == 3


def test_tokenizer_refuses_a_limit_that_is_not_a_real_number(tmp_path):
    folder = _tiny_tokenizer_folder(tmp_path, tokenizer_config={"model_max_length": 1e30})
    with pytest.raises(ValueError, match="maximum context length"):
        onnx_bge.load_tokenizer(folder)


# --- batching, inputs and order, on a fake session ---------------------------------------

class _Enc:
    def __init__(self, text):
        self.ids = [int(text), 5, 5]
        self.attention_mask = [1, 1, 1]


class _Tokenizer:
    def encode_batch(self, texts):
        return [_Enc(t) for t in texts]


class _Session:
    """Answers (batch, 3, 4). Token 0 carries the text's number, the other tokens carry 100s."""

    def __init__(self, names=("input_ids", "attention_mask", "token_type_ids")):
        self.names = names
        self.feeds = []

    def get_inputs(self):
        return [types.SimpleNamespace(name=n) for n in self.names]

    def run(self, output_names, feed):
        import numpy as np

        self.feeds.append(feed)
        ids = feed["input_ids"]
        out = np.full((ids.shape[0], 3, 4), 100.0, dtype=np.float32)
        out[:, 0, :] = 0.0
        out[:, 0, 0] = ids[:, 0] + 1
        out[:, 0, 1] = 1.0
        return [out]


def _model(session=None):
    pytest.importorskip("numpy")
    return onnx_bge.BgeOnnx(_Tokenizer(), session or _Session())


def test_embed_runs_texts_in_batches_and_keeps_their_order():
    session = _Session()
    texts = [str(i) for i in range(8)]
    vectors = list(_model(session).embed(texts, batch_size=3))
    assert [f["input_ids"].shape[0] for f in session.feeds] == [3, 3, 2]
    assert len(vectors) == 8
    for i, v in enumerate(vectors):
        norm = ((i + 1) ** 2 + 1) ** 0.5
        assert v.tolist() == pytest.approx([(i + 1) / norm, 1 / norm, 0.0, 0.0], abs=1e-6)


def test_embed_uses_the_first_token_not_the_mean():
    vector = next(iter(_model().embed(["0"])))
    assert vector.tolist() == pytest.approx([2 ** -0.5, 2 ** -0.5, 0.0, 0.0], abs=1e-6)


def test_embed_refuses_a_batch_size_below_one():
    with pytest.raises(ValueError, match="batch_size"):
        list(_model().embed(["1"], batch_size=0))


def test_embed_of_no_texts_runs_nothing():
    session = _Session()
    assert list(_model(session).embed([])) == []
    assert session.feeds == []


def test_embed_feeds_only_the_inputs_the_model_declares():
    session = _Session(names=("input_ids", "attention_mask"))
    list(_model(session).embed(["1", "2"]))
    assert sorted(session.feeds[0]) == ["attention_mask", "input_ids"]


def test_embed_feeds_zero_token_types_when_the_model_declares_them():
    session = _Session()
    list(_model(session).embed(["1", "2"]))
    feed = session.feeds[0]
    assert sorted(feed) == ["attention_mask", "input_ids", "token_type_ids"]
    assert feed["token_type_ids"].shape == feed["input_ids"].shape
    assert not feed["token_type_ids"].any()
    assert str(feed["input_ids"].dtype) == "int64"


# --- where the model is on disk -----------------------------------------------------------

REV = "a" * 40


def _folder(path, *, model=True, tokenizer=True):
    path.mkdir(parents=True, exist_ok=True)
    if tokenizer:
        (path / "tokenizer.json").write_text("{}")
        (path / "tokenizer_config.json").write_text("{}")
    if model:
        (path / onnx_bge.MODEL_FILE).write_bytes(b"x")
    return path


def _hub(cache, *, name="models--Qdrant--bge-small-en-v1.5-onnx-Q", rev=REV, ref=True, **kw):
    repo = cache / name
    snapshot = _folder(repo / "snapshots" / rev, **kw)
    if ref:
        (repo / "refs").mkdir(exist_ok=True)
        (repo / "refs" / "main").write_text(rev)
    return snapshot


def test_a_copy_in_the_fastembed_hub_layout_is_found(tmp_path):
    snapshot = _hub(tmp_path)
    assert onnx_bge.cached_folder(tmp_path) == snapshot


def test_a_copy_spelled_with_other_letter_case_is_found(tmp_path):
    snapshot = _hub(tmp_path, name="models--qdrant--bge-small-en-v1.5-onnx-q")
    assert onnx_bge.cached_folder(tmp_path) == snapshot


def test_a_copy_in_the_archive_folder_is_found(tmp_path):
    folder = _folder(tmp_path / "bge-small-en-v1.5")
    assert onnx_bge.cached_folder(tmp_path) == folder


def test_the_snapshot_that_refs_main_names_is_the_one_used(tmp_path):
    _hub(tmp_path, rev="b" * 40, ref=False)
    named = _hub(tmp_path, rev="c" * 40)
    assert onnx_bge.cached_folder(tmp_path) == named


def test_a_ref_that_is_a_path_is_not_followed(tmp_path):
    snapshot = _hub(tmp_path, ref=False)
    repo = snapshot.parent.parent
    outside = _folder(tmp_path / "outside")
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("../../outside")
    assert onnx_bge.cached_folder(tmp_path) == snapshot
    assert outside.is_dir()


def test_a_snapshot_without_the_model_file_is_not_a_copy(tmp_path):
    _hub(tmp_path, model=False)
    assert onnx_bge.cached_folder(tmp_path) is None


def test_a_snapshot_without_the_tokenizer_is_not_a_copy(tmp_path):
    _hub(tmp_path, tokenizer=False)
    assert onnx_bge.cached_folder(tmp_path) is None


def test_an_empty_or_missing_cache_has_no_copy(tmp_path):
    assert onnx_bge.cached_folder(tmp_path) is None
    assert onnx_bge.cached_folder(tmp_path / "nowhere") is None


@pytest.fixture
def online(monkeypatch):
    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)


def test_a_copy_on_disk_is_used_without_the_network(tmp_path, online, monkeypatch):
    snapshot = _hub(tmp_path)
    monkeypatch.setattr(onnx_bge, "download", lambda cache: pytest.fail("went to the network"))
    assert onnx_bge.locate(tmp_path) == snapshot


def test_with_no_copy_the_network_is_used_once(tmp_path, online, monkeypatch):
    calls = []
    monkeypatch.setattr(onnx_bge, "download", lambda cache: calls.append(cache) or tmp_path)
    assert onnx_bge.locate(tmp_path) == tmp_path
    assert calls == [tmp_path]


def test_offline_with_no_copy_raises_and_does_not_download(tmp_path, monkeypatch):
    monkeypatch.setenv(netguard.OFFLINE_ENV, "1")
    monkeypatch.setattr(onnx_bge, "download", lambda cache: pytest.fail("went to the network"))
    with pytest.raises(ValueError) as err:
        onnx_bge.locate(tmp_path)
    assert onnx_bge.MODEL_ID in str(err.value) and str(tmp_path) in str(err.value)
    assert "offline mode" in str(err.value)


def test_offline_with_a_copy_on_disk_still_loads(tmp_path, monkeypatch):
    monkeypatch.setenv(netguard.OFFLINE_ENV, "1")
    snapshot = _hub(tmp_path)
    assert onnx_bge.locate(tmp_path) == snapshot


def test_a_failed_download_is_a_value_error_that_names_the_model(tmp_path, online, monkeypatch):
    def fail(cache):
        raise OSError("connection refused")

    monkeypatch.setattr(onnx_bge, "download", fail)
    with pytest.raises(ValueError, match="connection refused") as err:
        onnx_bge.locate(tmp_path)
    assert onnx_bge.MODEL_ID in str(err.value)
    assert isinstance(err.value.__cause__, OSError)


def test_a_snapshot_that_lacks_the_model_after_download_is_refused(tmp_path, monkeypatch):
    hub = pytest.importorskip("huggingface_hub")
    incomplete = _folder(tmp_path / "snap", model=False)
    monkeypatch.setattr(hub, "snapshot_download", lambda **kw: str(incomplete))
    with pytest.raises(ValueError, match=onnx_bge.MODEL_FILE):
        onnx_bge.download(tmp_path)


def test_the_download_asks_for_fastembeds_repo_files_and_cache_dir(tmp_path, monkeypatch):
    hub = pytest.importorskip("huggingface_hub")
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return str(_folder(tmp_path / "snap"))

    monkeypatch.setattr(hub, "snapshot_download", fake)
    onnx_bge.download(tmp_path)
    assert seen["repo_id"] == "Qdrant/bge-small-en-v1.5-onnx-Q"
    assert seen["cache_dir"] == str(tmp_path)
    assert onnx_bge.MODEL_FILE in seen["allow_patterns"]
    assert "tokenizer.json" in seen["allow_patterns"]


# --- the engine: fastembed is never imported for the default model ------------------------

class _Ready:
    """Stands in for the loaded model."""

    def embed(self, texts, batch_size=256):
        import numpy as np

        return iter(np.ones((len(texts), 2), dtype=np.float32))


def test_the_default_model_never_imports_fastembed(tmp_path, monkeypatch):
    _need_the_four()
    monkeypatch.setitem(sys.modules, "fastembed", None)   # `import fastembed` now raises
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setattr(onnx_bge, "load", lambda cache_dir: _Ready())
    embedder = BuiltinEmbedder(engine="fastembed", cache_dir=str(tmp_path))
    assert embedder.embed(["a", "b"]) == [[1.0, 1.0], [1.0, 1.0]]
    assert embedder.identity == "builtin:fastembed:BAAI/bge-small-en-v1.5"


def test_embed_opens_the_copy_on_disk_and_never_downloads(tmp_path, online, monkeypatch):
    _need_the_four()
    snapshot = _hub(tmp_path)
    opened = []
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setattr(onnx_bge, "download", lambda cache: pytest.fail("went to the network"))
    monkeypatch.setattr(onnx_bge, "open_model", lambda folder: opened.append(folder) or _Ready())
    BuiltinEmbedder(engine="fastembed", cache_dir=str(tmp_path)).embed(["a"])
    assert opened == [snapshot]


def test_embed_offline_with_no_copy_raises_and_does_not_download(tmp_path, monkeypatch):
    _need_the_four()
    monkeypatch.setenv(netguard.OFFLINE_ENV, "1")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setattr(onnx_bge, "download", lambda cache: pytest.fail("went to the network"))
    with pytest.raises(ValueError, match="offline mode"):
        BuiltinEmbedder(engine="fastembed", cache_dir=str(tmp_path)).embed(["a"])


def test_the_default_model_loads_from_the_cache_dir_it_was_given(tmp_path, monkeypatch):
    _need_the_four()
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    seen = []
    monkeypatch.setattr(onnx_bge, "load", lambda cache_dir: seen.append(cache_dir) or _Ready())
    BuiltinEmbedder(engine="fastembed", cache_dir=str(tmp_path / "models")).embed(["a"])
    assert seen == [tmp_path / "models"]


def test_the_default_model_is_matched_without_regard_to_letter_case(tmp_path, monkeypatch):
    _need_the_four()
    monkeypatch.setitem(sys.modules, "fastembed", None)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setattr(onnx_bge, "load", lambda cache_dir: _Ready())
    embedder = BuiltinEmbedder(engine="fastembed", model="baai/BGE-small-en-v1.5",
                               cache_dir=str(tmp_path))
    assert embedder.embed(["a"]) == [[1.0, 1.0]]


def test_the_default_model_without_the_extra_names_kb_fastembed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "onnxruntime", None)   # `import onnxruntime` now raises
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    with pytest.raises(ImportError, match=r"kb-fastembed"):
        BuiltinEmbedder(engine="fastembed", cache_dir=str(tmp_path)).embed(["a"])


def test_another_model_uses_the_fastembed_library_when_it_is_there(tmp_path, monkeypatch):
    class TextEmbedding:
        def __init__(self, **kw):
            self.kw = kw

        def embed(self, texts, batch_size):
            return iter([[0.5, 0.5] for _ in texts])

    monkeypatch.setitem(sys.modules, "fastembed",
                        types.SimpleNamespace(TextEmbedding=TextEmbedding))
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setattr(onnx_bge, "load", lambda cache_dir: pytest.fail("took the direct path"))
    embedder = BuiltinEmbedder(engine="fastembed", model="BAAI/bge-base-en-v1.5",
                               cache_dir=str(tmp_path))
    assert embedder.embed(["a"]) == [[0.5, 0.5]]
    assert embedder._model.kw["model_name"] == "BAAI/bge-base-en-v1.5"


def test_another_model_without_the_library_says_to_install_fastembed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    embedder = BuiltinEmbedder(engine="fastembed", model="BAAI/bge-base-en-v1.5",
                               cache_dir=str(tmp_path))
    with pytest.raises(ImportError, match=r"pip install fastembed") as err:
        embedder.embed(["a"])
    assert "Pillow" in str(err.value)
    assert "kb-fastembed" in str(err.value)    # names the extra that does not supply it


# --- what each engine needs, for doctor, auto and the serve banner -----------------------

@pytest.mark.parametrize("engine,model,modules", [
    ("model2vec", None, ("model2vec",)),
    ("fastembed", None, ONNX_MODULES),
    ("fastembed", "BAAI/bge-small-en-v1.5", ONNX_MODULES),
    ("fastembed", "baai/bge-small-en-v1.5", ONNX_MODULES),
    ("fastembed", "BAAI/bge-base-en-v1.5", ("fastembed",)),
])
def test_engine_modules(engine, model, modules):
    assert engine_modules(engine, model) == modules


@pytest.mark.parametrize("engine,model,command", [
    ("model2vec", None, "pip install 'contextlake[kb-local]'"),
    ("fastembed", None, "pip install 'contextlake[kb-fastembed]'"),
    ("fastembed", "BAAI/bge-base-en-v1.5", "pip install fastembed"),
])
def test_install_command(engine, model, command):
    assert install_command(engine, model) == command


def test_a_fastembed_engine_is_installed_without_the_fastembed_library(monkeypatch):
    import importlib.util as util

    present = set(FOUR)
    monkeypatch.setattr(util, "find_spec",
                        lambda name, *a, **k: object() if name in present else None)
    assert engine_installed("fastembed") is True
    assert engine_installed("fastembed", "BAAI/bge-base-en-v1.5") is False
    present.discard("tokenizers")
    assert engine_installed("fastembed") is False


def test_auto_builds_the_fastembed_engine_when_only_the_four_are_installed(monkeypatch):
    import importlib.util as util

    pytest.importorskip("pydantic")   # the config models need it; the model extras do not
    from contextlake.kb.config import EmbeddingsCfg
    from contextlake.kb.embeddings import base

    monkeypatch.setattr(base, "ollama_reachable", lambda *a, **k: False)
    monkeypatch.setattr(util, "find_spec",
                        lambda name, *a, **k: object() if name in FOUR else None)
    cfg = EmbeddingsCfg(enabled=True, provider="auto", engine="fastembed")
    built = base.build_embedder(cfg)
    assert isinstance(built, BuiltinEmbedder) and built.engine == "fastembed"


def test_auto_builds_nothing_when_the_four_are_missing(monkeypatch):
    import importlib.util as util

    pytest.importorskip("pydantic")   # the config models need it; the model extras do not
    from contextlake.kb.config import EmbeddingsCfg
    from contextlake.kb.embeddings import base

    monkeypatch.setattr(base, "ollama_reachable", lambda *a, **k: False)
    monkeypatch.setattr(util, "find_spec",
                        lambda name, *a, **k: object() if name == "fastembed" else None)
    cfg = EmbeddingsCfg(enabled=True, provider="auto", engine="fastembed")
    assert base.build_embedder(cfg) is None


def test_the_serve_banner_calls_the_fastembed_engine_usable_without_the_library(
        tmp_path, monkeypatch):
    import importlib.util as util

    from contextlake.kb.embeddings import embedder_runtime_state

    monkeypatch.setattr(util, "find_spec",
                        lambda name, *a, **k: object() if name in FOUR else None)
    embedder = BuiltinEmbedder(engine="fastembed", cache_dir=str(tmp_path))
    ready, why = embedder_runtime_state(embedder)
    assert ready is None and "not installed" not in why     # installed, model not fetched yet

    monkeypatch.setattr(util, "find_spec", lambda name, *a, **k: None)
    ready, why = embedder_runtime_state(embedder)
    assert ready is False and "kb-fastembed" in why
