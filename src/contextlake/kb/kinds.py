"""The node-kind registry: one row per kind, and every kind vocabulary projected from it.

Before this module the vocabulary was sixteen hand-maintained lists in twelve files -- a
colour map, two glyph tables (Python + JavaScript) plus an SVG sprite, an embeddable set,
two impact sets, four name-resolution target sets, five diagram gates, and a doc taxonomy.
Adding a kind meant remembering all of them, and nothing checked that you had. Measured
drift at the time this module was written: 16 of the 35 produced kinds had no colour, and
because the graph page builds its kind filter by iterating the colour map rather than the
graph, "no colour" silently meant **no filter button at all** -- the nodes rendered but
could not be isolated or hidden. `config_key` reached the JS glyph map but not the sprite,
which is *worse* than being absent from both: registering a kind is exactly what disables
the generic file-icon fallback, so the browser rendered a blank box.

The fix is deliberately NOT one merged list. A colour map and an embeddable set answer
genuinely different questions, and collapsing them would destroy considered exclusions
(``file`` has a colour and must never be embedded). Instead: unify the *table*, project the
*sets*. Every list below is a one-line comprehension at its original definition site, so
consumers and imports did not move; what changed is that a new kind now has to answer every
question once, in one place, in one diff -- and :class:`KindSpec` has no defaults, so it
cannot be constructed without answering.

The existence proof that derivation works is older than this module: ``visualize/payload.py``
``_kind_floors`` and ``wiki/generate.py``'s floor slots have always been computed from the
kinds actually present at runtime, and are the only two vocabularies that never drifted.

Leaf-level on purpose -- imports nothing from ``kb/*``, so ``parse``, ``impact``,
``embeddings``, ``visualize`` and ``dashboard`` can all read it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

# Glyph artwork (inner SVG paths, Lucide-style line icons, bare 24x24 path content).
# Named constants rather than inline strings so KIND_REGISTRY below stays readable as a
# table. `visualize/styling.py` renders these into contrast-stroked data-URIs and
# `dashboard/static/dashboard.html` mirrors them as <symbol> defs -- one artwork per kind,
# so a "class" reads identically in the graph page and in the dashboard.
_G_FILE = ('<path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12'
           'a2 2 0 0 0 2-2V8z"/>')
_G_PAGE = ('<path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12'
           'a2 2 0 0 0 2-2V8z"/><line x1="8" y1="13" x2="15" y2="13"/>'
           '<line x1="8" y1="17" x2="15" y2="17"/>')
_G_MODULE = ('<path d="M12 2 2 7l10 5 10-5z"/><path d="M2 17l10 5 10-5"/>'
             '<path d="M2 12l10 5 10-5"/>')
_G_CLASS = ('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8'
            'v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>')
_G_STRUCT = ('<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" '
             'height="7"/><rect x="14" y="14" width="7" height="7"/>'
             '<rect x="3" y="14" width="7" height="7"/>')
_G_INTERFACE = ('<path d="M8 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h1"/>'
                '<path d="M16 3h1a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-1"/>')
_G_ENUM = ('<circle cx="4" cy="6" r="1.3"/><circle cx="4" cy="12" r="1.3"/>'
           '<circle cx="4" cy="18" r="1.3"/><line x1="9" y1="6" x2="21" y2="6"/>'
           '<line x1="9" y1="12" x2="21" y2="12"/><line x1="9" y1="18" x2="21" y2="18"/>')
# function and method share one glyph deliberately: the distinction is ownership, not
# shape, and the node label already carries the qualified name.
_G_CALLABLE = '<polyline points="8 7 3 12 8 17"/><polyline points="16 7 21 12 16 17"/>'
_G_PACKAGE = ('<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8'
              'a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>'
              '<path d="M3.3 7 12 12l8.7-5"/><line x1="12" y1="22" x2="12" y2="12"/>')
_G_REPO = ('<rect x="3" y="4" width="18" height="7" rx="1.5"/>'
           '<rect x="3" y="13" width="18" height="7" rx="1.5"/>'
           '<line x1="7" y1="7.5" x2="7.01" y2="7.5"/>'
           '<line x1="7" y1="16.5" x2="7.01" y2="16.5"/>')
_G_ISSUE = ('<circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/>'
            '<line x1="12" y1="16" x2="12.01" y2="16"/>')
_G_DESIGN = '<path d="M12 3l1.9 5.1L19 11l-5.1 1.9L12 18l-1.9-5L5 11l5.1-1.9z"/>'
_G_ENDPOINT = ('<circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/>'
               '<path d="M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z"/>')
_G_TOPIC = '<path d="M21 14a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'
# sliders, the conventional settings mark
_G_CONFIG_KEY = ('<line x1="4" y1="8" x2="20" y2="8"/><line x1="4" y1="16" x2="20" y2="16"/>'
                 '<circle cx="9" cy="8" r="2"/><circle cx="15" cy="16" r="2"/>')
# a check inside a ring
_G_TEST = '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>'


@dataclass(frozen=True)
class KindSpec:
    """Everything the codebase needs to know about one node kind.

    No field has a default. That is the whole mechanism: a new kind cannot be added
    without answering every question a consumer asks about node identity, and a
    reviewer sees all of those answers in one hunk.
    """

    color: str
    """Legend/node fill (``#rrggbb``). Load-bearing beyond cosmetics: the graph page's
    kind filter is built by iterating the colour projection, so a kind without one gets
    no filter button."""

    glyph: str | None
    """Inner SVG path content, or ``None`` for "no artwork yet" (degrades to a plain
    colour swatch in the graph and to the generic file icon in the dashboard). Whatever
    is set here must be mirrored by the dashboard sprite -- see
    ``tests/kb/test_dashboard_kind_glyph_parity.py``."""

    group: str
    """Which band the kind sits in on the published vocabulary diagram. Must be a member
    of :data:`KIND_GROUP_ORDER`."""

    embeddable: bool
    """In the *code-symbol* embedding set (``embeddings/index.EMBEDDABLE_KINDS``), which
    embeds name + signature + docstring. False does not always mean "unsearchable":
    ``document`` and ``wiki`` bodies are embedded by a separate content path."""

    why_not_embeddable: str
    """Required (non-empty) whenever :attr:`embeddable` is False, empty otherwise. Promoted
    from a prose comment to a field because an unexplained absence is indistinguishable
    from an oversight -- which is precisely how ``config_key`` and ``test`` ended up
    excluded from semantic search with nothing recording whether anyone had decided."""

    impact_source: bool
    """Ranks first when an impact walk disambiguates several nodes sharing one name
    (``impact._SOURCE_KINDS``). Absent -> the walk silently seeds a different node."""

    impact_precompute: bool
    """Gets a reverse-blast-radius precompute in a ``--site`` snapshot
    (``dashboard/site._IMPACT_KINDS``)."""

    classifier: bool
    """Renders as a box in the Mermaid class diagram (``visualize/diagrams``)."""

    class_member: bool
    """Renders *inside* a classifier's box when contained by one."""

    er_entity: bool
    """Renders as an entity in the ER diagram."""

    callable_target: bool
    """A ``calls`` reference may resolve to it (``parse._CALLABLE_KINDS``). Absent -> the
    edge is never created, and nothing reports the omission; this is the quietest gate
    in the codebase."""

    inheritable_target: bool
    """An ``inherits`` reference may resolve to it (``parse._INHERITABLE_KINDS``)."""

    hcl_ref_target: bool
    """A Terraform ``depends_on``/interpolation reference may resolve to it."""

    sql_ref_target: bool
    """A SQL foreign-key reference may resolve to it."""


# Doc-diagram bands, in render order. The five original bands keep their original order;
# the four new ones are inserted before the cross-source/boundary tail.
KIND_GROUP_ORDER = (
    "Symbols", "Containers", "Service surfaces", "Data model", "Infrastructure",
    "Configuration", "Documents", "Cross-source", "Boundary",
)


# Insertion order is the graph page's legend button order (the colour projection is a
# dict and `html_render` iterates it), which is why this is ordered for the legend rather
# than grouped by family: the first 19 rows preserve the order those kinds had before the
# registry existed, and the 16 that had no colour at all are appended by family.
KIND_REGISTRY: dict[str, KindSpec] = {
    "file": KindSpec(
        color="#8ecae6", glyph=_G_FILE, group="Containers",
        embeddable=False, why_not_embeddable="a path is not a semantic query",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "module": KindSpec(
        color="#ffb703", glyph=_G_MODULE, group="Containers",
        embeddable=False,
        why_not_embeddable=("shared cross-repo node: one id is re-embedded once per "
                            "referencing repo (wasted compute, inflated written count) "
                            "and dilutes results; the dependents/flow tools cover it"),
        impact_source=False, impact_precompute=True,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=True, sql_ref_target=False),
    "class": KindSpec(
        color="#fb8500", glyph=_G_CLASS, group="Symbols",
        embeddable=True, why_not_embeddable="",
        impact_source=True, impact_precompute=True,
        classifier=True, class_member=False, er_entity=False,
        callable_target=True, inheritable_target=True,
        hcl_ref_target=False, sql_ref_target=False),
    "interface": KindSpec(
        color="#fd9e02", glyph=_G_INTERFACE, group="Symbols",
        embeddable=True, why_not_embeddable="",
        impact_source=True, impact_precompute=True,
        classifier=True, class_member=False, er_entity=False,
        callable_target=True, inheritable_target=True,
        hcl_ref_target=False, sql_ref_target=False),
    "struct": KindSpec(
        color="#f4a261", glyph=_G_STRUCT, group="Symbols",
        embeddable=True, why_not_embeddable="",
        impact_source=True, impact_precompute=True,
        classifier=True, class_member=False, er_entity=False,
        callable_target=True, inheritable_target=True,
        hcl_ref_target=False, sql_ref_target=False),
    "function": KindSpec(
        color="#90be6d", glyph=_G_CALLABLE, group="Symbols",
        embeddable=True, why_not_embeddable="",
        impact_source=True, impact_precompute=True,
        classifier=False, class_member=True, er_entity=False,
        callable_target=True, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "method": KindSpec(
        color="#43aa8b", glyph=_G_CALLABLE, group="Symbols",
        embeddable=True, why_not_embeddable="",
        impact_source=True, impact_precompute=True,
        classifier=False, class_member=True, er_entity=False,
        callable_target=True, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "enum": KindSpec(
        color="#577590", glyph=_G_ENUM, group="Symbols",
        embeddable=True, why_not_embeddable="",
        impact_source=True, impact_precompute=True,
        classifier=True, class_member=False, er_entity=False,
        # an enum is a type, not something invoked or inherited from
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "package": KindSpec(
        color="#e76f51", glyph=_G_PACKAGE, group="Containers",
        embeddable=False,
        why_not_embeddable="shared cross-repo node, same dilution as module",
        impact_source=False, impact_precompute=True,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "repo": KindSpec(
        color="#264653", glyph=_G_REPO, group="Containers",
        embeddable=False,
        why_not_embeddable="a container sentinel; its name is an id, not prose",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "issue": KindSpec(
        color="#bc6c25", glyph=_G_ISSUE, group="Cross-source",
        embeddable=False,
        why_not_embeddable=("external reference node: the name is a tracker key, not "
                           "prose, and it is shared cross-repo"),
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "page": KindSpec(
        color="#606c38", glyph=_G_PAGE, group="Cross-source",
        embeddable=False,
        why_not_embeddable="external reference node: the name is a page id, not prose",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "design": KindSpec(
        color="#9d4edd", glyph=_G_DESIGN, group="Cross-source",
        embeddable=False,
        why_not_embeddable="external reference node: the name is a file key, not prose",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "endpoint": KindSpec(
        color="#f08c3a", glyph=_G_ENDPOINT, group="Service surfaces",
        embeddable=True, why_not_embeddable="",
        impact_source=False, impact_precompute=True,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "topic": KindSpec(
        color="#b07fd0", glyph=_G_TOPIC, group="Service surfaces",
        embeddable=False,
        why_not_embeddable="shared cross-repo node, same dilution as module",
        impact_source=False, impact_precompute=True,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "config_key": KindSpec(
        # deliberately distinct from every symbol colour: a setting is not code, and
        # reading it as one is the specific confusion this kind exists to remove.
        color="#7f8c8d", glyph=_G_CONFIG_KEY, group="Configuration",
        embeddable=False,
        why_not_embeddable=("ELIGIBLE, deferred -- not a judgement that a setting is "
                            "low signal. Adding it interacts with the per-kind embedding "
                            "budget floors and would evict existing vectors, so it is a "
                            "sequenced decision, not a refactor. Until then 'where is X "
                            "configured?' is not answerable semantically"),
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "test": KindSpec(
        color="#2a9d8f", glyph=_G_TEST, group="Symbols",
        embeddable=False,
        why_not_embeddable=("ELIGIBLE, deferred -- same budget-floor sequencing as "
                            "config_key. The kind exists precisely to give a case a real "
                            "suite+case name instead of the macro's, and that name is "
                            "what is currently never embedded"),
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        # A gtest-style TEST(Suite, Case) block is not invoked by name, so a `calls`
        # reference could never legitimately resolve here. Written down because the
        # identical gate IS a trap for a future `macro` kind, which is invoked by name.
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "namespace": KindSpec(
        color="#3d5a80", glyph=None, group="Boundary",
        embeddable=False,
        why_not_embeddable="a C4 boundary/compound parent, not a definition",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "system": KindSpec(
        # deliberately muted/neutral: unclassified, could be a real third party or just
        # an unindexed internal service, so it must never read as confidently "external".
        color="#6c757d", glyph=None, group="Boundary",
        embeddable=False,
        why_not_embeddable="a C1 external-system box, not a definition",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),

    # --- Service surfaces (flow extractors) ---
    "route": KindSpec(
        # sibling hue of `endpoint`: both are a service surface, one inbound-web.
        color="#e2711d", glyph=None, group="Service surfaces",
        embeddable=True, why_not_embeddable="",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "state": KindSpec(
        color="#5e548e", glyph=None, group="Service surfaces",
        embeddable=False,
        why_not_embeddable=("a bare state name with no signature or body; the state "
                            "diagram is how it is meant to be read"),
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),

    # --- Data model (SQL DDL). One magenta family, graded: the two real data objects,
    # then the low-signal one.
    # --- The five symbol kinds C/C++ never emitted (measured at 83,052 named symbols
    # on one large legacy tree, more than the rest of that graph put together). None is
    # embeddable YET: the measurement found the dilution risk is REPETITION, not short
    # names -- only 46.3% of data-member names are unique in that tree and one occurs 516
    # times. Turning these on interacts with the per-kind embedding budget floors, so it
    # is a sequenced decision, not a side effect of adding the kinds.
    "field": KindSpec(
        color="#8d99ae", glyph=None, group="Symbols",
        # Measured 2026-08-12 on a large legacy tree: embedding fields takes recall for
        # 40,948 previously-unfindable symbols from 0.0000 to 0.6533, and costs 2.75pp
        # of existing-kind recall@10 on top of the other four (5.25pp total). The
        # heaviest kind by far -- +105.9% vectors alone, and the least distinctive name
        # pool (54.8% unique, one name 506 times). Turned ON because a total blind spot
        # is worse than a bounded cost, but this is the row to reconsider first if the
        # cost ever bites: see testing/d8-embedding-measurement.md.
        embeddable=True, why_not_embeddable=None,
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=True, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "macro": KindSpec(
        color="#e07a5f", glyph=None, group="Symbols",
        # Measured: 0.0000 -> 0.8467 recall for 16,347 macros, at +1.50pp cost. The best
        # benefit-per-vector of the five (81.2% unique names). Include guards are ~6% and
        # remain a filtering opportunity, not a reason to withhold the rest.
        embeddable=True, why_not_embeddable=None,
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "typedef": KindSpec(
        color="#81b29a", glyph=None, group="Symbols",
        # Measured: part of the cheapest group (typedef + enum_constant +
        # global_variable) -- 0.0000 -> 0.7400 recall for 12,342 symbols at only 1.00pp
        # cost. 93.5% unique names, the most distinctive pool of the five.
        embeddable=True, why_not_embeddable=None,
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=True,
        hcl_ref_target=False, sql_ref_target=False),
    "enum_constant": KindSpec(
        color="#6d9dc5", glyph=None, group="Symbols",
        # Measured with typedef and global_variable: 0.0000 -> 0.7400 for the
        # group at 1.00pp cost. The earlier "its enum is already embedded" argument
        # did not survive the measurement -- the enum being embedded did NOT make
        # enumerators findable; their recall was exactly zero.
        embeddable=True, why_not_embeddable=None,
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=True, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "global_variable": KindSpec(
        color="#b08968", glyph=None, group="Symbols",
        # Measured with typedef and enum_constant: 0.0000 -> 0.7400 for the
        # group at 1.00pp cost. The budget floors this row was waiting on (G11) are
        # fixed, so the sequencing reason is discharged.
        embeddable=True, why_not_embeddable=None,
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "table": KindSpec(
        color="#b5179e", glyph=None, group="Data model",
        embeddable=True, why_not_embeddable="",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=True,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=True),
    "view": KindSpec(
        color="#d64ab5", glyph=None, group="Data model",
        embeddable=True, why_not_embeddable="",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=True,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=True),
    "procedure": KindSpec(
        color="#e0a3d5", glyph=None, group="Data model",
        embeddable=False,
        why_not_embeddable="low signal without a signature",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        # SQL FK references resolve to table/view defs, never to a procedure
        hcl_ref_target=False, sql_ref_target=False),

    # --- Infrastructure (HCL blocks). One indigo ramp, darkest for the two blocks that
    # carry real infrastructure, fading through the three that are plumbing.
    "resource": KindSpec(
        color="#3f37c9", glyph=None, group="Infrastructure",
        embeddable=True, why_not_embeddable="",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=True, sql_ref_target=False),
    "data": KindSpec(
        color="#5a67d8", glyph=None, group="Infrastructure",
        embeddable=False, why_not_embeddable="low-signal HCL block",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=True, sql_ref_target=False),
    "variable": KindSpec(
        color="#7c83db", glyph=None, group="Infrastructure",
        embeddable=False, why_not_embeddable="low-signal HCL block",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=True, sql_ref_target=False),
    "output": KindSpec(
        color="#9aa0e0", glyph=None, group="Infrastructure",
        embeddable=False, why_not_embeddable="low-signal HCL block",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=True, sql_ref_target=False),
    "local": KindSpec(
        color="#c3c7e8", glyph=None, group="Infrastructure",
        embeddable=False, why_not_embeddable="low-signal HCL block",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=True, sql_ref_target=False),

    # --- Documents. One green-grey family: all three are prose, differing only in where
    # the prose came from (in-repo decision record, ingested source, generated wiki).
    "adr": KindSpec(
        color="#52796f", glyph=None, group="Documents",
        # carries its full body under the `doc` attr, same as a docstring, so
        # `node_text()` picks it up with no extra wiring
        embeddable=True, why_not_embeddable="",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "document": KindSpec(
        color="#84a98c", glyph=None, group="Documents",
        embeddable=False,
        why_not_embeddable=("embedded by the *content* path instead (kb/cmds/ingest.py "
                            "_embed_documents embeds the body), so it is semantically "
                            "searchable without being in the code-symbol set"),
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "wiki": KindSpec(
        color="#354f52", glyph=None, group="Documents",
        embeddable=False,
        why_not_embeddable="embedded by the content path, same as document",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),

    # --- Cross-source (connectors). Names are keys/ids, never prose.
    "mr": KindSpec(
        # sibling hue of `issue`: both are forge items on one repo.
        color="#9c6644", glyph=None, group="Cross-source",
        embeddable=False,
        why_not_embeddable="external reference node: the name is `<repo>!<iid>`, not prose",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "message": KindSpec(
        color="#dda15e", glyph=None, group="Cross-source",
        embeddable=False,
        why_not_embeddable="external reference node: the name is a channel+timestamp key",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
    "channel": KindSpec(
        color="#a68a64", glyph=None, group="Cross-source",
        embeddable=False,
        why_not_embeddable="external reference node: the name is a channel id",
        impact_source=False, impact_precompute=False,
        classifier=False, class_member=False, er_entity=False,
        callable_target=False, inheritable_target=False,
        hcl_ref_target=False, sql_ref_target=False),
}


def kind_groups() -> list[tuple[str, list[str]]]:
    """``KIND_GROUP_ORDER`` paired with its kinds, in registry order.

    The published vocabulary diagram's taxonomy, projected rather than retyped -- its
    hand-written copy had drifted to 16 of 35 kinds while its own docstring claimed it
    "can never drift from the real output" (true of the colours it imported, false of
    the kind list it did not).
    """
    return [(g, [k for k, s in KIND_REGISTRY.items() if s.group == g])
            for g in KIND_GROUP_ORDER]
