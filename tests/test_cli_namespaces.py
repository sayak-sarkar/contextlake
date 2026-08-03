"""The two-level command tree (`contextlake mirror <verb>` / `contextlake kb
<verb>`), and the hard cutover that retired the old flat spellings.

There is no compatibility window: `contextlake fetch` does not parse. It fails
as an ordinary unknown command, with the suggester pointing at `mirror fetch`.
The files contextlake wrote the old forms into (the git post-commit hook, the
.mcp.json / AGENTS.md steering block) are repaired by re-running
`contextlake kb hook install` / `contextlake kb steer --force`.
"""

import pytest

from contextlake.cli import (
    _ALIASES,
    _COMMAND_CATEGORIES,
    _NAMESPACE_OF,
    _NAMESPACES,
    _resolve_command,
    build_parser,
)

MOVED = sorted(_NAMESPACE_OF)
TOP_LEVEL = ("version", "init", "completion", "bootstrap", "doctor")
# `source` is the one moved command with a required positional.
REQUIRED_ARGS = {"source": ["list"]}


def _resolve(argv):
    """Parse ``argv`` and collapse it the way main() does, returning the args."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _resolve_command(args, parser)
    return args


# --- the namespaced tree ---------------------------------------------------

@pytest.mark.parametrize("name", MOVED)
def test_every_moved_command_parses_under_its_namespace(name):
    """`args.command` stays the single dispatch key the rest of the CLI reads
    (_KB_COMMANDS, main()'s mirror chain, kb.cmds.dispatch), so nesting changed
    the invocation path and nothing else."""
    args = _resolve([_NAMESPACE_OF[name], name, *REQUIRED_ARGS.get(name, ())])
    assert args.command == name


@pytest.mark.parametrize("name", MOVED)
def test_every_moved_command_reports_its_namespaced_prog(name):
    """The leaf is created under its namespace, so its prog, usage line and
    --help all read `contextlake kb index` -- one spelling, everywhere."""
    leaf = build_parser()._command_choices[name]
    assert leaf.prog == f"contextlake {_NAMESPACE_OF[name]} {name}"


@pytest.mark.parametrize("name", TOP_LEVEL)
def test_top_level_commands_did_not_move(name):
    """init and bootstrap span BOTH tiers, version/completion belong to neither,
    and doctor is the diagnostic you reach for when nothing else works."""
    assert name not in _NAMESPACE_OF
    assert _resolve([name]).command == name


def test_the_roots_lookup_table_holds_the_namespaces_own_parser_object():
    """The root no longer PARSES a moved verb, but it still has to describe one
    (the categorized --help listing, the cross-command flag registry). It reads
    the very same parser object the namespace parses with, so there is no second
    definition to drift."""
    parser = build_parser()
    for name, ns in _NAMESPACE_OF.items():
        assert (parser._command_choices[name]
                is parser._namespace_parsers[ns]._command_choices[name])


def test_aliases_survive_under_the_namespace():
    for alias, canonical in _ALIASES.items():
        ns = _NAMESPACE_OF[canonical]
        assert _resolve([ns, alias, "x"]).command == alias


def test_three_level_positionals_with_suppress_defaults_still_parse():
    """`source`/`hook` put a `choices=`d positional a third level deep, and
    `hook`'s is additionally nargs="?" with a SUPPRESS default -- the exact
    shape that broke `completion` on Python 3.9-3.11 (see cli.py's note on the
    `shell` positional). The SUPPRESS sentinel now crosses one more
    parse_known_args boundary, so this needs its own guard."""
    assert _resolve(["kb", "source", "list"]).action == "list"
    assert _resolve(["kb", "source", "add", "jira"]).name == "jira"
    assert _resolve(["kb", "hook", "status"]).action == "status"
    assert _resolve(["kb", "hook"]).action is None
    assert _resolve(["kb", "index", "path/to/repo"]).path == "path/to/repo"


@pytest.mark.parametrize("argv", [
    ["--dry-run", "mirror", "fetch"],   # before the namespace (the root's copy)
    ["mirror", "--dry-run", "fetch"],   # between namespace and verb
    ["mirror", "fetch", "--dry-run"],   # after the verb
])
def test_pre_verb_mirror_flags_work_at_every_level(argv):
    """`--repos`/`--dry-run` before the verb is a supported style (see
    _root_hidden_flags); the namespace parser has to re-declare that surface or
    the middle form silently stops parsing."""
    assert _resolve(argv).dry_run is True


def test_pre_verb_knowledge_flags_work_at_the_namespace_level():
    assert _resolve(["kb", "--workspace", "/tmp/w", "index"]).workspace == "/tmp/w"


def test_help_advanced_is_not_a_namespace_flag():
    """--help-advanced belongs to the eight real mirror verbs only; the
    namespace parser takes _add_mirror(hidden=True), which must not add it."""
    parser = build_parser()
    for ns in _NAMESPACES:
        assert "--help-advanced" not in \
            parser._namespace_parsers[ns]._option_string_actions


# --- bare namespaces + namespace help --------------------------------------

@pytest.mark.parametrize("ns", _NAMESPACES)
def test_bare_namespace_prints_its_own_help_and_exits_clean(ns, capsys):
    """`contextlake mirror` with no verb is a first keystroke, not an error --
    the same treatment bare `contextlake` gets."""
    from contextlake import cli

    with pytest.raises(SystemExit) as exc:
        cli.main([ns])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert f"usage: contextlake {ns}" in out
    assert "Commands, by task:" in out


def test_namespace_help_lists_only_that_namespaces_commands(capsys):
    from contextlake import cli

    with pytest.raises(SystemExit):
        cli.main(["mirror"])
    out = capsys.readouterr().out
    assert "Mirror a fleet:" in out
    assert "fetch" in out
    # kb verbs and the top-level commands belong to other listings
    assert "Explore & search" not in out
    assert "bootstrap" not in out


def test_namespace_help_drops_the_redundant_prefix_from_its_group_titles(capsys):
    """The root spells the prefix out ("Explore & search (contextlake kb
    <command>)") because it mixes namespaces; inside `kb` the prefix is already
    in the usage line, so repeating it on every group is noise."""
    from contextlake import cli

    with pytest.raises(SystemExit):
        cli.main(["kb"])
    out = capsys.readouterr().out
    assert "Explore & search:" in out
    assert "contextlake kb <command>)" not in out


# --- the flat spellings are gone -------------------------------------------

@pytest.mark.parametrize("name", MOVED)
def test_no_moved_command_parses_at_the_root_any_more(name, capsys):
    """The hard cutover: there is no compatibility shim, so every moved verb is
    an unknown command at the root. This is the guard against one quietly
    reappearing in the root's choices."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([name, *REQUIRED_ARGS.get(name, ())])
    assert exc.value.code == 2
    assert f"Unknown command: {name!r}" in capsys.readouterr().err


@pytest.mark.parametrize("alias", sorted(_ALIASES))
def test_the_flat_aliases_are_gone_too(alias, capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args([alias, "Foo"])
    assert f"Unknown command: {alias!r}" in capsys.readouterr().err


@pytest.mark.parametrize("name", MOVED)
def test_a_flat_spelling_is_answered_with_its_namespaced_form(name, capsys):
    """Nothing special-cased: the old name still names a real command, so the
    ordinary unknown-command suggester matches it exactly and _qualified()
    renders where it lives now."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([name])
    assert f"Did you mean: {_NAMESPACE_OF[name]} {name}?" in capsys.readouterr().err


def test_no_leftover_deprecation_notice_on_the_namespaced_form(capsys):
    """Nothing warns any more -- and stdout in particular stays clean, which is
    what lint/query/owners/impact `--json` and `graph --format
    json|graphml|cypher|dot|mermaid` pipes depend on."""
    _resolve(["kb", "query", "CatalogService"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_namespaced_command_still_reaches_its_handler(monkeypatch, capsys):
    """End to end through main(), the path the old flat form used to take."""
    from contextlake import cli

    seen = {}
    monkeypatch.setattr(cli, "show_status",
                        lambda work_dir, config, group: seen.update(ran=True))
    cli.main(["mirror", "status"])
    assert seen == {"ran": True}
    assert "deprecated" not in capsys.readouterr().err


# --- error messages ---------------------------------------------------------

def test_a_top_level_command_typed_inside_a_namespace_says_so(capsys):
    """`contextlake kb doctor` is not a typo -- it is reaching for a top-level
    command one level too deep, the most predictable namespace mistake."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["kb", "doctor"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "'doctor' is a top-level command: run 'contextlake doctor'." in err


def test_a_typo_inside_a_namespace_suggests_a_sibling_not_a_prefixed_form(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["mirror", "stauts"])
    err = capsys.readouterr().err
    assert "Did you mean: status?" in err
    assert "Run 'contextlake mirror --help'" in err


def test_a_flag_from_another_command_is_reported_with_namespaced_spellings(capsys):
    """--local lives on init and `kb source`; naming it as a bare "source"
    would send the user to a spelling that is on its way out."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["mirror", "fetch", "--local"])
    err = capsys.readouterr().err
    assert "It's used by: init, kb source." in err
    assert "Run 'contextlake mirror fetch --help'" in err


def test_the_categorized_table_is_the_single_source_of_the_namespace_map():
    """_NAMESPACE_OF is derived from _COMMAND_CATEGORIES, and the namespace
    subparsers are built from _NAMESPACE_OF -- so the help listing and the
    parse tree cannot disagree about where a command lives."""
    derived = {name: ns for ns, _, names in _COMMAND_CATEGORIES if ns for name in names}
    assert derived == _NAMESPACE_OF
