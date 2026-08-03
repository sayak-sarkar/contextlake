"""Combinatorial coverage for the LLM/embedding/vector-store provider matrix
(RC-P1-7 / T-2, T-6, T-7).

Individual providers are already unit-tested in isolation (test_kb_embeddings.py,
test_kb_llm.py); this file instead sweeps the *combinations* -- provider x engine
x enabled, and provider x provider x backend together -- because a real
vulnerability in this codebase once lived precisely in an untested cell of a
flag matrix (see CHANGELOG's ``--llm-chat`` / non-loopback-host fix). The
provider name lists below are extracted from the actual
``if provider == "..."`` dispatch branches in kb/embeddings/base.py and
kb/llm/base.py (see ``_discover_providers``), not hand-maintained, so a
provider added there later is automatically picked up here without editing
this file.

Offline and hermetic by construction:
  - every "auto" cell monkeypatches ``ollama_reachable`` to False so no real
    network probe is attempted (same pattern as test_kb_embeddings.py /
    test_kb_llm.py's own "auto" tests);
  - no cell ever calls ``.embed()``/``.generate()`` -- only construction, which
    every provider class documents as network/subprocess-free (BuiltinEmbedder,
    BuiltinLlm, CliLlm all defer their real work to first use);
  - provider="cli" never shells out from ``build_llm()`` -- only from
    ``CliLlm.generate()``, which this file never calls -- so no subprocess is
    spawned by this matrix either.

Design note on cell count: fully crossing every axis in one
``itertools.product`` (embed_provider x llm_provider x vector_backend x engine x
embeddings.enabled x llm.enabled) would be 4*6*3*2*2*2 = 1152 cells asserting
the same handful of code paths over and over. Instead this file uses several
smaller, targeted products: enabled/disabled and engine are swept per-function
(where they actually matter), and a separate, still-programmatic
provider x provider x backend product covers cross-tier interaction with
enabled=True fixed (the "all three tiers configured together" shape a real
``kb.toml`` produces) -- full coverage of every axis, at ~150 total cells
instead of over a thousand.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

import contextlake.kb.embeddings.base as embed_base_mod
import contextlake.kb.embeddings.store as vector_store_mod
import contextlake.kb.llm.base as llm_base_mod
from contextlake.kb.config import EmbeddingsCfg, LlmCfg
from contextlake.kb.embeddings import build_embedder
from contextlake.kb.embeddings.base import Embedder
from contextlake.kb.embeddings.store import SqliteVecStore, VectorStore, build_vector_store
from contextlake.kb.llm import build_llm
from contextlake.kb.llm.base import LlmClient

_KB_SRC = Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb"


def _discover_providers(relpath: str) -> list[str]:
    """Provider names literally compared in ``if provider == "...":`` branches
    of the given source file under ``kb/`` -- read fresh every run so a newly
    added provider branch extends this matrix automatically, with no edit here.
    """
    text = (_KB_SRC / relpath).read_text(encoding="utf-8")
    names = sorted(set(re.findall(r'provider == "([a-z_]+)"', text)))
    assert names, (
        f"no `provider == \"...\"` branches found in {relpath} -- either the "
        "dispatch style changed (update the discovery regex) or the factory "
        "lost its branches entirely"
    )
    return names


EMBED_PROVIDERS = _discover_providers("embeddings/base.py")
LLM_PROVIDERS = _discover_providers("llm/base.py")

# build_vector_store's branches are `if backend in (...)` rather than a plain
# `==`, so (unlike the two lists above) this one is spelled out by hand from
# kb/embeddings/store.py's build_vector_store docstring: "auto | sqlite-vec | brute".
VECTOR_BACKENDS = ["auto", "sqlite-vec", "brute"]

# Only meaningful for the embeddings providers that read it (builtin, and
# auto's builtin fallback) -- EmbeddingsCfg allows extra keys, so passing it to
# ollama/openai cells is a harmless no-op, not an error.
EMBED_ENGINES = ["model2vec", "fastembed"]

BOOL = [True, False]


def test_discovery_found_the_known_branches():
    """Guards the discovery regex itself: if a refactor silently changed the
    branch style, `_discover_providers` would return an empty (or truncated)
    list and every matrix below would quietly shrink instead of failing loudly.
    """
    assert {"auto", "ollama", "openai", "builtin"} <= set(EMBED_PROVIDERS)
    assert {"auto", "ollama", "openai", "anthropic", "cli", "builtin"} <= set(LLM_PROVIDERS)


@pytest.fixture(autouse=True)
def _offline_ollama(monkeypatch):
    """Every "auto" cell in this file must resolve without a real network
    probe -- see module docstring."""
    monkeypatch.setattr(embed_base_mod, "ollama_reachable", lambda *a, **k: False)
    monkeypatch.setattr(llm_base_mod, "ollama_reachable", lambda *a, **k: False)


# --- embeddings: provider x engine x enabled --------------------------------

@pytest.mark.parametrize(
    "provider,engine,enabled",
    list(itertools.product(EMBED_PROVIDERS, EMBED_ENGINES, BOOL)),
)
def test_build_embedder_matrix_never_raises(provider, engine, enabled):
    cfg = EmbeddingsCfg(enabled=enabled, provider=provider, engine=engine)
    result = build_embedder(cfg)
    if not enabled:
        assert result is None
    else:
        assert result is None or isinstance(result, Embedder)


# --- llm: provider x enabled -------------------------------------------------

@pytest.mark.parametrize(
    "provider,enabled", list(itertools.product(LLM_PROVIDERS, BOOL)),
)
def test_build_llm_matrix_never_raises(provider, enabled):
    cfg = LlmCfg(enabled=enabled, provider=provider)
    result = build_llm(cfg)
    if not enabled:
        assert result is None
    else:
        assert result is None or isinstance(result, LlmClient)


# --- the "auto" resolver's own sub-matrix, both ways, still offline ---------
#
# build_embedder/build_llm's "auto" branch has its own 2x2 (ollama reachable x
# has the model) that the matrices above don't distinguish (they only ever see
# reachable=False). Covered here explicitly, matching the existing per-provider
# tests' own pattern (test_kb_embeddings.py/test_kb_llm.py).

@pytest.mark.parametrize("reachable,has_model", list(itertools.product(BOOL, BOOL)))
def test_build_embedder_auto_branch_matrix(monkeypatch, reachable, has_model):
    monkeypatch.setattr(embed_base_mod, "ollama_reachable", lambda *a, **k: reachable)
    monkeypatch.setattr(embed_base_mod, "ollama_has_model", lambda *a, **k: has_model)
    result = build_embedder(EmbeddingsCfg(enabled=True, provider="auto"))
    assert result is None or isinstance(result, Embedder)


@pytest.mark.parametrize("reachable,has_model", list(itertools.product(BOOL, BOOL)))
def test_build_llm_auto_branch_matrix(monkeypatch, reachable, has_model):
    monkeypatch.setattr(llm_base_mod, "ollama_reachable", lambda *a, **k: reachable)
    monkeypatch.setattr(llm_base_mod, "ollama_has_model", lambda *a, **k: has_model)
    result = build_llm(LlmCfg(enabled=True, provider="auto"))
    assert result is None or isinstance(result, LlmClient)


# --- embeddings.enabled x llm.enabled, crossed together ---------------------
#
# The two matrices above cover enabled/disabled per-function but never in the
# same cell -- spelled out explicitly here on one representative provider pair
# (builtin/builtin, both real no-network constructions) so the brief's named
# axis is actually crossed at least once, not just covered independently.
# Functionally this can't reveal anything the per-function tests don't already
# (the two factories share no state), but it makes the deliverable match the
# spec on its face.

@pytest.mark.parametrize("embeddings_enabled,llm_enabled", list(itertools.product(BOOL, BOOL)))
def test_embeddings_enabled_x_llm_enabled_crossed(embeddings_enabled, llm_enabled):
    embedder = build_embedder(EmbeddingsCfg(enabled=embeddings_enabled, provider="builtin"))
    llm = build_llm(LlmCfg(enabled=llm_enabled, provider="builtin"))
    assert (embedder is not None) == embeddings_enabled
    assert (llm is not None) == llm_enabled


# --- vector store builder: backend x chunk_size -----------------------------

CHUNK_SIZES = [8, 1024, 100_000]  # the vec0 minimum clamp, the default, a large value


@pytest.mark.parametrize(
    "backend,chunk_size", list(itertools.product(VECTOR_BACKENDS, CHUNK_SIZES)),
)
def test_build_vector_store_matrix_never_raises(tmp_path, backend, chunk_size):
    if backend in ("sqlite-vec", "auto"):
        pytest.importorskip("sqlite_vec")  # optional 'kb-vec' extra not installed
    store = build_vector_store(
        tmp_path / f"{backend}-{chunk_size}.sqlite", backend=backend, chunk_size=chunk_size,
    )
    try:
        assert isinstance(store, (VectorStore, SqliteVecStore))
        assert store.count() == 0  # a fresh store must be queryable, not just constructible
    finally:
        store.close()


def test_build_vector_store_sqlite_vec_forced_raises_when_unavailable(monkeypatch, tmp_path):
    """backend="sqlite-vec" (forced) is the one deliberately-raising cell in
    this factory -- unlike "auto", which degrades to the brute store on the
    same failure. Simulated here (rather than relying on the extra actually
    being missing, which is environment-dependent) so this regression is
    caught even in a venv that happens to have sqlite-vec installed."""

    def _boom(*a, **k):
        raise ImportError("simulated: sqlite_vec not installed")

    monkeypatch.setattr(vector_store_mod, "SqliteVecStore", _boom)
    with pytest.raises(ImportError):
        build_vector_store(tmp_path / "forced.sqlite", backend="sqlite-vec")


def test_build_vector_store_auto_degrades_to_brute_when_sqlite_vec_unavailable(
    monkeypatch, tmp_path, gls_logs,
):
    """The inverse of the previous test: "auto" hitting the exact same failure
    must NOT raise -- it degrades to the pure-Python store and says so (an
    operator otherwise has no idea search silently dropped to O(n))."""

    def _boom(*a, **k):
        raise ImportError("simulated: sqlite_vec not installed")

    monkeypatch.setattr(vector_store_mod, "SqliteVecStore", _boom)
    store = build_vector_store(tmp_path / "auto.sqlite", backend="auto")
    try:
        assert isinstance(store, VectorStore)
        assert "sqlite-vec unavailable" in gls_logs.text
    finally:
        store.close()


# --- cross-tier interaction: embed_provider x llm_provider x vector_backend -
#
# The shape a real kb.toml actually produces: embeddings, llm, and the vector
# store all configured together. enabled=True is fixed here (the
# enabled/disabled axis is already fully covered per-function above) so this
# stays a clean provider x provider x backend product, not a repeat of it.

_STACK_CASES = list(itertools.product(EMBED_PROVIDERS, LLM_PROVIDERS, VECTOR_BACKENDS))


@pytest.mark.parametrize("embed_provider,llm_provider,vector_backend", _STACK_CASES)
def test_full_stack_resolution_matrix_never_raises(
    tmp_path, embed_provider, llm_provider, vector_backend,
):
    if vector_backend in ("sqlite-vec", "auto"):
        pytest.importorskip("sqlite_vec")  # optional 'kb-vec' extra not installed

    embedder = build_embedder(EmbeddingsCfg(enabled=True, provider=embed_provider))
    assert embedder is None or isinstance(embedder, Embedder)

    llm = build_llm(LlmCfg(enabled=True, provider=llm_provider))
    assert llm is None or isinstance(llm, LlmClient)

    store = build_vector_store(tmp_path / "stack.sqlite", backend=vector_backend)
    try:
        assert isinstance(store, (VectorStore, SqliteVecStore))
    finally:
        store.close()
