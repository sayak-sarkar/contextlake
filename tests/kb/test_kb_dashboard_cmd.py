"""`contextlake dashboard --allow-mutations` refusal gates: --sample (no real
fleet to mutate) and a non-loopback --host (mutating routes would be reachable
over the network). Both are checked in cmd_dashboard before a server is ever
built, so these tests never bind a socket."""

from contextlake.cli import build_parser
from contextlake.kb.cmds.dashboard import cmd_dashboard


def _args(argv):
    return build_parser().parse_args(argv)


def _captured_log(monkeypatch):
    """log() goes through the package logger, whose stdout-vs-propagate wiring
    varies with what ran earlier in the session (observed: capsys and caplog
    each miss it depending on suite order) -- patch the name cmds.dashboard
    actually calls instead of depending on either capture mechanism."""
    lines = []
    monkeypatch.setattr("contextlake.kb.cmds.dashboard.log", lines.append)
    return lines


def test_allow_mutations_refused_with_sample(monkeypatch):
    lines = _captured_log(monkeypatch)
    args = _args(["kb", "dashboard", "--serve", "--sample", "--allow-mutations"])
    assert cmd_dashboard(args) == 1
    assert any("--sample" in line for line in lines)


def test_allow_mutations_refused_with_non_loopback_host(monkeypatch):
    lines = _captured_log(monkeypatch)
    args = _args(["kb", "dashboard", "--serve", "--allow-mutations", "--host", "0.0.0.0"])
    assert cmd_dashboard(args) == 1
    assert any("loopback" in line for line in lines)


def test_allow_mutations_accepted_with_loopback_host(tmp_path, monkeypatch):
    # Only prove the guard clears (doesn't refuse) -- don't actually bind/serve.
    monkeypatch.setattr("contextlake.kb.cmds.dashboard.load_kb_config",
                        lambda *_a, **_k: type("C", (), {"store_path": tmp_path})())
    called = {}

    def fake_serve(store_dir, **kw):
        called.update(kw)
        return 0

    monkeypatch.setattr("contextlake.kb.dashboard.server.serve_dashboard", fake_serve)
    args = _args(["kb", "dashboard", "--serve", "--allow-mutations", "--host", "127.0.0.1"])
    assert cmd_dashboard(args) == 0
    assert called["allow_mutations"] is True
