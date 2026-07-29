"""Regression tests for CLI UX fixes: no silent flag abbreviation, discoverable
help epilog. See docs/... review notes for the source of these findings.
"""

import pytest

from contextlake.cli import _ALIASES, _COMMAND_CATEGORIES, _KB_COMMANDS, build_parser


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


def test_flag_valid_on_a_different_command_names_that_command(capsys):
    """`bootstrap --local` used to dump argparse's generic "unrecognized
    arguments: --local" with no hint that --local is real, just not here (it's
    an init/source flag) -- a genuine, observed stumble."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["bootstrap", "--local"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "'--local' isn't a flag on 'bootstrap'" in err
    assert "init" in err and "source" in err
    assert "unrecognized arguments" not in err


def test_flag_typo_on_the_invoked_command_suggests_the_real_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["index", "--worksapce", "."])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Unknown flag: '--worksapce'" in err
    assert "Did you mean: --workspace?" in err


def test_flag_valid_elsewhere_check_runs_before_same_command_fuzzy_guess(capsys):
    """--local is lexically close to --llm (both short, both start '--l'), which
    at a loose cutoff would have "corrected" it to the wrong, unrelated flag --
    the exact used-by-another-command match must win over that fuzzy guess."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bootstrap", "--local"])
    err = capsys.readouterr().err
    assert "--llm" not in err


def test_unrecognized_flag_with_no_close_match_anywhere_falls_back(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["index", "--totally-bogus-flag"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: --totally-bogus-flag" in err
    assert "isn't a flag on" not in err and "Unknown flag" not in err


def test_value_taking_flag_followed_by_another_flag_names_the_real_problem(capsys):
    """`dashboard --serve --workspace --open` -- argparse's own "expected one
    argument" reads as "you forgot a value", when the real issue is --open
    landing where --workspace's value belongs."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["dashboard", "--serve", "--workspace", "--open"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "'--workspace' needs a value" in err
    assert "'--open'" in err
    assert "expected one argument" not in err


def test_value_taking_flag_genuinely_missing_a_value_keeps_argparses_message(capsys):
    """Regression guard: a flag missing its value with nothing (not even
    another flag) after it has no better explanation to offer -- must not
    regress to a worse or misleading message."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["dashboard", "--workspace"])
    err = capsys.readouterr().err
    assert "expected one argument" in err
    assert "needs a value" not in err


def test_mirror_command_help_hides_resilience_flags_by_default(capsys):
    """The 14 resilience/tuning flags (retry/backoff/worker-pool/safety-check
    overrides) clutter --help without being something a user guesses at --
    each already has a .contextlake.ini equivalent, so hiding them costs
    nothing. Default --help should show only the 4 common mirror flags."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch", "--help"])
    out = capsys.readouterr().out
    assert "--work-dir" in out
    assert "--dry-run" in out
    assert "--max-retries" not in out
    assert "--auto-stash" not in out


def test_mirror_command_help_advanced_reveals_resilience_flags(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch", "--help-advanced"])
    out = capsys.readouterr().out
    assert "--max-retries" in out
    assert "max retry attempts for failed operations" in out
    assert "--auto-stash" in out


def test_resilience_flags_still_parse_even_though_hidden():
    """Hiding help text must not remove the flag itself -- these are still the
    documented way to override the sync INI from the command line."""
    args = build_parser().parse_args(
        ["fetch", "--max-retries", "5", "--auto-stash"])
    assert args.max_retries == 5
    assert args.auto_stash is True


def test_typo_suggester_still_matches_a_hidden_resilience_flag(capsys):
    """The flag-typo suggester reads _option_string_actions directly, not the
    visible help text, so a hidden flag must still get a real suggestion."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["fetch", "--max-retrie", "5"])
    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "Did you mean: --max-retries?" in err


def test_help_advanced_on_the_wrong_command_gives_argparses_plain_message(capsys):
    """--help-advanced only exists on the 8 mirror commands; running it on any
    other command (e.g. index) must NOT trigger the cross-command 'used by'
    message -- that path is for real domain flags, and a 9-command 'used by:
    audit, bootstrap, ...' list for a help flag is noise, not help."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["index", "--help-advanced"])
    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "isn't a flag on" not in err
    assert "unrecognized arguments: --help-advanced" in err


def test_help_advanced_is_not_a_root_flag():
    """--help-advanced only exists on the real mirror subcommands; the root
    parser's hidden _add_mirror(hidden=True) copy (used for cross-command
    flag-suggestion matching) has no use for it."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help-advanced"])


@pytest.mark.parametrize("name", [
    "fetch", "clone", "update", "branches", "verify", "status", "audit",
])
def test_every_mirror_command_has_an_examples_epilog(name):
    """Before this fix, only sync/bootstrap/init/etc. carried a worked example
    in their own --help; the plain mirror-core commands did not, an
    inconsistency a user hits the moment they run e.g. `update --help`."""
    parser = build_parser()._command_choices[name]
    assert "Examples:" in (parser.epilog or "")
    assert f"contextlake {name}" in parser.epilog


def test_every_registered_command_is_categorized_exactly_once():
    """The categorized --help listing must never silently drop (or double-list)
    a real command -- every canonical name registered on the root parser
    (excluding aliases, which _COMMAND_CATEGORIES never lists separately) has
    to appear in exactly one category."""
    all_commands = {name for name in build_parser()._command_choices
                    if name not in _ALIASES}
    categorized = [name for _, names in _COMMAND_CATEGORIES for name in names]
    assert len(categorized) == len(set(categorized)), "a command is listed twice"
    assert set(categorized) == all_commands


def test_long_command_descriptions_wrap_to_terminal_width_not_mid_line(monkeypatch):
    """bootstrap/dashboard/source/etc. have long descriptions; RawDescription-
    HelpFormatter never wraps epilog text, so without manual wrapping these
    would run well past 80 columns on a plain line with no re-indent."""
    from contextlake.cli import _categorized_commands_text

    monkeypatch.setenv("COLUMNS", "80")
    text = _categorized_commands_text(build_parser()._command_choices)
    for line in text.splitlines():
        assert len(line) <= 80, f"line exceeds 80 columns: {line!r}"
    # the wrapped continuation re-indents under the description column, not col 0
    assert "\n                           config" in text


def test_bare_invocation_with_no_command_shows_the_categorized_help():
    """`contextlake` with no arguments is the single most common first
    invocation (subparsers are required=False) -- must render the new
    categorized help cleanly, not error, and not fall back to the old flat
    listing."""
    from contextlake.cli import main

    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 0


def test_root_help_shows_the_categorized_list_not_the_flat_one(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert "Commands, by task:" in out
    assert "Mirror a fleet:" in out
    assert "owners (who-knows)" in out
    # the old flat argparse listing (one un-grouped line per command) is gone
    assert "\ncommands:\n" not in out


def test_completion_command_parses_with_and_without_a_shell_override():
    parser = build_parser()
    assert parser.parse_args(["completion"]).shell is None
    assert parser.parse_args(["completion", "zsh"]).shell == "zsh"


def test_completion_rejects_an_unsupported_shell_name(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["completion", "powershell"])


def test_completion_is_not_a_kb_command():
    """Shell completion has nothing to do with the knowledge layer -- it must
    work in the base (mirror-only) install, not require the [kb] extra."""
    assert "completion" not in _KB_COMMANDS


def test_auto_register_completion_runs_once_and_does_not_swallow_the_real_exit_code(monkeypatch):
    """The zero-step auto-check fires before a command's own config
    validation (a deliberate ordering decision, see its call site's comment
    in main()) -- confirm it doesn't also swallow or alter that command's own
    exit code on a genuine failure (`serve --config <path that doesn't
    exist>` reliably exits 1, see docs/... the embedder/server config-path
    guard)."""
    from contextlake import cli, init_cmd

    calls = []
    monkeypatch.setattr(init_cmd, "maybe_auto_register_completion", lambda **kw: calls.append(1))
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--config", "/definitely/does/not/exist.toml"])
    assert calls == [1]
    assert exc.value.code == 1


@pytest.mark.parametrize("command_args", [["init", "--yes"], ["completion"]])
def test_auto_register_completion_is_skipped_for_init_and_completion(monkeypatch, command_args):
    """init and `completion` already own this exact decision explicitly --
    the auto-check must never also run (and potentially double-register or
    re-log) underneath them."""
    from contextlake import cli, init_cmd

    calls = []
    monkeypatch.setattr(init_cmd, "maybe_auto_register_completion", lambda **kw: calls.append(1))
    monkeypatch.setattr(init_cmd, "cmd_init", lambda args: 0)
    monkeypatch.setattr(init_cmd, "cmd_completion", lambda args: 0)
    with pytest.raises(SystemExit):
        cli.main(command_args)
    assert calls == []


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


def test_version_subcommand_matches_the_version_flag(capsys):
    """`contextlake version` was previously an unknown command -- a very
    natural first guess (docker/npm/kubectl all support both spellings) --
    reported as "Unknown command: 'version'". It must print the exact same
    string as `--version`, not a paraphrase that could drift from it."""
    from contextlake.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    flag_output = capsys.readouterr().out.strip()

    with pytest.raises(SystemExit) as exc:
        main(["version"])
    assert exc.value.code == 0
    subcommand_output = capsys.readouterr().out.strip()

    from contextlake import __version__

    assert flag_output == subcommand_output == f"contextlake {__version__}"


def test_argcomplete_engaged_when_importable(monkeypatch):
    """Shell completion (pip install "contextlake[completion]") is wired as a
    lazy, optional import in main() -- ImportError is caught so the core
    tool's zero-dependency promise holds when it's absent (the normal case in
    this dev venv; see test_argcomplete_absence_is_a_silent_noop below). This
    proves the *present* branch actually calls into it rather than silently
    no-op'ing every time, using a stub instead of a hard new test dependency
    on the real package."""
    import sys
    import types

    calls = []
    stub = types.ModuleType("argcomplete")
    stub.autocomplete = lambda parser: calls.append(parser)
    monkeypatch.setitem(sys.modules, "argcomplete", stub)

    from contextlake.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert len(calls) == 1


def test_argcomplete_absence_is_a_silent_noop(monkeypatch):
    """The common case (extra not installed): main() must still run normally,
    not crash, when `import argcomplete` raises ImportError."""
    import builtins
    import sys

    monkeypatch.delitem(sys.modules, "argcomplete", raising=False)
    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "argcomplete":
            raise ImportError("simulated: extra not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    from contextlake.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
