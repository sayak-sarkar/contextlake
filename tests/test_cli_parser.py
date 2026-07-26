"""Regression tests for CLI UX fixes: no silent flag abbreviation, discoverable
help epilog. See docs/... review notes for the source of these findings.
"""

import pytest

from contextlake.cli import build_parser


def test_abbreviated_long_flags_are_rejected():
    """Before the fix, argparse's default allow_abbrev=True silently resolved
    --work-d to --work-dir -- an undocumented surface that breaks the moment a
    new flag creates an ambiguity."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "--work-d", "/tmp/x"])


def test_full_long_flags_still_work():
    parser = build_parser()
    args = parser.parse_args(["fetch", "--work-dir", "/tmp/x"])
    assert args.work_dir == "/tmp/x"


def test_root_epilog_links_to_docs_and_issues():
    epilog = build_parser().epilog
    assert "https://sayak.in/contextlake" in epilog
    assert "https://github.com/sayak-sarkar/contextlake/issues" in epilog


def test_unknown_command_suggests_the_closest_real_one(capsys):
    """Before the fix this dumped argparse's raw 30-item choice list -- a wall
    of text under stress with no actionable next step."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["fetc"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Unknown command: 'fetc'" in err
    assert "Did you mean: fetch?" in err
    assert "invalid choice" not in err


def test_unknown_command_suggests_the_canonical_verb_not_an_alias(capsys):
    """A typo of the alias "blast-radius" is lexically closest to that alias,
    not to "impact" -- the suggestion must still translate to the canonical
    verb, matching what --help teaches."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["blast-radiu"])
    err = capsys.readouterr().err
    assert "Did you mean: impact?" in err
    assert "blast-radius" not in err


def test_unknown_command_with_no_close_match_skips_the_suggestion_line(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["zzzzzzzzzz"])
    err = capsys.readouterr().err
    assert "Did you mean" not in err
    assert "Run 'contextlake --help'" in err


def test_n_is_a_short_form_of_dry_run():
    args = build_parser().parse_args(["sync", "-n"])
    assert args.dry_run is True


def test_plain_flag_is_available_globally():
    root_args = build_parser().parse_args(["--plain", "status"])
    assert root_args.plain is True
    sub_args = build_parser().parse_args(["status", "--plain"])
    assert sub_args.plain is True


def test_plain_sets_no_color_before_any_output(monkeypatch):
    import os

    from contextlake.cli import main

    # No subcommand: main() prints help and exits 0, but only *after* the
    # --plain -> NO_COLOR translation runs -- unlike --help, which argparse
    # itself intercepts before main()'s own code ever executes.
    monkeypatch.delenv("NO_COLOR", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--plain"])
    assert exc.value.code == 0
    assert os.environ.get("NO_COLOR") == "1"
    monkeypatch.delenv("NO_COLOR", raising=False)
