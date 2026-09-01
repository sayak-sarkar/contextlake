"""Self-contained offline HTML rendering: the cytoscape.js page, wiki pages, the site index."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..kinds import kind_groups
from ..security import json_for_script
from .diagrams import _cytoscape_elements
from .payload import overview_subgraph, repo_node_sizes, repo_subgraph, to_payload
from .styling import (
    _CONF_DOT,
    _GLYPH_SVG,
    _LANG_LABELS,
    CONF_META,
    DEFAULT_COLOR,
    DEFAULT_EDGE_COLOR,
    DEFAULT_EDGE_COLOR_DARK,
    FOUND_COLOR,
    FOUND_COLOR_DARK,
    HILITE_COLOR,
    HILITE_COLOR_DARK,
    KIND_COLORS,
    NODE_BORDER_COLOR,
    NODE_BORDER_COLOR_DARK,
    NS_COLOR,
    NS_COLOR_DARK,
    RELATION_COLORS,
    RELATION_COLORS_DARK,
    SCAFFOLD_EDGE_COLOR,
    SCAFFOLD_EDGE_COLOR_DARK,
    _kind_icons,
    _lang_icons,
)

if TYPE_CHECKING:  # avoid importing the model at call time; we only need types here
    from ..store.base import Store

_CDN_URL = "https://cdn.jsdelivr.net/npm/cytoscape@3.34.0/dist/cytoscape.min.js"
# Layout/rendering extensions behind the opt-in "dagre (preview)" layout. Both are
# small next to cytoscape itself (~46 KB + ~11 KB), and cytoscape-dagre bundles dagre,
# so there is no separate dagre file to vendor.
_EXT_CDN_URLS = {
    "cytoscape-dagre.min.js":
        "https://cdn.jsdelivr.net/npm/cytoscape-dagre@4.0.0/dist/cytoscape-dagre.min.js",
    "cytoscape-dom-node.js":
        "https://cdn.jsdelivr.net/npm/cytoscape-dom-node@2.1.0/dist/index.global.js",
}
# every JS asset the page loads, in load order (cytoscape must come first: the
# dom-node global build self-registers against window.cytoscape on load)
_LIB_FILES = ("cytoscape.min.js", *_EXT_CDN_URLS)

# contextlake brand palette (see BRANDING.md): a lake seen in cross-section.


def _static_js(name: str) -> str:
    """A vendored static JS file's text, made safe to inline in a <script>."""
    from importlib.resources import files
    js = (files("contextlake.kb") / "static" / name).read_text(encoding="utf-8")
    return js.replace("</script", "<\\/script")


def _cytoscape_js() -> str:
    """The vendored cytoscape.min.js text, made safe to inline in a <script>."""
    return _static_js("cytoscape.min.js")


def _app_css() -> str:
    """The visualizer's stylesheet (extracted to static/app.css, inlined at emit)."""
    from importlib.resources import files
    return (files("contextlake.kb") / "static" / "app.css").read_text(encoding="utf-8")


def _app_js() -> str:
    """The visualizer's app JS (static/app.js), made safe to inline in a <script>."""
    from importlib.resources import files
    js = (files("contextlake.kb") / "static" / "app.js").read_text(encoding="utf-8")
    return js.replace("</script", "<\\/script")


_PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")


def _subst(template: str, mapping: dict[str, str]) -> str:
    """Fill ``__NAME__`` placeholders in ONE pass, so inserted text is never rescanned.

    A chain of ``str.replace`` calls re-scans everything the earlier calls inserted,
    so untrusted data that merely *spells* a later placeholder gets expanded after
    the fact: a symbol named ``__GLYPH__`` pulls the glyph markup -- which contains
    quotes -- into the middle of the JSON island and terminates the string literal it
    landed in. No amount of character escaping reaches that, because the injected
    characters are the *template's* own, added after the payload was escaped.

    A single left-to-right pass cannot do it: replacement text is output, never input
    again. An unknown ``__NAME__`` is left verbatim rather than blanked, so a literal
    token in someone's source still renders as itself.
    """
    return _PLACEHOLDER_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), template)


LAYOUTS = ("cose", "concentric", "breadthfirst", "circle", "grid", "dagre")
# "dagre" is an opt-in *preview* of a different look: a layered dagre layout whose
# nodes are drawn as real HTML cards (cytoscape-dom-node) instead of canvas circles.
# It is last in the list and never the default — picking any other layout leaves the
# existing canvas rendering exactly as it was.
_LAYOUT_LABELS = {"dagre": "dagre (preview)"}


def to_html(payload: dict, *, cdn: bool = False, live: bool = False,
            layout: str = "cose", title: str = "contextlake graph",
            assets: str = "inline", site: bool = False) -> str:
    """A single self-contained HTML page rendering the subgraph with cytoscape.js.

    Default inlines the vendored libs + CSS/JS so the file works offline / air-gapped;
    pass ``cdn=True`` for a small online-only file. ``assets="sibling"`` references
    the vendored JS (``cytoscape.min.js`` + the two layout/render extensions) and
    ``app.css`` / ``app.js`` as relative files instead of inlining them — used by ``build_site`` so a folder of cross-linked pages shares
    one copy of each asset rather than inlining ~1 MB per page. ``site=True`` enables
    cross-page navigation (overview repo nodes carry an ``href`` to their repo page).
    ``live=True`` wires node taps to a ``/neighbors`` endpoint (used by ``serve_graph``).
    """
    if cdn:
        urls = (_CDN_URL, *_EXT_CDN_URLS.values())
        lib_tag = "\n".join(f'<script src="{u}"></script>' for u in urls)
    elif assets == "sibling":
        lib_tag = "\n".join(f'<script src="{n}"></script>' for n in _LIB_FILES)
    else:
        lib_tag = "\n".join(f"<script>{_static_js(n)}</script>" for n in _LIB_FILES)
    if assets == "sibling":
        style_block = '<link rel="stylesheet" href="app.css">'
        app_js_block = '</script>\n<script src="app.js"></script>'
    else:
        style_block = f"<style>{_app_css()}</style>"
        app_js_block = f"  {_app_js()}</script>"
    from collections import Counter
    elements = json_for_script(_cytoscape_elements(payload))
    colors = json_for_script(KIND_COLORS)
    icon_map = _kind_icons()
    lang_icon_map = _lang_icons()
    icons = json_for_script(icon_map)
    lang_icons = json_for_script(lang_icon_map)
    kind_counts = Counter(n.get("kind", "") for n in payload["nodes"])

    def _kind_swatch(k: str, c: str) -> str:
        # Reuse the very data-URI glyph the node paints (zero extra payload, one
        # source of truth). The glyph stroke is contrast-picked for the node FILL, so
        # render it on a fill-coloured swatch — exactly mirroring the node on canvas.
        icon = icon_map.get(k)
        if icon:
            return f'<span class="gl" style="background:{c}"><img src="{icon}" alt=""></span>'
        return f'<i style="background:{c}"></i>'   # open-vocab kind with no glyph

    # `k` and `r` below are escaped for the HTML attribute and text contexts they
    # land in. `k` is a static KIND_COLORS key today and `r` is open-vocab relation
    # text straight off the graph -- both are escaped anyway, because "safe because
    # of where today's value happens to come from" is exactly the assumption this fix
    # exists to remove. Escaping is transparent to the page's own JS: the filter
    # reads these back with getAttribute(), which returns the decoded value.
    # aria-pressed="true" == "this kind is currently SHOWN", which is the state the page
    # boots in (nothing is filtered out yet). syncLegend() writes the same polarity, so
    # the server-rendered value and the live one can never disagree. Without this the
    # only cue that a whole node kind is hidden is a CSS class.
    def _lg(k: str) -> str:
        return (f'<button type="button" class="lg" aria-pressed="true" '
                f'data-kind="{html.escape(k, quote=True)}">'
                f'{_kind_swatch(k, KIND_COLORS[k])}'
                f'<span class="lbl">{html.escape(k)}</span>'
                f'<span class="cnt">{kind_counts[k]}</span></button>')

    # The legend is grouped by the registry's ten bands instead of one flat run of
    # 52 pills. `kind_groups()` projects KIND_REGISTRY over KIND_GROUP_ORDER, and
    # KIND_COLORS is itself a projection of the same registry -- so the two agree at
    # 52/52 today. The band list is still UNIONED with the flat source rather than
    # replacing it: a kind whose `group` fell outside KIND_GROUP_ORDER would render
    # today and silently vanish under a straight swap, and this registry has drifted
    # before (see kind_groups()'s own docstring). Anything ungrouped lands in a
    # trailing "Other" band where it is visible, not lost.
    present = [k for k in KIND_COLORS if kind_counts.get(k, 0) > 0]
    banded, seen_kinds = [], set()
    for group, kinds in kind_groups():
        members = [k for k in kinds if k in present]
        seen_kinds.update(members)
        # An empty band must not render a bare heading. Bands go empty routinely:
        # kind_counts is built from payload["nodes"], which is already post-fold, so
        # a view that folds its leaf kinds away drops whole bands with it.
        if members:
            banded.append((group, members))
    leftover = [k for k in present if k not in seen_kinds]
    if leftover:
        banded.append(("Other", leftover))

    legend = "".join(
        f'<div class="band"><h3 class="bandname">{html.escape(g)}</h3>'
        f'<div class="bandrow">{"".join(_lg(k) for k in ks)}</div></div>'
        for g, ks in banded)
    # edge legend = relations actually present (known hues first, then open-vocab)
    rel_counts = Counter(e.get("relation") for e in payload["edges"])
    present = {r for r in rel_counts if r}
    known = [r for r in RELATION_COLORS if r in present]
    rel_order = known + sorted(present - set(RELATION_COLORS))
    edge_legend = "".join(
        f'<button type="button" class="lg rel" aria-pressed="true" '
        f'data-rel="{html.escape(r, quote=True)}">'
        f'<i style="background:{RELATION_COLORS.get(r, DEFAULT_EDGE_COLOR)}"></i>'
        f'<span class="lbl">{html.escape(r)}</span>'
        f'<span class="cnt">{rel_counts[r]}</span></button>'
        for r in rel_order)
    # Legend key (collapsible): line-style = edge confidence; lettermark = repo
    # language. Both filtered to what is actually present so the key never lies.
    conf_present = [cf for cf in _CONF_DOT
                    if cf in {e.get("confidence") for e in payload["edges"]}]
    conf_key = "".join(
        f'<div class="ck"><span class="ln {_CONF_DOT[cf]}"></span>'
        f'<span class="lbl">{CONF_META[cf][0]}</span>'
        f'<span class="cnt">{CONF_META[cf][2]}</span></div>'
        for cf in conf_present)
    repo_langs = {n.get("lang") for n in payload["nodes"] if n.get("kind") == "repo"}
    langs_present = [lg for lg in _LANG_LABELS if lg in repo_langs]
    repo_fill = KIND_COLORS.get("repo", DEFAULT_COLOR)
    lang_key = "".join(
        f'<div class="ck"><span class="gl" style="background:{repo_fill}">'
        f'<img src="{lang_icon_map[lg]}" alt=""></span>'
        f'<span class="lbl">{_LANG_LABELS[lg]}</span></div>'
        for lg in langs_present)
    keys_inner = ""
    if conf_key:
        keys_inner += f'<div class="kgroup"><h3>Confidence</h3>{conf_key}</div>'
    if lang_key:
        keys_inner += f'<div class="kgroup"><h3>Languages</h3>{lang_key}</div>'
    legend_keys = (f'<details class="legend-keys"><summary>Legend key</summary>'
                   f'{keys_inner}</details>') if keys_inner else ""
    options = "".join(f'<option value="{n}">{_LAYOUT_LABELS.get(n, n)}</option>'
                      for n in LAYOUTS)
    meta = json_for_script(payload.get("meta", {}))
    # `title` reaches <title> as text and carries a repo id on the build_site /
    # dashboard paths (`kb index` can derive an id from a bare directory name), so it
    # is untrusted like everything else here.
    return _subst(_HTML_TEMPLATE, {
        "__STYLE_BLOCK__": style_block,
        "__APP_JS_BLOCK__": app_js_block,
        "__TITLE__": html.escape(title),
        "__SITE__": "true" if site else "false",
        "__LIB_TAG__": lib_tag,
        "__ELEMENTS__": elements,
        "__COLORS__": colors,
        "__ICONS__": icons,
        "__LANG_ICONS__": lang_icons,
        "__DEFAULT_COLOR__": DEFAULT_COLOR,
        "__REL_COLORS__": json_for_script(RELATION_COLORS),
        "__REL_COLORS_DARK__": json_for_script(RELATION_COLORS_DARK),
        "__DEFAULT_EDGE_COLOR__": DEFAULT_EDGE_COLOR,
        "__DEFAULT_EDGE_COLOR_DARK__": DEFAULT_EDGE_COLOR_DARK,
        "__EDGE_INK__": json_for_script({
            "light": {"scaffold": SCAFFOLD_EDGE_COLOR, "node": NODE_BORDER_COLOR,
                      "hi": HILITE_COLOR, "found": FOUND_COLOR, "ns": NS_COLOR},
            "dark": {"scaffold": SCAFFOLD_EDGE_COLOR_DARK, "node": NODE_BORDER_COLOR_DARK,
                     "hi": HILITE_COLOR_DARK, "found": FOUND_COLOR_DARK,
                     "ns": NS_COLOR_DARK},
        }),
        "__CONF_META__": json_for_script(CONF_META),
        "__LEGEND__": legend,
        "__EDGE_LEGEND__": edge_legend,
        "__LEGEND_KEYS__": legend_keys,
        "__LAYOUT_OPTIONS__": options,
        "__GLYPH__": _GLYPH_SVG,
        "__META__": meta,
        "__LAYOUT__": layout if layout in LAYOUTS else "cose",
        "__LIVE__": "true" if live else "false",
    })


# ---------------------------------------------------------------------------
# Static cross-linked site
# ---------------------------------------------------------------------------


def repo_slug(repo_id: str) -> str:
    """Filesystem-safe page name for a repo id (matches the wiki convention)."""
    return repo_id.replace("/", "__")


def _read_static_raw(name: str) -> str:
    from importlib.resources import files
    return (files("contextlake.kb") / "static" / name).read_text(encoding="utf-8")


def _slugify(text: str) -> str:
    """A stable, URL-safe anchor for one heading's text.

    Stability is the whole point: a link into a wiki page has to survive the page being
    regenerated, so this maps the words rather than the position. Markdown punctuation is
    stripped first -- a heading written ``## `parse.py` internals`` should anchor on
    ``parse-py-internals``, not on the backticks.

    Non-ASCII is kept. `id` allows it, browsers resolve it, and transliterating a heading
    written in another language would silently produce an anchor its author cannot predict.
    A heading with no word characters at all (an emoji, a rule) falls back to ``section``,
    which the caller's counter then makes unique.
    """
    import re as _re
    import unicodedata

    t = unicodedata.normalize("NFKC", text or "")
    t = _re.sub(r"`([^`]*)`", r"\1", t)          # inline code
    t = _re.sub(r"\*\*?([^*]*)\*?\*", r"\1", t)   # bold / italic
    t = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)  # links -> their text
    t = t.strip().lower()
    # Dots, slashes and colons SEPARATE, they do not vanish: `parse.py` must anchor on
    # `parse-py`, not `parsepy`. Filenames and dotted names are the common case in these
    # headings, and eliding the separator runs the words together into something a reader
    # would not guess.
    t = _re.sub(r"[./:]+", "-", t)
    t = _re.sub(r"[^\w\s-]", "", t, flags=_re.UNICODE)
    t = _re.sub(r"[\s_-]+", "-", t).strip("-")
    return t or "section"


def _md_to_html(md: str) -> str:
    """A tiny, dependency-free Markdown -> HTML renderer for wiki prose.

    Handles headings, fenced code, unordered lists, paragraphs, and inline
    code/bold/italic/links — enough for generated wiki pages. HTML is escaped
    *first* (the wiki is LLM-derived from repo content, so it's untrusted), then
    the Markdown punctuation that survives escaping is transformed.
    """
    import re as _re

    def esc(s: str) -> str:
        # Escape quotes too: rendered text is interpolated into href="…" attributes
        # below, and the wiki is untrusted (LLM-derived from repo content) — without
        # this, a crafted link URL could break out of the attribute (stored XSS).
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#39;"))

    def inline(s: str) -> str:
        s = esc(s)
        s = _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", s)
        # URL class excludes quotes/brackets/whitespace so it can't escape the
        # attribute even if escaping above ever regressed (defense in depth).
        s = _re.sub(r"\[([^\]]+)\]\((https?://[^)\s\"'<>]+)\)",
                    r'<a href="\2" rel="noopener noreferrer">\1</a>', s)
        return s

    out: list[str] = []
    para: list[str] = []
    in_list = False
    lines = md.split("\n")
    i = 0
    # Heading ids, and the counter that keeps them unique. Two headings in one document
    # may legitimately read the same ("Overview" under two subsystems), and duplicate ids
    # are a WCAG 4.1.1 failure AND make any link to them land non-deterministically. This
    # function also renders arbitrary repository READMEs (dashboard/data.py), not only
    # generated wiki pages, so the collision is ordinary rather than hypothetical.
    seen_ids: dict[str, int] = {}

    def heading_id(text: str) -> str:
        slug = _slugify(text)
        n = seen_ids.get(slug, 0) + 1
        seen_ids[slug] = n
        return slug if n == 1 else f"{slug}-{n}"

    def flush_para():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        h = _re.match(r"(#{1,4})\s+(.*)", line)
        if h:
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            lvl = len(h.group(1))
            # The id comes from the RAW heading text, not from `inline()`'s output: that
            # output is HTML, and slugifying it would fold tags and entities into the
            # anchor. A reader linking to a section should get the words they saw.
            out.append(f'<h{lvl} id="{heading_id(h.group(2))}">'
                       f"{inline(h.group(2))}</h{lvl}>")
            i += 1
            continue
        li = _re.match(r"\s*[-*]\s+(.*)", line)
        if li:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline(li.group(1)) + "</li>")
            i += 1
            continue
        if not line.strip():
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            i += 1
            continue
        para.append(line.strip())
        i += 1
    flush_para()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


_WIKI_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>contextlake: __REPO__ wiki</title>
<style>
  :root{--lake:#137A8B;--bg:#f5fafb;--surface:#fff;--line:#dce8ea;--text:#0E2A33;
    --muted:#5b7177;--sun:#E7B53C;--ff:"Inter",system-ui,-apple-system,Segoe UI,sans-serif;
    --ff-d:"Space Grotesk",var(--ff)}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--ff);line-height:1.6}
  header{display:flex;align-items:center;gap:10px;padding:14px 28px;background:var(--surface);
    border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
  .wm{font-family:var(--ff-d);font-weight:600}.wm .l{color:var(--lake)}
  header .repo{color:var(--muted);font-size:14px}
  header a{margin-left:auto;color:var(--lake);text-decoration:none;font-size:14px}
  header a:hover{text-decoration:underline}
  .badge{font-size:12px;padding:3px 10px;border-radius:999px;font-weight:600}
  .badge.fresh{background:#e6f6f1;color:#0f6473}
  .badge.stale{background:#fbf0d6;color:#7a5b16}
  main{max-width:820px;margin:0 auto;padding:8px 28px 64px}
  .advisory{font-size:13px;color:var(--muted);border-left:3px solid var(--sun);
    padding:8px 12px;margin:18px 0;background:var(--surface);border-radius:6px}
  h1,h2,h3{font-family:var(--ff-d);line-height:1.25;margin:1.4em 0 .5em}
  h1{font-size:24px}h2{font-size:19px}h3{font-size:16px}
  code{background:#eef6f7;padding:1px 5px;border-radius:4px;font-size:.92em}
  pre{background:var(--surface);border:1px solid var(--line);border-radius:10px;
    padding:14px;overflow:auto}pre code{background:none;padding:0}
  a{color:var(--lake)} ul{padding-left:22px}
</style></head>
<body>
  <header>__GLYPH__<span class="wm">context<span class="l">lake</span></span>
    <span class="repo">__REPO__</span><span class="badge __STALECLASS__">__STALE__</span>
    <a href="repo-__SLUG__.html">graph →</a></header>
  <main>
    <div class="advisory">Advisory: this page is LLM-synthesised from the knowledge graph.
      Verify against the cited sources; it never outranks extracted facts.</div>
    __BODY__
  </main>
</body></html>
"""


_INDEX_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>contextlake: index</title>
<style>
  :root{--deepwater:#0E2A33;--lake:#137A8B;--current:#2BB3A3;--bg:#f5fafb;
    --surface:#fff;--line:#dce8ea;--text:#0E2A33;--muted:#5b7177;--subtle:#8aa2a6;
    --ff:"Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    --ff-d:"Space Grotesk",var(--ff)}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--ff);
    line-height:1.5}
  header{display:flex;align-items:center;gap:10px;padding:16px 28px;background:var(--surface);
    border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
  .glyph{width:30px;height:30px;border-radius:8px;display:block;
    box-shadow:0 1px 2px rgba(14,42,51,.1)}
  .wm{font-family:var(--ff-d);font-size:19px;font-weight:600;letter-spacing:-.01em}
  .wm .l{color:var(--lake)}
  header .sub{color:var(--muted);font-size:13px;margin-left:4px}
  header a{margin-left:auto;color:var(--lake);text-decoration:none;font-size:14px;font-weight:500}
  header a:hover{text-decoration:underline}
  main{max-width:1100px;margin:0 auto;padding:24px 28px 60px;
    columns:320px;column-gap:24px}
  section{break-inside:avoid;margin:0 0 20px;background:var(--surface);
    border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  h2{font-family:var(--ff-d);font-size:14px;margin:0 0 10px;display:flex;
    align-items:center;gap:8px;text-transform:none}
  h2 .c{margin-left:auto;color:var(--subtle);font-size:12px;font-weight:500;
    font-variant-numeric:tabular-nums}
  ul{list-style:none;margin:0;padding:0}
  li{display:flex;align-items:baseline;gap:8px;padding:4px 0;border-top:1px solid var(--line);
    font-size:13px}
  li:first-of-type{border-top:0}
  li a{color:var(--lake);text-decoration:none;font-weight:500;flex:none}
  li a.wk{font-size:11px;color:var(--muted);font-weight:400;border:1px solid var(--line);
    border-radius:999px;padding:0 7px}
  li a.wk:hover{color:var(--lake);border-color:var(--lake)}
  li a:hover{text-decoration:underline}
  li .p{color:var(--subtle);font-size:11px;overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;flex:1}
  li .c{color:var(--subtle);font-size:11px;font-variant-numeric:tabular-nums;flex:none}
</style></head>
<body>
  <header>__GLYPH__
    <span class="wm">context<span class="l">lake</span></span>
    <span class="sub">__N__ repos with a parsed graph</span>
    <a href="overview.html">Fleet overview →</a></header>
  <main>__BODY__</main>
</body></html>
"""


def _wiki_page(repo: str, md: str, store: Store) -> str:
    """Render a repo's wiki Markdown into a standalone HTML page with a staleness badge."""
    import re as _re
    m = _re.search(r"at commit `([^`]+)`", md)
    wiki_commit = m.group(1) if m else None
    r = store.get_repo(repo)
    current = r.head_commit if r else None
    stale = wiki_commit is None or current is None or wiki_commit != current
    repo_esc = html.escape(repo, quote=True)
    # `wiki_commit` is regex-scraped out of the wiki Markdown, which is LLM-derived
    # from repo content -- so the badge is untrusted text, not a known-hex commit id.
    badge = ("stale · regenerate" if stale
             else "fresh · " + html.escape((wiki_commit or "")[:8]))
    return _subst(_WIKI_TEMPLATE, {
        "__GLYPH__": _GLYPH_SVG,
        "__REPO__": repo_esc,
        "__SLUG__": html.escape(repo_slug(repo), quote=True),
        "__STALECLASS__": "stale" if stale else "fresh",
        "__STALE__": badge,
        "__BODY__": _md_to_html(md),
    })


def _site_index(repos: list[str], sizes: dict, pages: dict, wiki: dict | None = None) -> str:
    from collections import defaultdict
    wiki = wiki or {}
    groups: dict[str, list[str]] = defaultdict(list)
    for r in repos:
        groups[r.split("/")[0]].append(r)
    sections = []
    # A repo id reaches this page three ways: as link text, inside an href, and as a
    # heading. `kb index` can derive an id from a bare directory name, so it is
    # untrusted text rather than a constrained namespace token.
    for ns in sorted(groups):
        items = "".join(
            f'<li><a href="{html.escape(pages[r], quote=True)}">'
            f'{html.escape(r.rsplit("/", 1)[-1])}</a>'
            + (f'<a class="wk" href="{html.escape(wiki[r], quote=True)}">wiki</a>'
               if r in wiki else "")
            + f'<span class="p">{html.escape(r)}</span>'
            + f'<span class="c">{sizes.get(r, 0)}</span></li>'
            for r in sorted(groups[ns]))
        sections.append(
            f'<section><h2>{html.escape(ns)}<span class="c">{len(groups[ns])}</span></h2>'
            f"<ul>{items}</ul></section>")
    return _subst(_INDEX_TEMPLATE, {
        "__GLYPH__": _GLYPH_SVG,
        "__N__": str(len(repos)),
        "__BODY__": "\n".join(sections),
    })


def _match_repo(repo_id: str, patterns: list[str]) -> bool:
    """A repo matches if any pattern is a glob hit or a plain substring of its id."""
    from fnmatch import fnmatch
    return any(fnmatch(repo_id, p) or p in repo_id for p in patterns)


def build_site(store: Store, out_dir, *, max_nodes: int = 5000,
               repo_max_nodes: int = 500, overview_layout: str = "concentric",
               repo_layout: str = "cose", repos: list[str] | None = None,
               cdn: bool = False, log=lambda _m: None) -> Path:
    """Emit a folder of cross-linked, offline HTML pages sharing one set of assets.

    Writes ``index.html`` + ``overview.html`` + one ``repo-<slug>.html`` per repo
    that has parsed nodes, plus a single shared copy of each vendored JS lib +
    ``app.css`` / ``app.js`` (referenced, not inlined — so the folder stays small instead of
    repeating ~1 MB per page). Overview repo nodes link to their repo page; every
    page links back to the overview + index. Fully offline.

    ``repos`` is an optional list of filter patterns (glob or substring against the
    repo id); when given, only matching repos get per-repo pages (and overview links
    to them) — the fleet overview itself still shows every repo. This keeps a scoped
    build small instead of materialising a page for all ~hundreds of repos.

    ``cdn=True`` points every page's cytoscape ``<script src>`` at the CDN and skips
    vendoring the three JS libs, for a thin build on a bandwidth-constrained host.
    It is opt-in and off by default because it costs the export its offline
    guarantee. ``app.css``/``app.js`` are contextlake's own and stay local either
    way -- they are not on any CDN.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    assets = ("app.css", "app.js") if cdn else (*_LIB_FILES, "app.css", "app.js")
    for name in assets:
        (out / name).write_text(_read_static_raw(name), encoding="utf-8")

    sizes = repo_node_sizes(store)
    repos_with_nodes = sorted(r for r, c in sizes.items() if c)
    if repos:
        repos_with_nodes = [r for r in repos_with_nodes if _match_repo(r, repos)]
    pages = {r: f"repo-{repo_slug(r)}.html" for r in repos_with_nodes}

    # wiki pages, if the LLM-wiki has been generated (store_dir/wiki/<slug>.md).
    # Discovered in full BEFORE any page is written, not while writing them: every
    # page carries the whole map so a node's inspector can offer its repo's wiki,
    # and a node on the overview (or a cross-repo node on a repo page) belongs to a
    # repo whose page has not been reached yet. Building the map inside the write
    # loop meant each page saw only the repos written before it, so the same node
    # got the affordance on one page and not on another. Statting first is the fix.
    sp = getattr(store, "path", None)
    wiki_dir = (Path(sp).parent / "wiki") if sp else None
    wiki_srcs: dict[str, Path] = {}
    wiki_pages: dict[str, str] = {}
    if wiki_dir:
        for r in repos_with_nodes:
            wf = wiki_dir / (repo_slug(r) + ".md")
            if wf.exists():
                wiki_srcs[r] = wf
                wiki_pages[r] = f"wiki-{repo_slug(r)}.html"

    meta: dict = {}
    nodes, edges = overview_subgraph(store, max_nodes=max_nodes, meta=meta)
    for n in nodes:
        if n["id"] in pages:
            n["href"] = pages[n["id"]]
    meta["mode"] = "overview"
    meta["wiki"] = wiki_pages
    (out / "overview.html").write_text(
        to_html(to_payload(nodes, edges, meta, fold_leaves=True), cdn=cdn,
                layout=overview_layout, assets="sibling", site=True,
                title="contextlake — fleet overview"), encoding="utf-8")

    for r in repos_with_nodes:
        m: dict = {}
        rn, re_ = repo_subgraph(store, r, max_nodes=repo_max_nodes, meta=m)
        m.update(mode="repo", repo=r, wiki=wiki_pages)
        (out / pages[r]).write_text(
            to_html(to_payload(rn, re_, m, fold_leaves=True), cdn=cdn,
                    layout=repo_layout, assets="sibling", site=True,
                    title=f"contextlake — {r}"), encoding="utf-8")
        if r in wiki_srcs:
            (out / wiki_pages[r]).write_text(
                _wiki_page(r, wiki_srcs[r].read_text(encoding="utf-8", errors="replace"), store),
                encoding="utf-8")
    log(f"  wrote overview + {len(repos_with_nodes)} repo pages "
        f"+ {len(wiki_pages)} wiki pages + index")

    (out / "index.html").write_text(
        _site_index(repos_with_nodes, sizes, pages, wiki_pages), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Live server (click-to-expand)
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
__STYLE_BLOCK__
__LIB_TAG__
</head>
<body data-theme="light" data-sidebar="open" data-inspect="closed">
<a class="cl-skip" href="#textview">Skip to the graph as text</a>
<h1 class="cl-sr">__TITLE__</h1>
<div id="app">
  <header id="topbar">
    <button class="ibtn" id="navToggle" title="Toggle sidebar" aria-label="Toggle sidebar"><svg
      viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path
      d="M2 4h12M2 8h12M2 12h12"/></svg></button>
    __GLYPH__
    <span class="wm">context<span class="l">lake</span></span>
    <span id="mode"></span>
    <span class="grow"></span>
    <div class="tsearch"><svg class="si" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      stroke-width="1.6"><circle cx="7" cy="7" r="4.5"/><path d="M11 11l3 3"/></svg>
      <input id="search" type="search" placeholder="Search nodes…" autocomplete="off"
        aria-label="Search nodes"></div>
    <button class="ibtn" id="theme" title="Toggle dark mode" aria-label="Toggle dark mode"><svg
      viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8"
      r="3.2"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6L13 13M13 3l-1.4 1.4M4.4
      11.6L3 13"/></svg></button>
  </header>
  <aside id="panel" role="complementary" aria-label="Controls">
    <div class="sgroup"><h2>View</h2>
      <div class="row" id="viewmodes" hidden>
        <!-- Not a tablist: there is no tabpanel, no aria-controls and no arrow-key
             model, so role="tab" announced a structure the page does not have. These
             are two mutually exclusive toggle buttons and aria-pressed says exactly
             that (kept in sync by setMode). -->
        <div class="seg" role="group" aria-label="Overview mode">
          <button class="segbtn on" id="vm-clusters" aria-pressed="true"
            title="Namespace clusters — the repo tree, drill in on click">Namespace</button>
          <button class="segbtn" id="vm-flow" aria-pressed="false"
            title="Dependency clusters — connected repos grouped by what they depend on"
            >Dependencies</button>
        </div>
      </div>
      <div class="row">
        <label>layout <select id="layout" aria-label="Layout">__LAYOUT_OPTIONS__</select></label>
        <button class="ibtn" id="zoomout" title="Zoom out" aria-label="Zoom out"><svg
          viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle
          cx="7" cy="7" r="4.5"/><path d="M4.5 7h5M10.5 10.5 14 14"/></svg></button>
        <button class="ibtn" id="zoomin" title="Zoom in" aria-label="Zoom in"><svg
          viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle
          cx="7" cy="7" r="4.5"/><path d="M4.5 7h5M7 4.5v5M10.5 10.5 14 14"/></svg></button>
        <button class="ibtn" id="fit" title="Fit to view" aria-label="Fit to view"><svg
          viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path
          d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"/></svg></button>
        <button class="ibtn" id="reset" title="Reset view &amp; filters" aria-label="Reset"><svg
          viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path
          d="M13 8a5 5 0 1 1-1.5-3.5M13 2v3h-3"/></svg></button>
        <button class="btn primary" id="png" title="Save a PNG snapshot"><svg viewBox="0 0 16 16"
          fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2v8M5 7l3 3 3-3M3
          13h10"/></svg>PNG</button>
        <button class="btn" id="svg" title="Save an SVG snapshot — vector, and the only format that keeps the dagre preview's HTML cards"><svg viewBox="0 0 16 16" fill="none"
          stroke="currentColor" stroke-width="1.5"><path d="M8 2v8M5 7l3 3 3-3M3
          13h10"/></svg>SVG</button>
      </div>
      <!-- These two govern the NEXT click-to-expand, not the current canvas: expanding
           is additive, so retroactively narrowing an already-explored graph would
           delete nodes the reader put there on purpose. Both are sent to /neighbors,
           which does the traversal -- depth is not narrowed in the browser, because
           the direction case provably loses nodes when it is (max_fanout is applied
           to the both-direction neighbour list, so in-edges crowd out the out-edges a
           directed view needs, and no later hop recovers them). -->
      <div class="row" role="group" aria-label="Expand behaviour">
        <label>depth <input type="range" id="hops" min="1" max="3" step="1" value="1"
          aria-describedby="hopshelp"><span class="cnt" id="hopsv">1</span></label>
        <label>edges <select id="direction" aria-label="Which edges to follow when expanding">
          <option value="both">both</option>
          <option value="out">outgoing</option>
          <option value="in">incoming</option>
        </select></label>
      </div>
      <p id="hopshelp">Applies to the next node you expand.</p>
      <label class="tog" id="nodeprow" hidden><input type="checkbox" id="shownodeps">
        show repos with no detected dependency <span id="nodepn" class="cnt"></span></label>
    </div>
    <div class="sgroup"><h2>Nodes</h2><div id="legend">__LEGEND__</div></div>
    <div class="sgroup"><h2>Relationships</h2><div id="edgelegend">__EDGE_LEGEND__</div></div>
    __LEGEND_KEYS__
    <div class="sgroup"><h2>Graph as text</h2>
      <details id="textview">
        <summary>Nodes and connections</summary>
        <p class="tv-note" id="tv-note"></p>
        <div id="tv-body"></div>
      </details>
    </div>
  </aside>
  <!-- role="application" was removed, not replaced. It tells assistive tech to hand
       every keystroke to the page and suppress browse mode; this page implemented no
       navigation to hand them to, so it produced a named empty region. The canvas is
       now a plain <main> that pans and zooms from the keyboard, and the readable
       rendering of the same subgraph lives in #textview. -->
  <main id="cy" aria-label="Knowledge graph" aria-describedby="cy-help" tabindex="0">
    <div id="empty"><img class="peek" src="data:image/webp;base64,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" alt="" aria-hidden="true"><div class="et">No nodes in this view</div>
      <div>Widen the seed, raise <code>--max-nodes</code>, or clear filters.</div></div>
    <!-- The minimap stays out of the accessibility tree on purpose. It is a pointer
         shortcut for panning, and panning is reachable without it: arrow keys pan the
         focused canvas, +/- zoom, 0 fits. Exposing a 180x130 canvas that can only be
         driven by dragging would announce a control with no keyboard behaviour. -->
    <canvas id="minimap" width="180" height="130" aria-hidden="true"
      title="Overview map — click or drag to navigate"></canvas>
    <p id="cy-help" class="cl-sr">Interactive diagram. Arrow keys pan, plus and minus
      zoom, 0 fits the view, Enter opens the text list of the same nodes and their
      connections. Escape clears the current selection.</p>
  </main>
  <aside id="info" role="complementary" aria-label="Details" tabindex="-1"></aside>
  <footer id="statusbar" role="status" aria-live="polite">
    <span id="meta"></span>
    <span id="trunc" class="trunc"></span>
    <span id="rmode" class="rmode"></span><span class="grow"></span>
    <span>context<span class="l">lake</span> graph</span>
  </footer>
</div>
<div id="tip" role="tooltip"></div>
<script>
  var ELEMENTS = __ELEMENTS__;
  var COLORS = __COLORS__;
  var ICONS = __ICONS__;
  var LANG_ICONS = __LANG_ICONS__;
  var DEFAULT_COLOR = "__DEFAULT_COLOR__";
  var REL_COLORS_LIGHT = __REL_COLORS__;
  var REL_COLORS_DARK = __REL_COLORS_DARK__;
  var REL_COLORS = REL_COLORS_LIGHT;          // swapped by applyTheme
  var DEFAULT_EDGE_COLORS = { light: "__DEFAULT_EDGE_COLOR__",
                              dark: "__DEFAULT_EDGE_COLOR_DARK__" };
  var DEFAULT_EDGE_COLOR = DEFAULT_EDGE_COLORS.light;
  var EDGE_INK = __EDGE_INK__;
  var CONF_META = __CONF_META__;
  var META = __META__;
  var LIVE = __LIVE__;
  var LAYOUT = "__LAYOUT__";
  var SITE = __SITE__;
__APP_JS_BLOCK__
</body>
</html>
"""
