"""XSLT support: a stylesheet's templates, and the calls and variable reads between them.

`.xsl` and `.xslt` were routed nowhere. A stylesheet is a program with a real call graph --
`<xsl:call-template name="X"/>` is a call by name -- and none of it was in the graph.

The test that carries the most weight is the last one. This module reuses `function` and
`global_variable` rather than minting kinds of its own, and the entire justification is that
the `calls` and `uses` streams filter candidates by language family. If that filter ever
stopped applying, an `xsl:template` named `format` would start resolving onto a Python
`format` with nothing reporting it, so the claim is tested rather than asserted in a comment.
"""

from __future__ import annotations

from contextlake.kb.model import Node
from contextlake.kb.parse import XSL_EXTS, RefCollector, _file_kind, is_indexable_name
from contextlake.kb.xsl import EMITTED_KINDS, parse_xsl

SHEET = b"""<?xml version="1.0"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:param name="currency"/>
  <xsl:variable name="prefix" select="concat($currency, ' ')"/>
  <!-- <xsl:template name="retired"/> -->
  <xsl:template match="/order" mode="summary">
    <xsl:variable name="local" select="'x'"/>
    <xsl:call-template name="header"/>
    <xsl:value-of select="$prefix"/>
  </xsl:template>
  <xsl:template name="header">
    <xsl:value-of select="$currency"/>
  </xsl:template>
</xsl:stylesheet>
"""


def _by_name(nodes):
    return {n.name: n for n in nodes}


def _pairs(refs):
    return {(src, target) for src, target, _rel, _line in refs}


# --- what becomes a node ---------------------------------------------------------------

def test_named_and_match_templates_are_both_extracted():
    nodes, _calls, _uses = parse_xsl("demo", "xform/order.xsl", SHEET)
    by_name = _by_name(nodes)
    assert by_name["header"].kind == "function"
    assert by_name["header"].attrs["xsl_construct"] == "template"
    # A match template has no name to be called by, so its pattern is the only handle it
    # has. A template with no handle at all cannot be pointed at from anywhere.
    assert by_name["/order"].attrs["xsl_match"] == "/order"
    assert by_name["/order"].attrs["xsl_mode"] == "summary"


def test_top_level_variables_and_params_are_nodes_and_locals_are_not():
    """`local` is declared inside a template. A large stylesheet has thousands of those,
    and none of them is reachable by name from anywhere else."""
    nodes, _calls, _uses = parse_xsl("demo", "xform/order.xsl", SHEET)
    by_name = _by_name(nodes)
    assert by_name["currency"].kind == "global_variable"
    assert by_name["prefix"].kind == "global_variable"
    assert "local" not in by_name


def test_a_commented_out_template_is_not_extracted():
    nodes, _calls, _uses = parse_xsl("demo", "xform/order.xsl", SHEET)
    assert "retired" not in _by_name(nodes)


def test_every_node_carries_the_stylesheet_language():
    """The language is not decoration here: it is what the same-language filter reads to
    keep these nodes out of every other language's name index."""
    nodes, _calls, _uses = parse_xsl("demo", "xform/order.xsl", SHEET)
    assert nodes and {n.lang for n in nodes} == {"xsl"}


# --- what becomes a reference ----------------------------------------------------------

def test_call_template_is_attributed_to_the_calling_template():
    nodes, calls, _uses = parse_xsl("demo", "xform/order.xsl", SHEET)
    caller = _by_name(nodes)["/order"].id
    assert _pairs(calls) == {(caller, "header")}


def test_a_call_after_a_template_closes_is_not_attributed_to_it():
    """The template's scope has to end at its own close tag, not merely be replaced when
    the next template opens. A first version of this suite only ever had one template
    follow another, so removing the close guard changed nothing and the test read as
    coverage it was not. The fixture was the bug, not the guard."""
    src = b"""<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
      <xsl:template name="body">
        <xsl:call-template name="inner"/>
      </xsl:template>
      <xsl:call-template name="stray"/>
    </xsl:stylesheet>"""
    nodes, calls, _uses = parse_xsl("demo", "x.xsl", src)
    body = _by_name(nodes)["body"].id
    assert _pairs(calls) == {(body, "inner")}, "the stray call leaked into the template"


def test_a_variable_read_is_attributed_to_the_template_it_sits_in():
    nodes, _calls, uses = parse_xsl("demo", "xform/order.xsl", SHEET)
    by_name = _by_name(nodes)
    assert (by_name["/order"].id, "prefix") in _pairs(uses)
    assert (by_name["header"].id, "currency") in _pairs(uses)


def test_a_top_level_variable_reading_another_is_recorded():
    """`prefix` reads `currency` outside any template. Attributing it to the declaration
    itself is what keeps it, rather than dropping it for having no enclosing template."""
    nodes, _calls, uses = parse_xsl("demo", "xform/order.xsl", SHEET)
    assert (_by_name(nodes)["prefix"].id, "currency") in _pairs(uses)


def test_a_call_outside_any_template_is_dropped_not_given_to_the_file():
    """Malformed, and the handling matters: the file node carries no language, and
    `_resolve_name_refs` reads the SOURCE node's language to apply the same-language
    filter. A file-attributed call would have that filter disabled and could resolve onto
    a same-named function in any language in the repository."""
    src = b"""<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
      <xsl:call-template name="orphan"/>
    </xsl:stylesheet>"""
    _nodes, calls, _uses = parse_xsl("demo", "x.xsl", src)
    assert calls == []


def test_two_templates_colliding_only_by_case_do_not_share_calls():
    src = b"""<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
      <xsl:template name="Row"><xsl:call-template name="first"/></xsl:template>
      <xsl:template name="row"><xsl:call-template name="second"/></xsl:template>
    </xsl:stylesheet>"""
    nodes, calls, _uses = parse_xsl("demo", "c.xsl", src)
    assert len(nodes) == 1, [n.name for n in nodes]
    assert {t for _s, t, _r, _l in calls} == {"first"}


# --- resolution ------------------------------------------------------------------------

def test_the_calls_resolve_into_a_stylesheet_call_graph():
    nodes, calls, uses = parse_xsl("demo", "xform/order.xsl", SHEET)
    by_id = {n.id: n for n in nodes}
    refs = RefCollector()
    refs.calls.extend(calls)
    refs.constant_uses.extend(uses)
    got = {(by_id[e.src].name, e.relation, by_id[e.dst].name)
           for e in refs.resolved_edges(by_id)}
    assert ("/order", "calls", "header") in got
    assert ("header", "uses", "currency") in got


def test_a_stylesheet_call_cannot_resolve_onto_a_same_named_code_function():
    """The whole reason this module mints no kinds of its own. If the language-family
    filter ever stopped applying to `calls`, this is what would break first."""
    nodes, calls, _uses = parse_xsl("demo", "xform/order.xsl", SHEET)
    decoy = Node(id="demo_util_py_function_header", repo="demo", kind="function",
                 name="header", file="util.py", line_start=1, lang="python")
    by_id = {n.id: n for n in nodes}
    refs = RefCollector()
    refs.calls.extend(calls)
    edges = refs.resolved_edges({**by_id, decoy.id: decoy})
    assert [e for e in edges if e.dst == decoy.id] == []
    # And the real target is still reached, so this is not passing by resolving nothing.
    assert any(by_id.get(e.dst) is not None and by_id[e.dst].name == "header"
               for e in edges)


# --- routing ---------------------------------------------------------------------------

def test_both_stylesheet_extensions_route_to_the_stylesheet_extractor():
    for fn, ext in (("order.xsl", ".xsl"), ("order.xslt", ".xslt")):
        assert _file_kind(fn, ext, f"xform/{fn}", allowed_exts=set(), allowed_names=set(),
                          index_hcl=True, index_sql=True) == "xsl"
        assert is_indexable_name(fn, f"xform/{fn}")
    assert XSL_EXTS == {".xsl", ".xslt"}


def test_the_kinds_this_module_emits_are_registered():
    from contextlake.kb.kinds import KIND_REGISTRY
    for kind in EMITTED_KINDS:
        assert kind in KIND_REGISTRY, f"{kind} is produced but not registered"
