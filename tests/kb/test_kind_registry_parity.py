"""The kind registry must cover every kind a producer can emit, and every list must
still be a projection of it.

Before the registry there were sixteen hand-maintained kind vocabularies and no test
asserting any relationship between any two of them. The strongest argument that spot-checks
do not work is `route`: it is the ONE kind somebody wrote a parity assertion for
(`test_flow_web.py`, `assert "route" in EMBEDDABLE_KINDS`) and it had still drifted out of
five other lists. A hand-written check pins the one list its author was thinking about.

These tests are structural, not judgemental. They convert a silent omission into a loud
failure; they cannot tell you that `embeddable=True` was the *wrong* answer for a kind.
"""

import re
from dataclasses import MISSING

import pytest

from contextlake.kb import hcl, parse, xml_cfg
from contextlake.kb.dashboard import site
from contextlake.kb.embeddings.index import EMBEDDABLE_KINDS
from contextlake.kb.impact import _SOURCE_KINDS
from contextlake.kb.kinds import KIND_GROUP_ORDER, KIND_REGISTRY, KindSpec, kind_groups
from contextlake.kb.visualize import diagrams, styling

# Kinds no table can be introspected for, because their producer is a regex classifier
# returning a bare string literal (connectors/atlassian.classify_link,
# connectors/slack.classify_slack_link, connectors/gitlab._item_node) or a single inline
# `kind="..."` keyword argument. Listing them here is the honest limitation; asserting
# EQUALITY below is what makes it acceptable -- a new connector kind fails this test even
# though the test could never have discovered it, and so does a removed one.
_CONNECTOR_KINDS = {"issue", "page", "design", "mr", "message", "channel"}


# One fixture per language `_member_symbols` branches on. The C++ one was alone here, and
# when the function grew branches for JavaScript, Python and the presentation tier this
# introspection could not see them: it reported six registered kinds as "produced by
# nothing" while they were being produced on every parse. A fixture that covers one branch
# is a list in disguise.
_MEMBER_FIXTURES = {
    "cpp": (b"#define M 1\n"
            b"typedef int T;\n"
            b"enum E { A };\n"
            b"int g = 0;\n"
            b"class C { int f_; };\n"),
    "javascript": b'const g = 1;\nclass C { f = 2; }\n',
    # The `if __name__` guard is here because Python's entry point is the ONE that
    # cannot be a re-kind: there is no definition node to re-kind, so this branch of
    # `_py_member_symbols` is its only producer and a fixture without it is blind to it.
    "python": b'g = 1\n\n\nclass C:\n    f = 2\n\n\nif __name__ == "__main__":\n    pass\n',
    "css": b'.c { }\n#i { }\nel { }\n',
    "html": b'<div id="i"></div>\n',
    "nix": b'{ attr = 1; }\n',
    # Two targets on one rule, because that is the shape a single-target fixture would
    # let a "targets node text" implementation pass while emitting one node named
    # "build test". The `.PHONY` line is here for the same reason: without a special
    # target present, the guard that drops them is never exercised.
    "make": b'.PHONY: build test\nbuild test:\n\techo hi\n',
    # Two FROMs, so both kinds this branch emits are exercised: the stage and the
    # external base image, which `_extra_imports` handles rather than this branch.
    "dockerfile": b'FROM node:20 AS builder\nFROM builder AS test\n',
}


def _member_symbol_kinds() -> set[str]:
    """Kinds emitted by `parse._member_symbols`, read off fixtures rather than a list.

    A hand-kept list here would be one more vocabulary to drift; parsing fixtures that
    exercise every branch keeps it honest.

    A language whose grammar is an OPTIONAL extra can be genuinely absent, and then this
    introspection cannot see its branch. That is reported as a skip naming the language,
    never worked around by assuming its kinds: silently substituting them would turn the
    one guard that catches unproduced vocabulary into a guard that asserts a list.
    """
    import tree_sitter as ts

    out: set[str] = set()
    for lang, src in _MEMBER_FIXTURES.items():
        try:
            language = parse._language(lang)
        except ImportError as exc:
            pytest.skip(f"cannot introspect the {lang} branch: {exc}")
        tree = ts.Parser(language).parse(src)
        got = {kind for kind, _, _ in parse._member_symbols(tree, lang)}
        assert got, (
            f"the {lang} fixture produced no member symbols, so this introspection is "
            f"blind to that branch and would report its kinds as unproduced")
        out |= got
    return out


def _produced_kinds() -> set[str]:
    """Every kind some producer in the codebase can write onto a node."""
    return (
        # tree-sitter definition types, introspected -- self-maintaining
        {k for m in parse._DEF_TYPES.values() for k in m.values()}
        # parse.py's own literals: the two RE-KINDS (test-macro, and the entry-point
        # check that turns a qualifying `main` into `entry_point`), plus file/module
        # nodes. Neither re-kind is in `_DEF_TYPES`, so introspecting that table alone
        # reports both as produced by nothing.
        | {"test", "file", "module", "entry_point"}
        # parse.py's member pass (`_member_symbols`), which is a SEPARATE producer from
        # _DEF_TYPES. Introspecting only _DEF_TYPES made this guard silently pass while
        # five brand-new kinds went unregistered -- the exact drift it exists to catch.
        | _member_symbol_kinds()
        # HCL block keywords, introspected, plus the `local.<attr>` special case
        | hcl._DEF_BLOCKS | {"local"}
        | {"table", "view", "procedure"}                 # kb/sql.py DDL regexes
        | {"adr", xml_cfg.CONFIG_KIND}                   # kb/adr.py, kb/xml_cfg.py
        | {"endpoint", "topic", "route", "state"}        # kb/flow/*
        | {"package"}                                    # kb/manifest.py
        | {"repo", "namespace", "system"}                # connectors/common.py, kb/c4.py
        | _CONNECTOR_KINDS
        | {"document", "wiki"}                           # kb/cmds/ingest.py, kb/cmds/wiki.py
    )


def test_every_produced_kind_is_registered():
    """Equality, not subset: a kind added to a parser and forgotten fails here, and so
    does a registry row for a kind nothing emits any more (`_SOURCE_KINDS` carried a dead
    `type` entry for exactly that reason)."""
    produced, registered = _produced_kinds(), set(KIND_REGISTRY)
    assert produced == registered, (
        f"produced but unregistered: {sorted(produced - registered)}; "
        f"registered but produced by nothing: {sorted(registered - produced)}. "
        "Add the row to kb/kinds.py KIND_REGISTRY (it will force you to answer every "
        "question the vocabularies ask), or remove the stale row."
    )


def test_every_kind_has_a_colour_so_every_kind_can_be_filtered():
    """A missing colour is not cosmetic. `visualize/html_render` builds the graph page's
    kind filter by iterating KIND_COLORS rather than the graph, so a kind with no colour
    gets no legend button and cannot be isolated or hidden -- 16 produced kinds were in
    that state, including table/view/resource, which run to hundreds of nodes per repo."""
    assert set(styling.KIND_COLORS) == set(KIND_REGISTRY)
    bad = {k: s.color for k, s in KIND_REGISTRY.items()
           if not re.fullmatch(r"#[0-9a-f]{6}", s.color)}
    assert not bad, f"not a lowercase #rrggbb colour: {bad}"


def test_every_non_embeddable_kind_records_why():
    """The forcing function, and the one that would have caught the live defect.

    `embeddings/index.py` documented why nine kinds were excluded from semantic search
    and was silent on `config_key` and `test` -- so nobody could tell a considered
    exclusion from an oversight, and "where is the retry timeout configured?" quietly
    returned nothing from the kind created to answer it. A reason is now a field, and an
    empty one is a test failure.
    """
    missing = sorted(k for k, s in KIND_REGISTRY.items()
                     if not s.embeddable and not s.why_not_embeddable.strip())
    assert not missing, (
        f"excluded from EMBEDDABLE_KINDS with no stated reason: {missing}. Write it in "
        "the registry's why_not_embeddable -- 'eligible, deferred' is a fine answer, "
        "'nobody decided' is what this test exists to prevent."
    )
    contradictory = sorted(k for k, s in KIND_REGISTRY.items()
                           if s.embeddable and s.why_not_embeddable)
    assert not contradictory, f"embeddable yet carrying an exclusion reason: {contradictory}"


def test_embeddable_membership_is_pinned():
    """Membership is a sequenced decision, not a refactoring detail.

    The set feeds the per-kind embedding budget floors, so widening it evicts vectors that
    already exist and re-embedding is the only repair. This literal is therefore a gate:
    changing it should mean somebody chose to, and re-ran `kb embed`. `config_key` and
    `test` are the two known candidates -- they are recorded in the registry as eligible
    and deferred, deliberately not added here.

    Widened once, deliberately, on 2026-08-12: the five C/C++ symbol kinds joined after an
    A/B on a large legacy tree. Before the change their semantic recall was **exactly
    zero** -- tens of thousands of symbols no query could reach. The cost is real and was
    measured rather than assumed: -5.25pp existing-kind recall@10 for +180% vectors, with
    `field` alone responsible for over half of it. `EMBED_CONTENT_VERSION` moved to 4 in
    the same change, because widening this set leaves an existing store INCOMPLETE and
    nothing else would have noticed. See `testing/d8-embedding-measurement.md`.
    """
    assert EMBEDDABLE_KINDS == frozenset({
        "class", "function", "method", "interface", "struct", "enum",
        "endpoint", "route", "resource", "table", "view", "adr",
        "field", "macro", "typedef", "enum_constant", "global_variable"})


def test_no_gate_names_a_kind_the_registry_does_not_have():
    """Every behaviour-gating set is a projection today; this fails the moment one is
    re-hardcoded with a kind the registry never heard of (which is how `_SOURCE_KINDS`
    came to test membership against a `type` kind no producer emits)."""
    registered = set(KIND_REGISTRY)
    gates = {
        "EMBEDDABLE_KINDS": set(EMBEDDABLE_KINDS),
        "impact._SOURCE_KINDS": set(_SOURCE_KINDS),
        "site._IMPACT_KINDS": set(site._IMPACT_KINDS),
        "parse._CALLABLE_KINDS": set(parse._CALLABLE_KINDS),
        "parse._INHERITABLE_KINDS": set(parse._INHERITABLE_KINDS),
        "parse._HCL_KINDS": set(parse._HCL_KINDS),
        "parse._SQL_KINDS": set(parse._SQL_KINDS),
        "diagrams._CLASSIFIER_KINDS": set(diagrams._CLASSIFIER_KINDS),
        "diagrams._CLASS_MEMBER_KINDS": set(diagrams._CLASS_MEMBER_KINDS),
        "diagrams._ER_ENTITY_KINDS": set(diagrams._ER_ENTITY_KINDS),
    }
    unknown = {name: sorted(s - registered) for name, s in gates.items() if s - registered}
    assert not unknown, f"gates naming unregistered kinds: {unknown}"


def test_kindspec_has_no_field_defaults():
    """The mechanism that makes "absent from a gate" impossible as a *silent* default.

    Every semantic flag except `embeddable` (covered above) is enforced structurally rather
    than by an assertion: `KindSpec` cannot be constructed without stating each one, so a
    new kind's answers all appear in one reviewable hunk. The moment a field grows a default
    that guarantee is gone and omission becomes silent again -- which is exactly how the old
    lists behaved, since not being in a set is indistinguishable from nobody having looked.
    """
    defaulted = sorted(name for name, f in KindSpec.__dataclass_fields__.items()
                       if f.default is not MISSING or f.default_factory is not MISSING)
    assert not defaulted, (
        f"KindSpec fields with defaults: {defaulted}. Remove the default: an unstated answer "
        "must be a construction error, not a quiet False."
    )


def test_every_kind_lands_in_exactly_one_doc_group():
    """`kind_groups()` is the published vocabulary diagram's taxonomy. Its hand-written
    predecessor documented 16 of 35 kinds on the page a reader would trust for the
    answer."""
    grouped = [k for _, kinds in kind_groups() for k in kinds]
    assert sorted(grouped) == sorted(KIND_REGISTRY)
    assert len(grouped) == len(set(grouped))
    empty = [g for g, kinds in kind_groups() if not kinds]
    assert not empty, f"declared in KIND_GROUP_ORDER but holding no kinds: {empty}"
    strays = sorted({s.group for s in KIND_REGISTRY.values()} - set(KIND_GROUP_ORDER))
    assert not strays, f"kind groups missing from KIND_GROUP_ORDER: {strays}"


def test_every_parsed_language_has_a_lettermark():
    """The kind vocabulary is pinned above; the LANGUAGE vocabulary was not, and it drifted.

    `visualize/styling._LANG_LABELS` supplies the lettermark overlaid on a repo node.
    A language the parser emits but this map has never heard of gets no glyph at all,
    which renders as "this node has no language" rather than as a missing entry --
    silent in exactly the way the kind-colour drift was, and found the same way: by
    someone comparing two lists by hand.

    Scala was in that state. It parsed `.scala`/`.sc` files and carried no lettermark.

    Reads `ALL_LANGS`, the union of both routing tables, NOT `LANG_BY_EXT`. A language
    a file reaches by name has no extension entry at all, so checking the extension
    table alone would report full coverage while every build file went unglyphed.
    """
    from contextlake.kb.parse import ALL_LANGS
    from contextlake.kb.visualize.styling import _LANG_LABELS

    missing = sorted(ALL_LANGS - set(_LANG_LABELS))
    assert not missing, (
        f"languages the parser emits with no lettermark: {missing}. Add each to "
        "`_LANG_LABELS` in kb/visualize/styling.py -- a repo node in that language "
        "currently renders with no language glyph."
    )


def test_the_lettermark_map_holds_no_language_that_is_not_an_alias():
    """The other direction, kept deliberately loose.

    An entry for a language the parser does not emit is dead weight, not a defect --
    `c_sharp` is retained on purpose as an alias of `csharp` for stores written before
    the id settled. So this asserts the *set of strays is exactly the aliases we chose*,
    which fails when somebody adds a third one without saying why, rather than banning
    strays outright."""
    from contextlake.kb.parse import ALL_LANGS
    from contextlake.kb.visualize.styling import _LANG_LABELS

    strays = set(_LANG_LABELS) - ALL_LANGS
    assert strays == {"c_sharp"}, (
        f"unexpected lettermark entries for languages the parser never emits: "
        f"{sorted(strays - {'c_sharp'})}. Either the language was removed and this "
        "entry is dead, or a new alias needs a comment saying which id it stands in for."
    )
