"""INV-2 enforcement: the core knowledge-layer commands must run fully OFFLINE.

Code parse -> graph -> FTS -> query -> lint -> visualize never touch the network;
enrichment (`connect`, which reaches Atlassian/Figma/GitLab MCPs) is the deliberate,
opt-in ONLINE exception and must *degrade, not fail* when the network is absent. This
blocks all outbound network at the socket layer and asserts the offline commands still
succeed — so a regression that sneaks a network call into the offline path is caught.
"""

import socket
from pathlib import Path

import pytest

from contextlake.cli import main

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
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    assert _run(["query", "OrderService", "--config", str(cfg)]) == 0
    assert _run(["graph", "--config", str(cfg), "--overview"]) == 0
    # lint runs offline too; it exits 1 here only because a JSON-fixture repo has no
    # matching git HEAD (a normal "stale" health finding), never a network error.
    assert _run(["lint", "--config", str(cfg)]) in (0, 1)


def test_embed_offline_is_a_graceful_noop(tmp_path, no_network):
    # embeddings are opt-in/off by default; with no embedder the command degrades to a
    # clean no-op (exit 0) rather than reaching out — never a hard failure offline.
    cfg = _cfg(tmp_path)
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    assert _run(["embed", "--config", str(cfg)]) == 0


def test_connect_degrades_not_fails_offline(tmp_path, no_network):
    # `connect` is the online exception, but with no connector configured + no network
    # it must degrade (skip/warn, exit 0), never crash — so running fully offline is safe.
    cfg = _cfg(tmp_path)
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    assert _run(["connect", "--config", str(cfg)]) == 0


def test_dashboard_site_builds_offline(tmp_path, no_network):
    # The static dashboard --site export (sample showcase) must build with all outbound
    # connections blocked — it reads only the committed fixture + local templates.
    cfg = _cfg(tmp_path)
    out = tmp_path / "dash"
    assert _run(["dashboard", "--config", str(cfg), "--site", str(out), "--sample"]) == 0
    assert (out / "index.html").exists() and (out / "data.json").exists()
