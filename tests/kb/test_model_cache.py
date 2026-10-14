"""A model on disk is never downloaded again (owner decision 5).

The cache here is a real Hugging Face cache layout in a temp folder, read by the real
`snapshot_download(local_files_only=True)`. Only the ONLINE call is replaced, by a
recorder that never touches the network, so every test counts the online calls.
"""

from __future__ import annotations

import sys
import types

import huggingface_hub
import pytest
from huggingface_hub import constants

from contextlake import netguard
from contextlake.kb import model_cache

REPO = "acme/tiny-model"
PIN = "a" * 40
OTHER = "b" * 40


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A temp cache and a recorder of online calls."""
    cache = tmp_path / "hub"
    cache.mkdir()
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    real = huggingface_hub.snapshot_download
    online = []

    def recorder(*args, **kw):
        if kw.get("local_files_only"):
            return real(*args, cache_dir=str(cache), **kw)
        online.append(kw)
        folder = cache / "downloaded"
        folder.mkdir(exist_ok=True)
        return str(folder)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", recorder)
    return types.SimpleNamespace(cache=cache, online=online)


def _cached(hub, commit, *, ref=True):
    repo_dir = hub.cache / f"models--{REPO.replace('/', '--')}"
    snap = repo_dir / "snapshots" / commit
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    if ref:
        (repo_dir / "refs").mkdir(exist_ok=True)
        (repo_dir / "refs" / "main").write_text(commit)
    return str(snap)


def test_the_pinned_commit_on_disk_is_used_with_no_network(hub):
    folder = _cached(hub, PIN)
    assert model_cache.fetch_snapshot(REPO, PIN) == folder
    assert hub.online == []


def test_a_different_commit_on_disk_is_used_with_no_network(hub):
    """An older install or a baked image holds another commit than the pin."""
    folder = _cached(hub, OTHER)
    assert model_cache.fetch_snapshot(REPO, PIN) == folder
    assert hub.online == []


def test_a_snapshot_no_ref_names_is_still_found(hub):
    folder = _cached(hub, OTHER, ref=False)
    assert model_cache.fetch_snapshot(REPO, PIN) == folder
    assert hub.online == []


def test_an_empty_cache_downloads_the_pinned_commit_once(hub):
    model_cache.fetch_snapshot(REPO, PIN)
    assert [c["revision"] for c in hub.online] == [PIN]


def test_offline_with_an_empty_cache_names_the_model_and_sends_nothing(hub, monkeypatch):
    monkeypatch.setenv(netguard.OFFLINE_ENV, "1")
    with pytest.raises(RuntimeError, match=REPO):
        model_cache.fetch_snapshot(REPO, PIN)
    assert hub.online == []


def test_complete_asks_the_network_even_with_a_copy_on_disk(hub):
    _cached(hub, PIN)
    model_cache.fetch_snapshot(REPO, PIN, complete=True)
    assert len(hub.online) == 1


# --- the loaders ---------------------------------------------------------------------

def test_the_builtin_llm_loads_the_copy_on_disk(hub, monkeypatch):
    from contextlake.kb.llm import builtin

    opened = []
    fake = types.ModuleType("openvino_genai")
    fake.LLMPipeline = lambda folder, device: opened.append(folder) or object()
    monkeypatch.setitem(sys.modules, "openvino_genai", fake)
    folder = _cached(hub, PIN)
    llm = builtin.BuiltinLlm(repo_id=REPO)
    llm._ensure_model()
    assert opened == [folder]
    assert hub.online == []


def test_a_partial_copy_is_completed_once_then_loaded(hub, monkeypatch):
    from contextlake.kb.llm import builtin

    opened = []

    def pipeline(folder, device):
        opened.append(folder)
        if len(opened) == 1:
            raise RuntimeError("openvino_model.bin is missing")
        return object()

    fake = types.ModuleType("openvino_genai")
    fake.LLMPipeline = pipeline
    monkeypatch.setitem(sys.modules, "openvino_genai", fake)
    _cached(hub, PIN)
    builtin.BuiltinLlm(repo_id=REPO)._ensure_model()
    assert len(hub.online) == 1 and len(opened) == 2


def test_model2vec_is_given_a_folder_not_a_repo_id(hub, monkeypatch):
    """Given a repo id, model2vec's `from_pretrained` asks the Hub on every load."""
    from contextlake.kb.embeddings.builtin import BuiltinEmbedder

    loaded = []
    static = types.SimpleNamespace(from_pretrained=lambda path: loaded.append(path) or object())
    folder = _cached(hub, OTHER)
    BuiltinEmbedder(engine="model2vec", model=REPO)._load_model2vec(static)
    assert loaded == [folder]
    assert hub.online == []


class _FastEmbed:
    def __init__(self, behaviour):
        self.calls = []
        self.behaviour = behaviour

    def __call__(self, **kw):
        self.calls.append(kw)
        if kw.get("local_files_only") and self.behaviour:
            raise self.behaviour
        return object()


@pytest.mark.parametrize("behaviour,expected", [
    (None, [True]),                                  # on disk: one local load
    (ValueError("not in cache"), [True, None]),      # not on disk: then the network
    (TypeError("unexpected keyword"), [True, None]),  # a fastembed without the flag
])
def test_fastembed_tries_its_own_cache_first(behaviour, expected, monkeypatch):
    from contextlake.kb.embeddings.builtin import BuiltinEmbedder

    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    fake = _FastEmbed(behaviour)
    BuiltinEmbedder(engine="fastembed")._load_fastembed(fake)
    assert [c.get("local_files_only") for c in fake.calls] == expected


def test_fastembed_offline_does_not_go_online(monkeypatch):
    from contextlake.kb.embeddings.builtin import BuiltinEmbedder

    monkeypatch.setenv(netguard.OFFLINE_ENV, "1")
    fake = _FastEmbed(ValueError("not in cache"))
    with pytest.raises(ValueError):
        BuiltinEmbedder(engine="fastembed")._load_fastembed(fake)
    assert len(fake.calls) == 1
