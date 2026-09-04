"""`kb enrich` returned 0 whatever happened, in a module whose sibling reads the counter.

Source methods here are contractually non-raising: an unreachable source yields nothing so
one dead source cannot break the run. That makes a try/except blind to exactly the failure
that matters, which is why `kb connect` takes its verdict from `resilience.degraded_calls()`
instead. `kb enrich` sat beside it and never read that counter, so a run where every source
call was written off printed the same green line as a healthy run over repos with nothing
to find.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from contextlake.kb.connectors.enrich import EnrichCounts


def _args(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        '\n[embeddings]\nenabled = false\n'
        '\n[[sources]]\nname = "deadsrc"\ntype = "mcp"\ntool = "search"\n'
        'url = "http://127.0.0.1:1/mcp"\n')
    return SimpleNamespace(config=str(cfg), args=[], workspace=None, repos=None,
                           source=None, json=False)


def _index(tmp_path, monkeypatch):
    from contextlake.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "svc-billing"
    repo.mkdir()
    (repo / "billing.py").write_text("def charge_card(amount):\n    return amount\n")
    args = _args(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["kb", "index", str(repo), "--config", args.config])
    assert e.value.code == 0
    return args


def test_every_source_call_written_off_is_a_failure(tmp_path, monkeypatch, capsys):
    from contextlake.kb.cmds.enrich import cmd_enrich

    args = _index(tmp_path, monkeypatch)
    capsys.readouterr()

    # Drive the counter directly rather than waiting on a real socket: the command's
    # contract is "read the counter and decide", and that is what is under test.
    import contextlake.kb.connectors.enrich as enrich_mod
    from contextlake.kb import resilience

    def _fail(*a, **k):
        resilience.note_unavailable("deadsrc search", RuntimeError("unreachable"))
        return EnrichCounts(3, 0, 0)

    monkeypatch.setattr(enrich_mod, "run_enrich_repo", _fail)
    code = cmd_enrich(args)
    printed = "".join(capsys.readouterr())
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert code == 1, "no source reachable and nothing stored is a failed run, not an empty one"
    assert "unavailable" in printed
    # The accounting has to survive the early exit. A bucket line printed only on the
    # happy path leaves the reader guessing on the one run where the numbers matter.
    assert ("1 repo(s) planned: 0 enriched, 1 nothing returned, 0 returned but "
            "unattached, 0 failed, 0 skipped") in printed


def test_a_clean_run_with_nothing_to_find_still_succeeds(tmp_path, monkeypatch, capsys):
    """The verdict must follow the COUNTER, not the document total: a healthy run over
    repos with nothing to find legitimately stores zero."""
    from contextlake.kb.cmds.enrich import cmd_enrich

    args = _index(tmp_path, monkeypatch)
    capsys.readouterr()
    import contextlake.kb.connectors.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "run_enrich_repo",
                        lambda *a, **k: EnrichCounts(3, 0, 0))
    code = cmd_enrich(args)
    printed = "".join(capsys.readouterr())
    assert code == 0
    assert "Enrich complete" in printed


def test_partial_degradation_with_results_matches_connects_rule(tmp_path, monkeypatch, capsys):
    """`kb connect` exits 0 when some calls degraded but something was still stored. The
    rule is copied rather than tightened: two sibling commands disagreeing on one event is
    the defect this batch is about."""
    from contextlake.kb.cmds.enrich import cmd_enrich

    args = _index(tmp_path, monkeypatch)
    capsys.readouterr()
    import contextlake.kb.connectors.enrich as enrich_mod
    from contextlake.kb import resilience

    def _partial(*a, **k):
        resilience.note_unavailable("deadsrc search", RuntimeError("unreachable"))
        return EnrichCounts(3, 3, 1)

    monkeypatch.setattr(enrich_mod, "run_enrich_repo", _partial)
    code = cmd_enrich(args)
    printed = "".join(capsys.readouterr())
    assert code == 0
    assert "incomplete" in printed, "the degradation still has to be stated"
