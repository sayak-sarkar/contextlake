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


def test_unrecognized_flag_followed_by_a_value_reports_the_flag_not_the_value(capsys):
    """--work-d isn't a real root flag, so argparse never learns it takes a
    value -- /tmp used to fall into the <command> positional slot instead and
    fail as 'Unknown command: /tmp', hiding the actual problem (the
    unrecognized --work-d) behind a confusing typo-suggestion message."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--work-d", "/tmp", "doctor"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: --work-d" in err
    assert "Unknown command" not in err


def test_unrecognized_flag_with_no_following_token_still_reports_correctly(capsys):
    """Regression guard: the no-trailing-token case already worked before this
    fix (no positional exists for the bad value to fall into) -- must keep
    working identically."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--work-d", "doctor"])
    err = capsys.readouterr().err
    assert "unrecognized arguments: --work-d" in err


def test_unrecognized_flag_inside_a_subcommand_still_reports_correctly(capsys):
    """Regression guard: subcommand-scope unrecognized flags (a different code
    path, argparse's own subparser error, not the root <command> positional)
    already worked before this fix -- must keep working identically."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["index", "--work-d", "/tmp"])
    err = capsys.readouterr().err
    assert "unrecognized arguments: --work-d" in err


def test_n_is_a_short_form_of_dry_run():
    args = build_parser().parse_args(["sync", "-n"])
    assert args.dry_run is True


def test_plain_flag_is_available_globally():
    root_args = build_parser().parse_args(["--plain", "status"])
    assert root_args.plain is True
    sub_args = build_parser().parse_args(["status", "--plain"])
    assert sub_args.plain is True


def test_plain_help_text_does_not_overclaim_glyph_suppression():
    """--plain (and NO_COLOR) only strip ANSI color -- the unicode status
    glyphs (check/warn/cross/...) are hardcoded literals with no ASCII
    fallback and render either way. The help text must not claim otherwise."""
    help_text = build_parser().format_help()
    plain_line = next(ln for ln in help_text.splitlines() if "--plain" in ln)
    assert "glyphs" not in plain_line


def test_plain_sets_no_color_before_any_output(monkeypatch):
    import os

    from contextlake.cli import main

    # No subcommand: main() prints help and exits 0, but only *after* the
    # --plain -> NO_COLOR translation runs -- unlike --help, which argparse
    # itself intercepts before main()'s own code ever executes.
    #
    # main() sets os.environ directly (not via monkeypatch), but this delenv
    # call still registers pytest's teardown to restore NO_COLOR to its
    # pre-test state regardless of what main() does to it afterwards -- so a
    # failed assert below can't leak NO_COLOR=1 into later tests the way a
    # manual cleanup line at the end of the test (skipped on failure) would.
    monkeypatch.delenv("NO_COLOR", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--plain"])
    assert exc.value.code == 0
    assert os.environ.get("NO_COLOR") == "1"
