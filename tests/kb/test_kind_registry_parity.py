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


def _member_symbol_kinds() -> set[str]:
    """Kinds emitted by `parse._member_symbols`, read off a fixture rather than a list.

    A hand-kept list here would be one more vocabulary to drift; parsing a fixture that
    exercises every branch keeps it honest."""
    import tree_sitter as ts
    src = (b"#define M 1\n"
           b"typedef int T;\n"
           b"enum E { A };\n"
           b"int g = 0;\n"
           b"class C { int f_; };\n")
    tree = ts.Parser(parse._language("cpp")).parse(src)
    return {kind for kind, _, _ in parse._member_symbols(tree, "cpp")}


def _produced_kinds() -> set[str]:
    """Every kind some producer in the codebase can write onto a node."""
    return (
        # tree-sitter definition types, introspected -- self-maintaining
        {k for m in parse._DEF_TYPES.values() for k in m.values()}
        # parse.py's own literals: the test-macro re-kind, plus file/module nodes
        | {"test", "file", "module"}
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
