"""`contextlake kb dashboard --serve` refusal gates, swept as a full flag matrix.

Two flags arm privileged dashboard routes -- ``--allow-mutations`` (sync/clone/
spawn) and ``--llm-chat`` (a paid provider call per question) -- and both are
gated by the *same* per-process token, which is inlined into ``/dashboard.js``
and served over a plain GET. So both are refusable on a non-loopback bind for
the same reason, and ``--allow-mutations`` additionally refuses ``--sample``
(the demo fleet is fictional; there is nothing on disk to sync).

Why a matrix and not three cases: ``--llm-chat --host 0.0.0.0`` was a live
vulnerability precisely because it was the one cell nobody had written a test
for. The per-flag tests that existed here all passed while it was open. So the
guard's whole truth table is asserted below -- every combination of
(allow_mutations, llm_chat, sample, loopback host), with the *reason* pinned,
not just the exit code, so a refusal that fires for the wrong cause fails too.

All checks run in ``cmd_dashboard`` before a server is built, so no test here
binds a socket.
"""

from __future__ import annotations

import itertools

import pytest

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


def _stub_serving(tmp_path, monkeypatch) -> dict:
    """Make every *accepted* cell return without touching disk or the network.

    ``serve_dashboard`` and (for ``--sample``) ``materialize_sample_store`` are
    the only two things an accepted cell reaches; both are replaced, so an
    "accepted" assertion below means the guard let the call through, and
    nothing more.
    """
    monkeypatch.setattr("contextlake.kb.cmds.dashboard.load_kb_config",
                        lambda *_a, **_k: type("C", (), {"store_path": tmp_path})())
    monkeypatch.setattr("contextlake.kb.dashboard.site.materialize_sample_store",
                        lambda tmp: tmp)
    called: dict = {}

    def fake_serve(store_dir, **kw):
        called.update(kw)
        return 0

    monkeypatch.setattr("contextlake.kb.dashboard.server.serve_dashboard", fake_serve)
    return called


# --- the truth table ---------------------------------------------------------
#
# Derived from the guard's stated policy rather than transcribed from it:
#   * --allow-mutations + --sample  -> refused ("--sample"), checked first, so a
#     cell that also has a bad host still names --sample;
#   * a non-loopback host + either privileged flag -> refused (that flag named),
#     --allow-mutations reported first when both are set;
#   * everything else -> accepted, including a non-loopback read-only dashboard,
#     which is a supported (if noisy, see host pinning) way to run it.
def _expected(allow_mutations: bool, llm_chat: bool, sample: bool, loopback: bool):
    """Return (exit_code, substring the refusal must name) for one cell."""
    if allow_mutations and sample:
        return 1, "--sample"
    if not loopback:
        if allow_mutations:
            return 1, "--allow-mutations"
        if llm_chat:
            return 1, "--llm-chat"
    return 0, None


_MATRIX = [
    pytest.param(
        am, chat, sample, loopback,
        id=f"mutations={int(am)}-chat={int(chat)}-sample={int(sample)}-loopback={int(loopback)}",
    )
    for am, chat, sample, loopback in itertools.product((False, True), repeat=4)
]


@pytest.mark.parametrize(("allow_mutations", "llm_chat", "sample", "loopback"), _MATRIX)
def test_serve_flag_matrix(allow_mutations, llm_chat, sample, loopback,
                           tmp_path, monkeypatch):
    called = _stub_serving(tmp_path, monkeypatch)
    lines = _captured_log(monkeypatch)

    host = "127.0.0.1" if loopback else "0.0.0.0"
    argv = ["kb", "dashboard", "--serve", "--host", host]
    if allow_mutations:
        argv.append("--allow-mutations")
    if llm_chat:
        argv.append("--llm-chat")
    if sample:
        argv.append("--sample")

    expected_code, expected_reason = _expected(allow_mutations, llm_chat, sample, loopback)
    assert cmd_dashboard(_args(argv)) == expected_code, argv

    if expected_reason is None:
        # Accepted: the flags must have reached the server, not been quietly dropped.
        assert called.get("llm_chat") is llm_chat
        if not sample:
            assert called.get("allow_mutations") is allow_mutations
    else:
        refusals = [line for line in lines if "refused" in line]
        assert len(refusals) == 1, lines
        assert expected_reason in refusals[0]
        assert not called, "a refused cell must never reach serve_dashboard"


def test_llm_chat_refusal_explains_the_token_exposure(monkeypatch):
    """The refusal has to say *why* a loopback bind is required, or the obvious
    reaction is to assume it's over-caution and go looking for an override."""
    lines = _captured_log(monkeypatch)
    assert cmd_dashboard(
        _args(["kb", "dashboard", "--serve", "--llm-chat", "--host", "0.0.0.0"])) == 1
    (refusal,) = [line for line in lines if "refused" in line]
    assert "loopback" in refusal
    assert "/dashboard.js" in refusal   # names where the token leaks from
    assert "--host 127.0.0.1" in refusal  # names the fix


def test_localhost_counts_as_loopback(tmp_path, monkeypatch):
    """`--host localhost` is the same bind as 127.0.0.1 for these servers, so the
    guard must not refuse it -- the matrix above only exercises the dotted form."""
    called = _stub_serving(tmp_path, monkeypatch)
    _captured_log(monkeypatch)
    argv = ["kb", "dashboard", "--serve", "--allow-mutations", "--llm-chat",
            "--host", "localhost"]
    assert cmd_dashboard(_args(argv)) == 0
    assert called["allow_mutations"] is True and called["llm_chat"] is True
