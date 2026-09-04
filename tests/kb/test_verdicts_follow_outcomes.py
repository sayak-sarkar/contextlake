"""Three commands printed a fault and then a success word about the same run.

`kb enrich` returned 0 unconditionally while the counter that would have told it every
source call had been written off sat unread, in a module its sibling `kb connect` reads a
few files away. `doctor` folded exactly three of its twenty-odd checks into the verdict, so
a red ✗ for a repository indexed by an older parser printed above a green "OK" and exit 0.
`kb forget` warned that paths were still on disk and then ticked the operation as done.

All three are the same shape, and it is the shape this codebase keeps producing: the
summary is written from a variable that the reporting does not feed.
"""

from __future__ import annotations

import pytest

# --- doctor -----------------------------------------------------------------------

def test_a_printed_fault_counts_towards_the_verdict():
    from contextlake.kb.cmds import doctor

    doctor._start_report()
    doctor._check("something broken", False, "detail")
    assert doctor._FAULTS == ["something broken"], (
        "a check that prints ✗ must count, without a caller remembering to add it")


def test_an_advisory_does_not_count():
    """The tri-state has to keep meaning what it says, or every ⚠ becomes a failure."""
    from contextlake.kb.cmds import doctor

    doctor._start_report()
    doctor._check("optional thing absent", None, "advisory")
    doctor._check("fine", True)
    assert doctor._FAULTS == []


def test_an_untested_check_is_neither_a_fault_nor_a_pass(capsys):
    """The fourth state, pinned from both sides.

    `bool(UNTESTED)` is True, so `_check`'s old `return bool(ok)` would report a
    check that never ran as a pass. Every call site is bare, so nothing else in
    the codebase would catch that; this assertion is the only thing pinning
    `return ok is True`.
    """
    from contextlake.kb.cmds import doctor

    doctor._start_report()
    assert doctor._check("nothing to dial", doctor.UNTESTED, "no probe for this type") is False, (
        "a check that never ran must not report itself to a caller as a pass")
    assert doctor._FAULTS == [], "and it is not a fault either"

    # The other three states still behave, in the same test: a branch that
    # returned False and recorded nothing for EVERY input would satisfy the two
    # assertions above on its own.
    assert doctor._check("a real passing check", True) is True
    assert doctor._FAULTS == []
    doctor._check("a real fault", False)
    assert doctor._FAULTS == ["a real fault"]

    printed = capsys.readouterr().out
    assert printed.strip(), "the capture must see something or this proves nothing"
    untested_line = [ln for ln in printed.splitlines() if "nothing to dial" in ln][0]
    assert "⊘" in untested_line
    assert "⚠" not in untested_line, "not-tested and probed-and-degraded are different facts"


def test_the_report_resets_between_runs():
    """A fault left over from a previous run would fail the next one for nothing."""
    from contextlake.kb.cmds import doctor

    doctor._start_report()
    doctor._check("broken", False)
    doctor._start_report()
    doctor._check("fine", True)
    assert doctor._FAULTS == []


def test_a_stale_shard_is_advisory_and_does_not_fail_the_verdict(tmp_path, monkeypatch,
                                                                 capsys):
    """The resolution of the contradiction, pinned from both sides.

    A parser bump makes every existing shard stale, so failing on it would redden every
    user's CI on upgrade -- a decision recorded in `kb lint` and in this command's own
    tests. What was wrong was drawing a red ✗ for it and printing "OK" underneath. The mark
    is now ⚠, so the screen and the exit code agree, and neither side of the earlier
    disagreement had to be given up.
    """
    from contextlake.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    repo = tmp_path / "svc-billing"
    repo.mkdir()
    (repo / "billing.py").write_text("def charge_card(amount):\n    return amount\n")
    with pytest.raises(SystemExit) as e:
        main(["kb", "index", str(repo), "--config", str(cfg)])
    assert e.value.code == 0
    capsys.readouterr()

    # Patched on `kb.parse`, which is where `doctor` imports the constant from INSIDE the
    # function. A first version patched the doctor module, changed nothing, printed no
    # mark at all, and its assertion -- written as a conditional -- silently never ran.
    from contextlake.kb import parse as parse_mod

    monkeypatch.setattr(parse_mod, "PARSER_VERSION", "999999")
    with pytest.raises(SystemExit) as e:
        main(["doctor", "--config", str(cfg)])
    printed = "".join(capsys.readouterr())
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert "older parser" in printed, (
        "the fixture is meant to PRODUCE the stale-shard line; without it this asserts "
        "nothing about the mark that line carries")
    assert "⚠" in printed.split("older parser")[0].splitlines()[-1], (
        "the stale-shard line must carry the advisory mark, not a fault mark")
    assert e.value.code == 0, "an advisory must not fail the verdict"
    assert "Problems found" not in printed


def test_an_optional_tool_being_absent_does_not_fail_the_verdict(tmp_path, monkeypatch,
                                                                 capsys):
    """`glab` is only needed for the mirror against a forge, and CI installs none.

    This is the regression gate for the hand-maintained part of the change: converting
    `_check` to record its own faults means every remaining plain-bool call site had to be
    re-read, and two advisory ones were missed on the first pass. A stock install without
    the optional tooling must still exit 0.
    """
    import shutil as shutil_mod

    from contextlake.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    real_which = shutil_mod.which
    monkeypatch.setattr(shutil_mod, "which",
                        lambda name: None if name == "glab" else real_which(name))
    with pytest.raises(SystemExit) as e:
        main(["doctor", "--config", str(cfg)])
    printed = "".join(capsys.readouterr())
    assert "glab" in printed, "the fixture must actually reach the glab check"
    assert e.value.code == 0, "an absent optional tool is not a fault"


# --- forget -----------------------------------------------------------------------

def test_forget_reports_partial_when_a_path_survives(tmp_path, monkeypatch, capsys):
    from contextlake.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "kb.toml"
    store_dir = tmp_path / "kb"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    repo = tmp_path / "svc-billing"
    repo.mkdir()
    (repo / "billing.py").write_text("def charge_card(amount):\n    return amount\n")
    with pytest.raises(SystemExit) as e:
        main(["kb", "index", str(repo), "--config", str(cfg)])
    assert e.value.code == 0
    capsys.readouterr()

    # `rmtree(ignore_errors=True)` failing silently is exactly the condition the old
    # summary papered over. `forget` imports shutil inside the function, so the patch goes
    # on the shutil module itself rather than on a name the module does not hold.
    import shutil
    from pathlib import Path

    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    # Files as well as directories: which shape the store's artefacts take is an
    # implementation detail, and patching only one made the test pass for the wrong
    # reason -- nothing survived, so the survivor branch was never entered.
    monkeypatch.setattr(Path, "unlink", lambda self, **k: None)

    # A wiki page to remove, so the count in the summary has something to be wrong about.
    wiki = store_dir / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "svc-billing.md").write_text("# svc-billing\n")

    with pytest.raises(SystemExit) as e:
        main(["kb", "forget", "svc-billing", "--config", str(cfg)])
    printed = "".join(capsys.readouterr())
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert e.value.code == 1, "paths still on disk means the operation did not finish"
    assert "Partly forgot" in printed
    assert "✓ Forgot" not in printed, "a tick over a warning is the contradiction being fixed"
    assert "0 wiki page(s) removed" in printed, (
        "the page did not go, so counting it as removed is the same claim-not-measurement "
        "the byte figure was already corrected for")


def test_forget_still_reports_success_when_everything_goes(tmp_path, monkeypatch, capsys):
    """The guard must catch the survivor case, not turn every forget into a failure."""
    from contextlake.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    repo = tmp_path / "svc-billing"
    repo.mkdir()
    (repo / "billing.py").write_text("def charge_card(amount):\n    return amount\n")
    with pytest.raises(SystemExit) as e:
        main(["kb", "index", str(repo), "--config", str(cfg)])
    assert e.value.code == 0
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        main(["kb", "forget", "svc-billing", "--config", str(cfg)])
    printed = "".join(capsys.readouterr())
    assert e.value.code == 0
    assert "✓ Forgot" in printed
