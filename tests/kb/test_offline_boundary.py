"""INV-2 enforcement: the core knowledge-layer commands must run fully OFFLINE.

Code parse -> graph -> FTS -> query -> lint -> visualize never touch the network;
enrichment (`connect`, which reaches Atlassian/Figma/GitLab MCPs) is the deliberate,
opt-in ONLINE exception and must *degrade, not fail* when the network is absent. This
blocks all outbound network at the socket layer and asserts the offline commands still
succeed — so a regression that sneaks a network call into the offline path is caught.
"""

import importlib.util
import socket
from pathlib import Path

import pytest

from contextlake.cli import main
from contextlake.kb import embeddings
from contextlake.kb.embeddings.store import build_vector_store

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "fixtures" / "sample-graph.json"


@pytest.fixture
def no_network(monkeypatch):
    """Make any outbound DNS/connect raise — local file/SQLite work is untouched."""
    def _blocked(*args, **kwargs):
        raise OSError("INV-2 violation: an offline command attempted network access")

    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def _run(argv) -> int:
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def _cfg(tmp_path) -> Path:
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    return cfg


def test_core_commands_run_with_network_blocked(tmp_path, no_network):
    cfg = _cfg(tmp_path)
    # index -> query -> visualize -> lint, all with every outbound connection blocked
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    assert _run(["kb", "query", "ForecastService", "--config", str(cfg)]) == 0
    assert _run(["kb", "graph", "--config", str(cfg), "--overview"]) == 0
    # lint runs offline too; it exits 1 here only because a JSON-fixture repo has no
    # matching git HEAD (a normal "stale" health finding), never a network error.
    assert _run(["kb", "lint", "--config", str(cfg)]) in (0, 1)


def test_embed_offline_with_no_embedder_is_a_graceful_noop(tmp_path, no_network,
                                                          monkeypatch):
    # With no embedder the command degrades to a clean no-op (exit 0) rather than
    # reaching out. Pinned to "no engine installed", CI's state: left to the machine,
    # a machine with model2vec took the builtin branch instead, which needs the model
    # and passed only if an earlier test in the same process had downloaded it.
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: (
        None if name in ("model2vec", "fastembed") else real_find_spec(name, *a, **k)))
    cfg = _cfg(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    assert _run(["kb", "embed", "--config", str(cfg)]) == 0


def test_embed_with_a_ready_embedder_runs_offline(tmp_path, no_network, monkeypatch):
    # docs/internals.md: once the model is cached, embedding is offline too. A fake
    # stands in for the cached model, so this proves `kb embed` adds no network call
    # of its own around the embedder.
    class _Ready:
        name = "fake"

        def embed(self, texts):
            return [[1.0, float(i)] for i, _ in enumerate(texts)]

    monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: _Ready())
    cfg = _cfg(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    assert _run(["kb", "embed", "--config", str(cfg)]) == 0
    vs = build_vector_store(tmp_path / "kb" / "embeddings.sqlite")
    try:
        assert vs.count_repo("demo/app") > 0, "the embedder ran, so vectors must exist"
    finally:
        vs.close()


def test_connect_degrades_not_fails_offline(tmp_path, no_network):
    # `connect` is the online exception, but with no connector configured + no network
    # it must degrade (skip/warn, exit 0), never crash — so running fully offline is safe.
    cfg = _cfg(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    assert _run(["kb", "connect", "--config", str(cfg)]) == 0


def test_dashboard_site_builds_offline(tmp_path, no_network):
    # The static dashboard --site export (sample showcase) must build with all outbound
    # connections blocked — it reads only the committed fixture + local templates.
    cfg = _cfg(tmp_path)
    out = tmp_path / "dash"
    assert _run(["kb", "dashboard", "--config", str(cfg), "--site", str(out), "--sample"]) == 0
    assert (out / "index.html").exists() and (out / "data.json").exists()
