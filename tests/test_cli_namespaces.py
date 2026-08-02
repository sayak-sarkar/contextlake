"""The two-level command tree (`contextlake mirror <verb>` / `contextlake kb
<verb>`) and the deprecation shim that keeps the old flat spellings working.

The shim exists because contextlake wrote the old forms into files it does not
revisit -- the git post-commit hook it installs and the .mcp.json / AGENTS.md it
generates -- where a hard cutover fails *silently*: no error the user ever sees,
just a graph that quietly stops updating.
"""

import pytest

from contextlake.cli import (
    _ALIASES,
    _COMMAND_CATEGORIES,
    _DEPRECATION_REMOVED_IN,
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
    """One parser object serves both spellings, and it is created under the
    namespace -- so even the deprecated `contextlake fetch --help` prints the
    namespaced usage line, teaching the form the user should switch to."""
    leaf = build_parser()._command_choices[name]
    assert leaf.prog == f"contextlake {_NAMESPACE_OF[name]} {name}"


@pytest.mark.parametrize("name", TOP_LEVEL)
def test_top_level_commands_did_not_move(name):
    """init and bootstrap span BOTH tiers, version/completion belong to neither,
    and doctor is the diagnostic you reach for when nothing else works."""
    assert name not in _NAMESPACE_OF
    assert _resolve([name]).command == name


def test_namespaced_and_flat_spellings_resolve_to_the_same_parser_object():
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


# --- the deprecation shim ---------------------------------------------------

@pytest.mark.parametrize("name", MOVED)
def test_flat_spelling_still_parses_and_warns(name, capsys):
    args = _resolve([name, *REQUIRED_ARGS.get(name, ())])
    assert args.command == name
    err = capsys.readouterr().err
    assert f"'contextlake {name}' is deprecated" in err
    assert f"use 'contextlake {_NAMESPACE_OF[name]} {name}'" in err
    assert _DEPRECATION_REMOVED_IN in err


def test_the_warning_never_touches_stdout(capsys):
    """lint/query/owners/impact all have --json and `graph --format
    json|graphml|cypher|dot|mermaid` writes machine-readable stdout -- a notice
    there would corrupt every one of those pipes."""
    _resolve(["query", "CatalogService"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "deprecated" in captured.err


@pytest.mark.parametrize("argv", [["mirror", "fetch"], ["kb", "index"], ["doctor"],
                                  ["bootstrap"], ["init"], ["version"]])
def test_no_warning_for_the_namespaced_or_top_level_forms(argv, capsys):
    _resolve(argv)
    assert "deprecated" not in capsys.readouterr().err


def test_the_warning_names_the_alias_the_user_actually_typed(capsys):
    _resolve(["blast-radius", "Foo"])
    err = capsys.readouterr().err
    assert "'contextlake blast-radius' is deprecated" in err
    assert "use 'contextlake kb blast-radius'" in err


def test_quiet_suppresses_the_warning(capsys):
    _resolve(["-q", "status"])
    assert "deprecated" not in capsys.readouterr().err


def test_the_env_var_suppresses_the_warning(monkeypatch, capsys):
    """So a team's CI logs stay clean while they migrate."""
    monkeypatch.setenv("CONTEXTLAKE_NO_DEPRECATION", "1")
    _resolve(["status"])
    assert "deprecated" not in capsys.readouterr().err


def test_the_warning_does_not_break_dispatch(monkeypatch, capsys):
    """End to end through main(): the notice is additive -- the deprecated verb
    still reaches its handler with the same arguments."""
    from contextlake import cli

    seen = {}
    monkeypatch.setattr(cli, "show_status",
                        lambda work_dir, config, group: seen.update(ran=True))
    cli.main(["status"])
    assert seen == {"ran": True}
    assert "deprecated" in capsys.readouterr().err


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
