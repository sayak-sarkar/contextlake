"""The generated graph page must stay inert whatever the indexed source contains.

The graph is built from arbitrary repositories contextlake has been pointed at:
symbol names, file paths, commit context, connector titles, and repo ids (``kb index``
can derive one from a bare directory name). Anyone who can land a string in an indexed
repo can therefore choose bytes that reach the page. The dashboard serves that page on
the same origin as ``/dashboard.js``, which carries the per-process mutation/LLM token,
so "inert" is a security property here, not cosmetics.

These tests drive the *sinks*, not the escaping helper: each one pushes real untrusted
data through ``to_html`` / ``_site_index`` / ``_wiki_page`` / ``build_site`` and asserts
both that

(a) the hostile construct never appears raw -- no break-out; and
(b) the value still round-trips to its original text.

(b) matters as much as (a): an escape that *mangled* the label would satisfy a
break-out-only assertion while silently corrupting every graph.
"""

import json
import re

import pytest

from contextlake.kb import visualize as viz
from contextlake.kb.security import json_for_script
from contextlake.kb.visualize.html_render import _site_index, _subst

# One hostile string per context, each closing the construct it lands in.
SCRIPT_BREAKOUT = "</script><img src=x onerror=alert(1)>"
ATTR_BREAKOUT = '"><img src=x onerror=alert(2)>'


def _island(html: str, var: str, nxt: str):
    """The parsed ``var <name> = ...;`` island -- i.e. what the browser would see.

    Parsing it (rather than substring-searching the page) is what proves the payload is
    *inert but intact*: a mangled island would not parse, and a broken-out one would
    not contain the value.
    """
    m = re.search(rf"var {var} = (.*?);\n  var {nxt}", html, re.S)
    assert m, f"the {var} island is missing from the page"
    return json.loads(m.group(1))


def _elements(html: str):
    return _island(html, "ELEMENTS", "COLORS")


def _meta(html: str):
    return _island(html, "META", "LIVE")


# --- the four script-context sinks -----------------------------------------


def test_node_label_cannot_break_out_of_the_script_block():
    html = viz.to_html({"nodes": [{"id": "a", "kind": "class",
                                   "name": f"A{SCRIPT_BREAKOUT}"}], "edges": []})
    assert SCRIPT_BREAKOUT not in html
    assert "<img src=x" not in html
    assert _elements(html)[0]["data"]["label"] == f"A{SCRIPT_BREAKOUT}"


def test_meta_cannot_break_out_of_the_script_block():
    html = viz.to_html({"nodes": [{"id": "n1", "kind": "class", "name": "ok"}],
                        "edges": [], "meta": {"mode": f"x{SCRIPT_BREAKOUT}"}})
    assert SCRIPT_BREAKOUT not in html
    assert _meta(html)["mode"] == f"x{SCRIPT_BREAKOUT}"


def test_edge_context_cannot_break_out_of_the_script_block():
    html = viz.to_html({
        "nodes": [{"id": "a", "kind": "class", "name": "A"},
                  {"id": "b", "kind": "class", "name": "B"}],
        "edges": [{"src": "a", "dst": "b", "relation": "calls",
                   "context": f"x = 1  # {SCRIPT_BREAKOUT}"}]})
    assert SCRIPT_BREAKOUT not in html
    edge = [e for e in _elements(html) if e["data"].get("relation") == "calls"][0]
    assert edge["data"]["context"] == f"x = 1  # {SCRIPT_BREAKOUT}"


def test_edge_relation_cannot_break_out_of_the_legend_attribute():
    """``relation`` is open-vocab and lands in an HTML attribute, not the script."""
    html = viz.to_html({
        "nodes": [{"id": "a", "kind": "class", "name": "A"},
                  {"id": "b", "kind": "class", "name": "B"}],
        "edges": [{"src": "a", "dst": "b", "relation": ATTR_BREAKOUT}]})
    assert ATTR_BREAKOUT not in html
    assert "<img src=x" not in html
    # escaped in place, so the page's own getAttribute() filter still sees the value
    assert 'data-rel="&quot;&gt;&lt;img src=x onerror=alert(2)&gt;"' in html


# --- the sinks that character escaping alone cannot reach ------------------


def _template_placeholders() -> list[str]:
    """Every ``__NAME__`` token the page templates define, read off the templates.

    Derived rather than listed on purpose: a placeholder added later is then covered
    without anyone remembering to extend this test, which is the same
    "close the class, not the instance" property the fix itself aims for.
    """
    from contextlake.kb.visualize import html_render as hr

    names: set[str] = set()
    for tpl in (hr._HTML_TEMPLATE, hr._WIKI_TEMPLATE, hr._INDEX_TEMPLATE):
        names |= set(re.findall(r"__[A-Z][A-Z0-9_]*__", tpl))
    assert names, "found no placeholders -- this test would silently prove nothing"
    return sorted(names)


@pytest.mark.parametrize("token", _template_placeholders())
def test_a_label_spelling_a_placeholder_is_not_expanded(token):
    """Untrusted data that merely *spells* a template placeholder must stay data.

    The old renderer chained ``str.replace`` calls over the whole document, so a label
    of ``__GLYPH__`` had the glyph markup -- quotes included -- substituted into the
    JSON island *after* the payload was serialised and escaped, terminating the string
    literal it landed in. Character escaping cannot reach this class: the injected
    characters are the template's own, and the attacker's input contains no ``<``,
    ``>`` or quote at all. It is a separate defect from the escaping sinks, with a
    separate fix, and a patch that closed only the escaping half would still fail here.
    """
    html = viz.to_html({"nodes": [{"id": "a", "kind": "class", "name": token}],
                        "edges": []})
    assert _elements(html)[0]["data"]["label"] == token


def test_a_label_containing_every_placeholder_at_once_still_parses():
    """The whole set in one label -- the worst case, and the cheapest to get wrong."""
    blob = " ".join(_template_placeholders())
    html = viz.to_html({"nodes": [{"id": "a", "kind": "class", "name": blob}],
                        "edges": []})
    assert _elements(html)[0]["data"]["label"] == blob


def test_placeholder_injection_needs_no_angle_brackets_or_quotes():
    """Guards the reasoning, not just the symptom.

    If this class is ever "fixed" by escaping harder, this test still fails: the input
    is alphanumeric and underscores only, so no escaper can see it coming.
    """
    token = "__GLYPH__"
    assert not set(token) & set("<>\"'&")
    html = viz.to_html({"nodes": [{"id": "a", "kind": "class", "name": token}],
                        "edges": []})
    assert _elements(html)[0]["data"]["label"] == token


def test_page_title_is_escaped():
    """``build_site`` and the dashboard pass a repo id through ``title``."""
    html = viz.to_html({"nodes": [], "edges": []},
                       title=f"contextlake - {SCRIPT_BREAKOUT}")
    assert SCRIPT_BREAKOUT not in html
    assert "</title><img" not in html


def test_repo_id_is_escaped_in_the_site_index():
    """A repo id reaches the index as link text, inside an href, and as a heading."""
    hostile = f"ns{ATTR_BREAKOUT}/app"
    html = _site_index([hostile], {hostile: 1}, {hostile: f"repo-{ATTR_BREAKOUT}.html"})
    assert ATTR_BREAKOUT not in html
    assert "<img src=x" not in html


def test_subst_leaves_an_unknown_placeholder_verbatim():
    """An unrecognised ``__NAME__`` is data, not an error and not a blank."""
    assert _subst("a __KNOWN__ b __MYSTERY__ c", {"__KNOWN__": "1"}) == \
        "a 1 b __MYSTERY__ c"


# --- the shared helper, and the second call site it now covers -------------


def test_json_for_script_is_inert_but_lossless():
    payload = {"k": f"{SCRIPT_BREAKOUT} & <!-- x -->"}
    out = json_for_script(payload)
    for raw in ("<", ">", "&"):
        assert raw not in out
    assert json.loads(out) == payload


def test_static_site_data_js_uses_the_same_defence(tmp_path):
    """``dashboard/site.py`` writes the same kind of payload into a script context."""
    from contextlake.kb.dashboard.site import build_dashboard_site

    out = build_dashboard_site(tmp_path / "store", tmp_path / "site", sample=True)
    data_js = (out / "data.js").read_text(encoding="utf-8")
    assert "<" not in data_js and ">" not in data_js
    prefix = "window.__CONTEXTLAKE__ = "
    assert data_js.startswith(prefix)
    json.loads(data_js[len(prefix):].rstrip().rstrip(";"))


def test_wiki_page_staleness_badge_is_escaped():
    """The badge's commit id is regex-scraped out of LLM-derived wiki Markdown."""
    from contextlake.kb.model import Repo
    from contextlake.kb.visualize.html_render import _wiki_page

    hostile = f"{ATTR_BREAKOUT}deadbeef"

    class _Store:            # the one method _wiki_page calls
        def get_repo(self, repo_id):
            return Repo(id=repo_id, path="/tmp/x", head_commit=hostile)

    html = _wiki_page("team/app", f"# W\n\nat commit `{hostile}`\n", _Store())
    assert ATTR_BREAKOUT not in html
    assert "<img src=x" not in html
