#!/usr/bin/env python3
"""Render contextlake's markdown docs into branded, cross-linked site pages.

Every page shares one template: a hero (layer eyebrow + title + subtitle +
Pebble accent), the doc body, an on-page TOC rail, and a Next-steps footer.
The eyebrows anchor each page to the Mirror -> Knowledge -> Serve spine."""
import re
import json
import pathlib
import subprocess
import markdown

HERE = pathlib.Path(__file__).resolve().parent   # the site/ dir (source + build output)
REPO = HERE.parent                                # repo root
OUT = HERE
GH = "https://github.com/sayak-sarkar/contextlake/blob/main/"
BASE = "https://sayak.in/contextlake/"

# Shared brand assets are single-sourced in docs/; the build copies them into
# site/ (gitignored there) so the site stays self-contained without duplicating
# them in git.
SHARED_IMG = ["icon-16.png", "icon-32.png", "icon-48.png", "icon-64.png",
              "icon-180.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png",
              "og-card.jpg", "graph.jpg"]
SHARED_BRANDING = ["mark.png", "pebble-doc.png"]


def sync_assets():
    import shutil
    for f in SHARED_IMG:
        shutil.copy(REPO / "docs/img" / f, OUT / f)
    for f in SHARED_BRANDING:
        shutil.copy(REPO / "docs/branding" / f, OUT / f)
    shutil.copy(REPO / "docs/branding" / "pebble-peek-web.png", OUT / "pebble-peek.png")
    print(f"  synced {len(SHARED_IMG) + len(SHARED_BRANDING) + 1} shared assets from docs/")

# out, src, nav title, hero title, layer eyebrow, subtitle, pebble accent, next-steps
PAGES = [
    ("docs.html", "README.md", "Overview", "contextlake",
     "Start here", "A local context layer for your AI tools: mirror your repos, "
     "index them into a knowledge graph, and serve it over MCP.",
     "pebble-doc.png",
     [("quickstart.html", "Quickstart"), ("usage.html", "Mirror repositories"),
      ("knowledge-layer.html", "Knowledge layer")]),
    ("quickstart.html", "QUICKSTART.md", "Quickstart", "Quickstart",
     "Start here · all three layers", "Install, bootstrap, and wire your editor, "
     "the whole Mirror -> Knowledge -> Serve path in a few minutes.",
     "pebble-doc.png",
     [("usage.html", "Mirror repositories"), ("knowledge-layer.html", "Knowledge layer")]),
    ("usage.html", "docs/usage.md", "Mirror repositories", "Mirror repositories",
     "Build your knowledge base", "Mirror your Git repos locally and keep them fresh: fetch, "
     "clone, update, most-active branch, verify, and audit, with branch-safety and scheduling.",
     "pebble-doc.png",
     [("knowledge-layer.html", "Knowledge layer"), ("index-code-graph.html", "Index the code graph")]),
    ("configuration.html", "docs/configuration.md", "Configuration", "Configuration",
     "Get started", "Config-file precedence and the full settings reference for the "
     "mirror layer.",
     "pebble-doc.png",
     [("quickstart.html", "Quickstart"), ("usage.html", "Mirror repositories")]),
    ("index-code-graph.html", "docs/index-code-graph.md", "Index the code graph",
     "Index the code graph",
     "Build your knowledge base", "Turn your mirrored repos into a queryable code graph: "
     "incremental indexing, and the full node and edge model across 14 languages, Terraform, "
     "SQL, and web topology.",
     "pebble-doc.png",
     [("connect-enrich.html", "Connect and enrich"), ("semantic-search.html", "Semantic search")]),
    ("connect-enrich.html", "docs/connect-enrich.md", "Connect and enrich", "Connect and enrich",
     "Build your knowledge base", "Link repos to their issues, docs, and designs, manage "
     "sources, and pull grounded external facts into the knowledge layer with query-driven "
     "enrichment.",
     "pebble-doc.png",
     [("semantic-search.html", "Semantic search"), ("generate-wiki.html", "Generate the wiki")]),
    ("semantic-search.html", "docs/semantic-search.md", "Semantic search", "Semantic search",
     "Build your knowledge base", "Natural-language and hybrid graph-propagation retrieval: "
     "embed your code, tune the vector backend, and query across repos and languages.",
     "pebble-doc.png",
     [("generate-wiki.html", "Generate the wiki"), ("model-providers.html", "Model providers")]),
    ("generate-wiki.html", "docs/generate-wiki.md", "Generate the wiki", "Generate the wiki",
     "Build your knowledge base", "Turn the graph into grounded, council-verified prose per "
     "repo: searchable, enrichment-aware, with a provenance footer.",
     "pebble-doc.png",
     [("model-providers.html", "Model providers"), ("bootstrap.html", "Bootstrap and keep fresh")]),
    ("model-providers.html", "docs/model-providers.md", "Model providers", "Model providers",
     "Build your knowledge base", "The pluggable embeddings and wiki backends: auto, built-in "
     "CPU, Ollama, OpenAI, Anthropic, and agent-CLI, with data-sharing posture and setup.",
     "pebble-doc.png",
     [("bootstrap.html", "Bootstrap and keep fresh"), ("dashboard.html", "Dashboard")]),
    ("bootstrap.html", "docs/bootstrap.md", "Bootstrap and keep fresh", "Bootstrap and keep fresh",
     "Build your knowledge base", "Run the whole pipeline in one command, compose the stages, "
     "and keep it fresh with cron or a git post-commit hook.",
     "pebble-doc.png",
     [("dashboard.html", "Dashboard"), ("serve.html", "Serve (MCP)")]),
    ("knowledge-layer.html", "docs/knowledge-layer.md", "Knowledge layer", "Knowledge layer",
     "Layer 2 · Knowledge", "Turn the mirror into a queryable graph with search, a wiki, "
     "and connectors.",
     "pebble-doc.png",
     [("index-code-graph.html", "Index the code graph"), ("bootstrap.html", "Bootstrap and keep fresh")]),
    ("dashboard.html", "docs/dashboard.md", "Dashboard", "The dashboard",
     "Layer 2 · the human UI", "A guided tour of the local, offline-first dashboard: "
     "the fleet overview, per-repo anatomy, the architecture graph, blast radius, and "
     "generating a wiki.",
     "pebble-doc.png",
     [("knowledge-layer.html", "Knowledge layer"), ("serve.html", "Serve (MCP)")]),
    ("serve.html", "docs/serve.md", "Serve (MCP)", "Serve it to your editor",
     "Layer 3 · Serve", "Expose the knowledge layer over MCP and wire your editors "
     "(Claude Code, Windsurf, Kiro) in one command.",
     "pebble-doc.png",
     [("dashboard.html", "Dashboard"), ("visualize.html", "Visualize the graph")]),
    ("visualize.html", "docs/visualize.md", "Visualize the graph", "Visualize the graph",
     "Use it", "Draw bounded, offline graph slices (`contextlake graph`) in HTML, DOT, Mermaid, "
     "or a class diagram, plus the composed namespace C4 diagram.",
     "pebble-doc.png",
     [("dashboard.html", "Dashboard"), ("serve.html", "Serve (MCP)")]),
    ("ownership.html", "docs/ownership.md", "Ownership and SMEs", "Ownership and SMEs",
     "Use it", "Find who owns a repo or path and who to ask, ranked recency-weighted from git "
     "history (`contextlake owners` / `who_knows`), no config or index required.",
     "pebble-doc.png",
     [("serve.html", "Serve (MCP)"), ("dashboard.html", "Dashboard")]),
    ("benchmarks.html", "docs/benchmarks.md", "Benchmarks", "What it actually saves",
     "Layer 3 · Serve", "An honest, measured look at the token, cost, and correctness "
     "impact of connecting the contextlake MCP to your AI coding tools, new-code "
     "grounding first, plus search, maintenance, and the caveats.",
     "pebble-doc.png",
     [("serve.html", "Serve (MCP)"), ("knowledge-layer.html", "Knowledge layer")]),
    ("internals.html", "docs/internals.md", "Architecture", "Architecture & internals",
     "Under the hood", "How all three layers work inside, the store, concurrency, "
     "branch selection, extraction, and the offline boundary.",
     "pebble-doc.png",
     [("storage.html", "Storage"), ("knowledge-layer.html", "Knowledge layer")]),
    ("storage.html", "docs/storage.md", "Storage", "Storage & the no-pollution invariant",
     "Under the hood", "Where contextlake keeps everything it generates, one store "
     "directory, never polluting your synced repos.",
     "pebble-doc.png",
     [("internals.html", "Architecture"), ("usage.html", "Mirror repositories")]),
    ("cli-reference.html", "docs/cli-reference.md", "Command reference",
     "contextlake command reference",
     "Reference", "Every contextlake command at a glance, with links to the page that "
     "documents each in depth.",
     "pebble-doc.png",
     [("console-output.html", "Reading the console output"), ("docs.html", "Overview")]),
    ("console-output.html", "docs/console-output.md", "Reading the console output",
     "Reading the console output",
     "Reference", "Decode the progress bar, the status glyph vocabulary, and the "
     "stdout/stderr split.",
     "pebble-doc.png",
     [("cli-reference.html", "Command reference"), ("docs.html", "Overview")]),
    ("changelog.html", "CHANGELOG.md", "Changelog", "Changelog",
     "Reference", "Release history for contextlake.",
     "pebble-doc.png",
     [("docs.html", "Overview"), ("quickstart.html", "Quickstart")]),
    ("style-guide.html", "docs/style-guide.md", "Writing style", "Documentation style guide",
     "Writing style", "The spirit, the checklist, and links to the focused pages: voice, "
     "structure, formatting, and the word reference.",
     "pebble-doc.png",
     [("style-guide-voice.html", "Voice and tone"), ("style-guide-structure.html", "Page types and structure")]),
    ("style-guide-voice.html", "docs/style-guide-voice.md", "Voice and tone", "Voice and tone",
     "Writing style", "Second person, present tense, warm and grounded: the voice defaults, "
     "word choice, and writing for every reader.",
     "pebble-doc.png",
     [("style-guide-structure.html", "Page types and structure"), ("style-guide-formatting.html", "Formatting")]),
    ("style-guide-structure.html", "docs/style-guide-structure.md", "Page types and structure",
     "Page types and structure",
     "Writing style", "The concept, how-to, reference, and tutorial page types, each with a "
     "fixed skeleton, and how to structure a page.",
     "pebble-doc.png",
     [("style-guide-formatting.html", "Formatting"), ("style-guide-reference.html", "Word reference")]),
    ("style-guide-formatting.html", "docs/style-guide-formatting.md",
     "Formatting", "Formatting, accessibility, and inclusive language",
     "Writing style", "Headings, lists, code, callouts, links, accessibility, and inclusive "
     "language: the mechanics that keep every page consistent.",
     "pebble-doc.png",
     [("style-guide-reference.html", "Word reference"), ("brand.html", "Brand overview")]),
    ("style-guide-reference.html", "docs/style-guide-reference.md", "Word reference",
     "Word and term reference",
     "Writing style", "The house-style decision cache, before and after rewrites, and the "
     "A-to-Z term reference.",
     "pebble-doc.png",
     [("brand.html", "Brand overview"), ("style-guide.html", "Writing style")]),
    ("brand.html", "docs/brand.md", "Brand overview", "Brand overview",
     "Brand", "contextlake's brand in one page: essence, voice, the lake metaphor, Pebble, "
     "the palette, and the mark, with the full spec linked.",
     "pebble-doc.png",
     [("style-guide.html", "Writing style"), ("docs.html", "Overview")]),
]
TO_PAGE = {src: out for out, src, *_ in PAGES}
TO_GH = ["docs/releasing.md", "ROADMAP.md", "CONTRIBUTING.md", "BRANDING.md", "LICENSE"]

# Sidebar navigation, organized into labeled groups (ordered). Every PAGES `out` appears
# in exactly one group; the group heading reuses the existing `.side h2` styling.
NAV_GROUPS = [
    ("Get started", ["docs.html", "quickstart.html", "configuration.html"]),
    ("Build your knowledge base", ["usage.html", "knowledge-layer.html", "index-code-graph.html",
                                   "connect-enrich.html", "semantic-search.html",
                                   "generate-wiki.html", "model-providers.html", "bootstrap.html"]),
    ("Use it", ["serve.html", "dashboard.html", "visualize.html", "ownership.html"]),
    ("Understand it", ["internals.html", "storage.html", "benchmarks.html"]),
    ("Writing style", ["style-guide.html", "style-guide-voice.html", "style-guide-structure.html",
                       "style-guide-formatting.html", "style-guide-reference.html"]),
    ("Brand", ["brand.html"]),
    ("Reference", ["cli-reference.html", "console-output.html", "changelog.html"]),
]
GROUP_OF = {out: g for g, outs in NAV_GROUPS for out in outs}
SUBTITLE_OF = {m[0]: m[5] for m in PAGES}
TITLES = {out: nav for out, _, nav, *_ in PAGES}

# "Next steps" are CURATED per page: the last field of each PAGES entry names the one or two
# most relevant next reads (learning-journey pages point forward; reference/meta pages point
# back to a guide or omit it). Build-time check: every curated target must be a real page, so a
# renamed or retired page fails the build loudly instead of leaving a dead link.
_VALID_OUT = {p[0] for p in PAGES}
for _p in PAGES:
    for _href, _lbl in _p[7]:
        if _href not in _VALID_OUT:
            raise SystemExit(f"build_docs: Next-steps target {_href!r} on {_p[0]} is not a page")

GLYPH = '<img class="glyph" src="icon-64.png" width="28" height="28" alt="" aria-hidden="true">'
GH_MARK = ('<svg class="lmark" viewBox="0 0 24 24" fill="currentColor" width="15" height="15" '
           'aria-hidden="true"><path d="M12 2A10 10 0 0 0 8.8 21.5c.5.1.7-.2.7-.5v-1.7c-2.8.6-3.4-1.3-3.4-1.3-.5-1.2-1.1-1.5-1.1-1.5-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.5 2.3 1.1 2.9.8.1-.6.3-1.1.6-1.4-2.2-.2-4.6-1.1-4.6-5 0-1.1.4-2 1-2.7-.1-.3-.4-1.3.1-2.7 0 0 .8-.3 2.7 1a9.4 9.4 0 0 1 5 0c1.9-1.3 2.7-1 2.7-1 .5 1.4.2 2.4.1 2.7.6.7 1 1.6 1 2.7 0 3.9-2.3 4.8-4.6 5 .4.3.7.9.7 1.9v2.8c0 .3.2.6.7.5A10 10 0 0 0 12 2Z"/></svg>')
PYPI_MARK = ('<img src="pypi-logo.svg" class="lmark" width="15" height="13" alt="" style="vertical-align:-2px">')


def _btn(href, mark, label):
    return (f'<a class="icon-btn" href="{href}" aria-label="{label}" title="{label}" '
            f'rel="noopener" target="_blank">{mark}</a>')


GH_BTN = _btn("https://github.com/sayak-sarkar/contextlake", GH_MARK, "contextlake on GitHub")
PYPI_BTN = _btn("https://pypi.org/project/contextlake/", PYPI_MARK, "contextlake on PyPI")
# a labeled GitHub button for the header, matching the landing (PyPI stays in the footer)
GH_LABELED = ('<a class="hbtn" href="https://github.com/sayak-sarkar/contextlake" '
              'rel="noopener" target="_blank">' + GH_MARK + "GitHub</a>")
# footer social links: labeled (icon + text), mirroring the landing footer for a uniform look
FOOT_GH = ('<a href="https://github.com/sayak-sarkar/contextlake" rel="noopener" target="_blank">'
           + GH_MARK + "GitHub</a>")
FOOT_PYPI = ('<a href="https://pypi.org/project/contextlake/" rel="noopener" target="_blank">'
             + PYPI_MARK + "PyPI</a>")
FOOT_MARK = ('<img src="mark.png" width="28" height="28" alt="" aria-hidden="true" '
             'style="width:28px;height:28px;vertical-align:middle;margin-right:8px">')


def de_emdash(text: str) -> str:
    return text.replace(" — ", ", ").replace("—", ", ")


def theme_swap_dashboard_imgs(html: str) -> str:
    """A dashboard screenshot with a ``NAME-dark.png`` sibling becomes a light+dark pair;
    docs.css shows the one that matches the reader's theme (manual toggle or OS preference)."""
    def add_cls(tag: str, cls: str) -> str:
        if 'class="' in tag:
            return re.sub(r'class="([^"]*)"', lambda mm: f'class="{mm.group(1)} {cls}"', tag, count=1)
        return tag.replace("<img", f'<img class="{cls}"', 1)

    def repl(m):
        tag, src, name = m.group(0), m.group("src"), m.group("name")
        if name.endswith("-dark") or not (REPO / f"docs/img/dashboard/{name}-dark.png").exists():
            return tag
        light = add_cls(tag, "ss ss-light")
        dark = add_cls(tag.replace(f"{name}.png", f"{name}-dark.png"), "ss ss-dark")
        return light + dark

    return re.sub(
        r'<img[^>]*\bsrc="(?P<src>[^"]*docs/img/dashboard/(?P<name>[a-z0-9-]+)\.png)"[^>]*>',
        repl, html)


# Map each doc source to its built page by BOTH its full path and its bare basename, so
# cross-links written either way resolve — README uses `docs/foo.md`, sibling docs use a
# bare `foo.md`. Anchors (`foo.md#sec`) are preserved.
_LINK_TO_PAGE = {}
for _out, _src, *_rest in PAGES:
    _LINK_TO_PAGE[_src] = _out
    _LINK_TO_PAGE[_src.split("/")[-1]] = _out
    # a doc may also link directly to the built page name (`changelog.html`); map it to itself
    # so such links stay on-site instead of falling through to a nonexistent GitHub blob.
    _LINK_TO_PAGE[_out] = _out
    # README/PyPI links are absolute GitHub URLs (they must resolve on PyPI); map those
    # back to the local built page so the on-site nav stays on-site.
    _LINK_TO_PAGE[GH + _src] = _out


def linkify(out: str) -> str:
    """An internal page link without its ``.html`` extension. Files keep their ``.html``
    names on disk, but GitHub Pages (with .nojekyll) serves them extensionless, so the
    address bar stays clean and old ``/foo.html`` links still resolve. ``index.html`` maps
    to the site root so it never shows as ``/index``."""
    if out == "index.html":
        return "./"
    return out[:-5] if out.endswith(".html") else out


def rewrite_links(html: str) -> str:
    """Resolve every doc link consistently: a link to a built page (in any form —
    `foo.md`, `docs/foo.md`, `../foo.md`, or the absolute GitHub URL used in the README)
    becomes the local `.html`; a relative link to a repo file that has no page (examples/,
    LICENSE, …) becomes an absolute GitHub URL; external/anchor links are left alone."""
    def repl(m):
        href = m.group(1)
        path, sep, anchor = href.partition("#")
        norm = path
        while norm.startswith(("../", "./")):
            norm = norm.split("/", 1)[1] if "/" in norm else ""
        for key in (path, norm, norm.split("/")[-1], "docs/" + norm):
            if key and key in _LINK_TO_PAGE:
                return f'href="{linkify(_LINK_TO_PAGE[key])}{sep}{anchor}"'
        if href.startswith(("http", "#", "mailto:")):
            return m.group(0)
        if norm:  # a repo file with no built page → point at GitHub
            return f'href="{GH}{norm}{sep}{anchor}"'
        return m.group(0)
    return re.sub(r'href="([^"]+)"', repl, html)


def mark_external(html: str) -> str:
    """After links are resolved, in-prose links to an http(s) target are outbound. Give them
    rel=noopener + target=_blank and a small ↗ so a reader can tell an outbound link from an
    in-site one at a glance. Image links (badges) are left unmarked. Applied to the rendered
    page only, never to the text harvested for the search index."""
    def repl(m):
        attrs, inner = m.group(1), m.group(2)
        if 'href="http' not in attrs:
            return m.group(0)
        if "target=" not in attrs:
            attrs += ' target="_blank"'
        if "rel=" not in attrs:
            attrs += ' rel="noopener"'
        arrow = "" if "<img" in inner else '<span class="ext-arrow" aria-hidden="true">↗</span>'
        return f"<a{attrs}>{inner}{arrow}</a>"
    return re.sub(r"(?s)<a\b([^>]*)>(.*?)</a>", repl, html)


def strip_first_h1(html: str) -> str:
    """Remove the first <h1>…</h1> (lifted into the hero), wherever it sits."""
    return re.sub(r"<h1[^>]*>.*?</h1>", "", html, count=1, flags=re.S)


def strip_readme_frontmatter(html: str) -> str:
    """The README opens with a banner image + centered title + tagline + badges,
    which the page hero now replaces. Drop everything up to the first <hr>."""
    m = re.search(r"<hr\s*/?>", html)
    return html[m.end():] if m else strip_first_h1(html)


def sidebar(active: str) -> str:
    # home is reached via the clickable wordmark in the header. Nav is organized into
    # labeled groups (NAV_GROUPS); each group heading reuses the `.side h2` styling.
    blocks = []
    for group, outs in NAV_GROUPS:
        links = []
        for out in outs:
            cls = ' class="active"' if out == active else ""
            links.append(f'<a href="{linkify(out)}"{cls}>{TITLES[out]}</a>')
        blocks.append(f'<h2>{group}</h2><nav aria-label="{group}">'
                      + "".join(links) + "</nav>")
    ext = f'<div class="ext"><div class="social-row">{GH_BTN}{PYPI_BTN}</div></div>'
    # On mobile the whole nav is collapsed behind a disclosure toggle so readers land on
    # the content, not a wall of links; on desktop the toggle is hidden and the body shows.
    toggle = ('<button class="side-toggle" id="side-toggle" type="button" '
              'aria-expanded="false" aria-controls="side-body">'
              '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
              '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/>'
              '<line x1="3" y1="18" x2="21" y2="18"/></svg>'
              '<span>Browse docs</span>'
              '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
              'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
              '<path d="m6 9 6 6 6-6"/></svg></button>')
    body = f'<div class="side-body" id="side-body">{"".join(blocks)}{ext}</div>'
    return f'<aside class="side">{toggle}{body}</aside>'


def hero(title: str, eyebrow: str, subtitle: str, pebble: str) -> str:
    return (f'<header class="doc-hero">'
            f'<div class="doc-hero-text">'
            f'<div class="doc-eyebrow">{eyebrow}</div>'
            f'<h1>{title}</h1>'
            f'<p class="doc-sub">{subtitle}</p>'
            f'</div>'
            f'<img class="doc-pebble" src="{pebble}" alt="" aria-hidden="true" '
            f'width="120" height="120" loading="lazy">'
            f'</header>')


def next_steps(links) -> str:
    if not links:  # reference / meta pages may omit Next steps entirely
        return ""
    cards = "".join(
        f'<a class="next-card" href="{linkify(href)}"><span>{label}</span>'
        f'<svg viewBox="0 0 24 24" aria-hidden="true" width="18" height="18">'
        f'<path d="M5 12h14M13 6l6 6-6 6" fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></a>'
        for href, label in links)
    return (f'<section class="next-steps" aria-label="Next steps">'
            f'<h2>Next steps</h2><div class="next-grid">{cards}</div></section>')


def toc_rail(toc_html: str) -> str:
    if not toc_html or "<ul" not in toc_html:
        return '<aside class="toc-rail" aria-hidden="true"></aside>'
    # strip the wrapping <div class="toc"> markdown adds; keep the <ul>
    inner = re.sub(r'^\s*<div class="toc">|</div>\s*$', "", toc_html.strip())
    return (f'<aside class="toc-rail"><nav aria-label="On this page">'
            f'<p class="toc-title">On this page</p>{inner}</nav></aside>')


# Site-wide theme: a no-flash init (runs in <head>) + a toggle button + its wiring. The same
# three pieces live in site/index.html so the light/dark choice persists across the whole site
# (shared localStorage key "cl-theme"). No JS -> the page follows the OS via prefers-color-scheme.
THEME_INIT = ('<script>(function(){try{var t=localStorage.getItem("cl-theme");'
              'if(t)document.documentElement.setAttribute("data-theme",t);}catch(e){}})();</script>')
THEME_TOGGLE = (
    '<button class="icon-btn theme-toggle" id="theme-toggle" type="button" hidden '
    'aria-label="Switch theme" title="Switch light / dark theme">'
    '<svg class="moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
    '<svg class="sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4'
    'M17.6 17.6 19 19M19 5l-1.4 1.4M6.4 17.6 5 19"/></svg></button>')
THEME_JS = (
    '<script>(function(){var r=document.documentElement,b=document.getElementById("theme-toggle");'
    'if(!b)return;b.hidden=false;'
    'function cur(){var e=r.getAttribute("data-theme");'
    'return e||(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");}'
    'function lbl(t){b.setAttribute("aria-label",t==="dark"?"Switch to light theme":"Switch to dark theme");}'
    'lbl(cur());'
    'function set(n){r.setAttribute("data-theme",n);try{localStorage.setItem("cl-theme",n);}catch(e){}lbl(n);}'
    'b.addEventListener("click",function(){var n=cur()==="dark"?"light":"dark";'
    'var reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;'
    'if(document.startViewTransition&&!reduce){document.startViewTransition(function(){set(n);});}else{set(n);}});})();</script>')

# copy-to-clipboard on code blocks (progressive enhancement: no Clipboard API -> no button)
COPY_JS = r"""<script>(function(){if(!navigator.clipboard||!navigator.clipboard.writeText)return;
var C='<svg class="ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg><svg class="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5 10 17l9-10"/></svg>';
document.querySelectorAll(".prose pre").forEach(function(pre){if(pre.querySelector(".copy-btn"))return;
var code=pre.querySelector("code")||pre;pre.classList.add("has-copy");
var b=document.createElement("button");b.type="button";b.className="copy-btn";b.setAttribute("aria-label","Copy to clipboard");b.innerHTML=C;var t=null;
b.addEventListener("click",function(){navigator.clipboard.writeText(code.innerText.replace(/\n+$/,"")).then(function(){b.classList.add("copied");b.setAttribute("aria-label","Copied");clearTimeout(t);t=setTimeout(function(){b.classList.remove("copied");b.setAttribute("aria-label","Copy to clipboard");},1600);});});
pre.appendChild(b);});})();</script>"""

# content tabs: progressive enhancement. Authors write <div class="tabs"> with <div class="tab"
# data-label="..."> children; no-JS stacks them labeled, JS builds a tab strip showing one at a time.
TAB_JS = r"""<script>(function(){document.querySelectorAll(".prose .tabs").forEach(function(g){
var tabs=g.querySelectorAll(":scope > .tab");if(tabs.length<2)return;
var strip=document.createElement("div");strip.className="tab-strip";strip.setAttribute("role","tablist");
tabs.forEach(function(t,i){var b=document.createElement("button");b.type="button";b.className="tab-btn";b.setAttribute("role","tab");b.textContent=t.getAttribute("data-label")||("Tab "+(i+1));
b.addEventListener("click",function(){tabs.forEach(function(x){x.hidden=true;});strip.querySelectorAll(".tab-btn").forEach(function(x){x.setAttribute("aria-selected","false");});t.hidden=false;b.setAttribute("aria-selected","true");});
strip.appendChild(b);});
g.insertBefore(strip,g.firstChild);g.classList.add("tabs-js");
tabs.forEach(function(t,i){t.hidden=i!==0;});strip.querySelector(".tab-btn").setAttribute("aria-selected","true");});})();</script>"""


# Mobile-only sidebar disclosure: the toggle button flips .side.open and its aria-expanded.
# It closes when a nav link is chosen (navigation follows anyway) so the menu never lingers.
SIDE_JS = r"""<script>(function(){var b=document.getElementById("side-toggle");var s=b&&b.closest(".side");if(!s)return;
b.addEventListener("click",function(){var open=s.classList.toggle("open");b.setAttribute("aria-expanded",open?"true":"false");});
s.querySelectorAll(".side-body a").forEach(function(a){a.addEventListener("click",function(){s.classList.remove("open");b.setAttribute("aria-expanded","false");});});})();</script>"""


def _plain_text(html: str) -> str:
    # drop heading permalink anchors first (tag + its "#" text), else the # leaks into snippets
    t = re.sub(r'(?s)<a class="anchor".*?</a>', " ", html)
    t = re.sub(r"(?s)<(script|style)\b.*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _sections(html: str):
    """Split prose HTML into (anchor, section_title, section_text) by content headings
    (h2/h3 carry an id from the toc extension; sidebar headings don't, so they're skipped).
    The heading's trailing permalink anchor is stripped from the title by _plain_text."""
    heads = list(re.finditer(r'<h[23]\s+id="([^"]+)"[^>]*>(.*?)</h[23]>', html, re.S))
    secs = []
    for i, m in enumerate(heads):
        inner = re.sub(r'(?s)<a class="anchor".*?</a>', "", m.group(2))  # drop the permalink #
        title = _plain_text(inner)
        if not title:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(html)
        secs.append((m.group(1), title, _plain_text(html[m.end():end])))
    return secs


_GIT_DATE_CACHE = {}


def git_date(relpath: str):
    """The source file's last git commit date (YYYY-MM-DD), for sitemap lastmod + article
    dateModified. Returns None if git/history is unavailable, so lastmod is simply omitted
    rather than faked to the build date."""
    if relpath in _GIT_DATE_CACHE:
        return _GIT_DATE_CACHE[relpath]
    d = None
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%cs", "--", relpath],
                           cwd=str(REPO), capture_output=True, text=True, timeout=10)
        d = r.stdout.strip() or None
    except Exception:
        d = None
    _GIT_DATE_CACHE[relpath] = d
    return d


def _ld_script(obj) -> str:
    """Serialize a JSON-LD object safely for an inline <script>: json.dumps handles quoting
    (never f-string interpolation of untrusted title/description), and </ is escaped so a
    stray closing tag in the data can't break out of the script element."""
    return ('<script type="application/ld+json">'
            + json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
            + "</script>")


def docs_jsonld(nav_title: str, subtitle: str, url: str, moddate) -> str:
    """Per-page structured data: a TechArticle (with dateModified) + a BreadcrumbList
    (contextlake -> Docs -> this page; the sidebar group is a UI grouping, not a navigable
    page, so it is left out to keep every crumb a distinct real URL), both under one @graph."""
    article = {
        "@type": "TechArticle",
        "headline": nav_title,
        "description": subtitle,
        "url": url,
        "inLanguage": "en",
        "author": {"@type": "Person", "name": "Sayak Sarkar"},
        "publisher": {"@type": "Organization", "name": "contextlake", "url": BASE},
        "isPartOf": {"@type": "WebSite", "name": "contextlake", "url": BASE},
    }
    if moddate:
        article["dateModified"] = moddate
    crumbs = [("contextlake", BASE), ("Docs", f"{BASE}docs"), (nav_title, url)]
    breadcrumb = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": item}
            for i, (name, item) in enumerate(crumbs)
        ],
    }
    return _ld_script({"@context": "https://schema.org", "@graph": [article, breadcrumb]})


def shell(meta, body, toc_html) -> str:
    out, src, nav_title, h_title, eyebrow, subtitle, pebble, hand_links = meta
    links = hand_links  # curated per page (see PAGES); validated at import against _VALID_OUT
    url = f"{BASE}{linkify(out)}"
    jsonld = docs_jsonld(nav_title, subtitle, url, git_date(src))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{THEME_INIT}
<title>{nav_title} · contextlake docs</title>
<meta name="description" content="{subtitle}">
<meta name="robots" content="index, follow, max-image-preview:large">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="contextlake">
<meta property="og:title" content="{nav_title} · contextlake docs">
<meta property="og:description" content="{subtitle}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{BASE}og-card.jpg">
<meta property="og:image:alt" content="contextlake: a local context layer for your AI tools.">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{nav_title} · contextlake docs">
<meta name="twitter:description" content="{subtitle}">
<meta name="twitter:image" content="{BASE}og-card.jpg">
{jsonld}
<link rel="icon" type="image/png" sizes="32x32" href="icon-32.png">
<link rel="apple-touch-icon" href="icon-180.png">
<meta name="theme-color" content="#137A8B">
<link rel="preload" as="font" type="font/woff2" href="fonts/space-grotesk-600.woff2" crossorigin>
<link rel="preload" as="font" type="font/woff2" href="fonts/inter-400.woff2" crossorigin>
<link rel="preload" as="font" type="font/woff2" href="fonts/jetbrains-mono-400.woff2" crossorigin>
<link rel="stylesheet" href="fonts.css">
<link rel="stylesheet" href="docs.css">
<link rel="stylesheet" href="cmdk.css">
</head>
<body>
<a class="skip" href="#doc">Skip to content</a>
<header><div class="nav">
  <a class="brand" href="./" aria-label="contextlake home">{GLYPH}contextlake</a>
  <span class="spacer"></span>
  <button class="cmdk-trigger" id="cmdk-open" type="button" aria-haspopup="dialog" aria-label="Search the docs">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
    <span class="lbl">Search docs</span><kbd class="cmdk-kbd" id="cmdk-hint">/</kbd>
  </button>
  <span class="social-row">{THEME_TOGGLE}{GH_LABELED}</span>
</div></header>
<div class="shell">
  {sidebar(out)}
  <main class="prose" id="doc">
    {breadcrumbs(out)}
    {hero(h_title, eyebrow, subtitle, pebble)}
    {body}
    {next_steps(links)}
  </main>
  {toc_rail(toc_html)}
</div>
<footer><div class="f-in">
  <span class="tagline">{FOOT_MARK}Deep context. Clear answers.</span>
  <nav class="f-links" aria-label="Footer">
    <a href="./">Home</a><a href="changelog">Changelog</a>{FOOT_GH}{FOOT_PYPI}
  </nav>
</div></footer>
{THEME_JS}
{COPY_JS}
{TAB_JS}
{SIDE_JS}
<script defer src="cmdk.js"></script>
</body>
</html>"""


def make404():
    # Self-contained immersive 404: a full-bleed misty-Pebble scene with the copy
    # overlaid. Deliberately does NOT use docs.css (own styles) so the page layout
    # can't be affected by the shared sticky-footer/grid rules.
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lost in the fog · contextlake</title>
<meta name="robots" content="noindex">
<meta name="description" content="That page drifted off into the mist. Pebble will guide you back to contextlake.">
<link rel="icon" type="image/png" sizes="32x32" href="icon-32.png">
<link rel="apple-touch-icon" href="icon-180.png">
<meta name="theme-color" content="#0E2A33">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{ --deepwater:#0E2A33; --abyss:#081a20; --lake:#137A8B; --current:#2BB3A3; --mist:#EAF4F4; }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;font-family:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    color:var(--mist);background:var(--abyss);-webkit-font-smoothing:antialiased}

  /* full-bleed misty-Pebble scene */
  .scene{position:relative;min-height:100svh;display:grid;place-items:center;
    text-align:center;padding:48px 24px;overflow:hidden;
    background:#0b2129 url("hero-scene.webp") center 38% / cover no-repeat;}
  @media (max-width:720px){
    .scene{background-image:url("hero-scene-mobile.webp");background-position:center 30%}
  }

  /* underwater depth + a vignette that closes in like fog */
  .scene::before{content:"";position:absolute;inset:0;pointer-events:none;
    background:
      radial-gradient(125% 85% at 50% 32%, transparent 38%, rgba(14,42,51,.55) 72%, rgba(8,26,32,.94) 100%),
      linear-gradient(180deg, rgba(8,26,32,.55), rgba(14,42,51,.12) 38%, rgba(8,26,32,.9));}

  /* a slow drifting fog bank */
  .scene::after{content:"";position:absolute;inset:-25% -25% -25% -25%;pointer-events:none;mix-blend-mode:screen;
    background:
      radial-gradient(40% 32% at 30% 42%, rgba(204,224,228,.16), transparent 70%),
      radial-gradient(48% 30% at 72% 58%, rgba(190,214,219,.12), transparent 72%),
      radial-gradient(30% 24% at 55% 24%, rgba(231,181,60,.07), transparent 70%);
    filter:blur(6px);animation:drift 34s ease-in-out infinite alternate;}
  @keyframes drift{from{transform:translate3d(-3%,1%,0) scale(1.04)}
                   to{transform:translate3d(4%,-2%,0) scale(1.12)}}

  .content{position:relative;z-index:2;max-width:540px;text-shadow:0 2px 24px rgba(0,0,0,.55)}
  .eyebrow{font-family:"Space Grotesk",sans-serif;font-weight:700;letter-spacing:.38em;
    text-transform:uppercase;font-size:13px;color:#bfe0e4;margin:0 0 14px;opacity:.92}
  h1{font-family:"Space Grotesk",sans-serif;font-weight:700;line-height:1.05;
    font-size:clamp(38px,8vw,68px);margin:0}
  .sub{font-size:clamp(16px,2.4vw,19px);color:#d7e9ec;line-height:1.6;margin:18px auto 30px;max-width:34em}
  .sub b{color:#fff;font-weight:600}
  .actions{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}
  .btn{display:inline-flex;align-items:center;gap:8px;height:46px;padding:0 22px;border-radius:11px;
    font-weight:600;font-size:15px;text-decoration:none;transition:transform .15s,background .15s,border-color .15s}
  .btn.primary{background:var(--current);color:#06231f;box-shadow:0 10px 30px -10px rgba(43,179,163,.6)}
  .btn.primary:hover{background:#36c4b3;transform:translateY(-1px)}
  .btn.ghost{color:var(--mist);border:1px solid rgba(234,244,244,.32);background:rgba(234,244,244,.06)}
  .btn.ghost:hover{border-color:var(--current);background:rgba(234,244,244,.12);transform:translateY(-1px)}
  :focus-visible{outline:none;box-shadow:0 0 0 2px var(--abyss),0 0 0 4px var(--current);border-radius:12px}

  .home{position:absolute;top:22px;left:24px;z-index:3;display:inline-flex;align-items:center;gap:9px;
    color:var(--mist);text-decoration:none;font-family:"Space Grotesk",sans-serif;font-weight:600;
    font-size:16px;opacity:.92;text-shadow:0 2px 16px rgba(0,0,0,.5)}
  .home img{height:26px;width:auto;display:block}
  .home:hover{opacity:1}

  @media (prefers-reduced-motion:reduce){ .scene::after{animation:none} }
</style>
</head>
<body>
<main class="scene">
  <a class="home" href="./" aria-label="contextlake home">
    <img src="icon-64.png" width="26" height="26" alt="">contextlake
  </a>
  <div class="content">
    <p class="eyebrow">404 · off the map</p>
    <h1>Lost in the fog</h1>
    <p class="sub">This page drifted off into the mist. <b>Pebble</b> can't find it
      down here either, but the way back to shore is just a click away.</p>
    <div class="actions">
      <a class="btn primary" href="./">Back to shore</a>
      <a class="btn ghost" href="docs">Read the docs</a>
    </div>
  </div>
</main>
</body>
</html>"""


def breadcrumbs(out: str) -> str:
    group = GROUP_OF.get(out, "")
    return (f'<nav class="crumbs" aria-label="Breadcrumb">'
            f'<a href="docs">Docs</a><span aria-hidden="true">/</span>'
            f'<span>{group}</span><span aria-hidden="true">/</span>'
            f'<span aria-current="page">{TITLES.get(out, "")}</span></nav>')


LLMS_INTRO = """# contextlake

> A local context layer for your AI tools: mirror your Git repositories, index them into a
> queryable knowledge graph + wiki, and serve it to AI editors over MCP. Offline-first, so
> agents answer from real source instead of guessing. Python CLI, published on PyPI.
"""


def _sm_url(loc: str, pr: str, mod) -> str:
    lm = f"<lastmod>{mod}</lastmod>" if mod else ""
    return f"<url><loc>{loc}</loc>{lm}<priority>{pr}</priority></url>"


def gen_sitemap():
    """sitemap.xml, generated from PAGES so it never goes stale by hand. lastmod is the
    source file's git commit date (the landing tracks site/index.html)."""
    urls = [_sm_url(BASE, "1.0", git_date("site/index.html"))]
    for out, src, *_ in PAGES:
        pr = "0.9" if out == "docs.html" else "0.6"
        urls.append(_sm_url(f"{BASE}{linkify(out)}", pr, git_date(src)))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n  '
           + "\n  ".join(urls) + "\n</urlset>\n")
    (OUT / "sitemap.xml").write_text(xml, encoding="utf-8")
    print("  -> sitemap.xml")


def gen_llms():
    """llms.txt (llmstxt.org), generated from PAGES/NAV_GROUPS so an AI ingesting the
    docs gets a complete, current, link-annotated map. Grouped by the nav sections."""
    parts = [LLMS_INTRO]
    for group, outs in NAV_GROUPS:
        parts.append(f"## {group}\n")
        for out in outs:
            parts.append(f"- [{TITLES[out]}]({BASE}{linkify(out)}): {SUBTITLE_OF[out]}")
        parts.append("")
    parts += ["## Source\n",
              "- [GitHub repository](https://github.com/sayak-sarkar/contextlake)",
              "- [PyPI package](https://pypi.org/project/contextlake/)", ""]
    parts += ["## Optional\n",
              f"- [Full documentation, one file]({BASE}llms-full.txt): every page's full text "
              "concatenated for single-fetch ingestion.", ""]
    (OUT / "llms.txt").write_text("\n".join(parts), encoding="utf-8")
    print("  -> llms.txt")


def gen_llms_full():
    """llms-full.txt: the whole docs corpus in one file (intro + every page's source markdown),
    so an LLM can ingest everything in a single fetch. Same sources as the rendered site."""
    parts = [LLMS_INTRO.strip(), ""]
    for out, src, nav_title, *_ in PAGES:
        parts += ["\n---\n", f"# {nav_title}", f"Source: {BASE}{linkify(out)}", "",
                  de_emdash((REPO / src).read_text(encoding="utf-8")).strip()]
    (OUT / "llms-full.txt").write_text("\n".join(parts) + "\n", encoding="utf-8")
    print("  -> llms-full.txt")


def verify_jsonld():
    """Build-time parse-gate: every inline ld+json block in every built page must be valid JSON
    (catches the whole class of interpolation/escaping bugs offline, since we can't hit Google's
    Rich Results Test here)."""
    n = 0
    for html_file in sorted(OUT.glob("*.html")):
        text = html_file.read_text(encoding="utf-8")
        for block in re.findall(r'(?s)<script type="application/ld\+json">(.*?)</script>', text):
            block = block.replace("<\\/", "</")  # undo the script-safe escaping before parsing
            try:
                json.loads(block)
                n += 1
            except json.JSONDecodeError as e:
                raise SystemExit(f"build_docs: invalid JSON-LD in {html_file.name}: {e}")
    print(f"  -> verified {n} JSON-LD blocks parse")


def main():
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "codehilite", "toc", "sane_lists", "md_in_html"],
        extension_configs={
            "codehilite": {"guess_lang": False},
            "toc": {"permalink": "#", "permalink_class": "anchor",
                    "permalink_title": "Link to this section", "toc_depth": "2-3"},
        },
    )
    search = []
    for meta in PAGES:
        out, src = meta[0], meta[1]
        md.reset()
        md_text = de_emdash((REPO / src).read_text(encoding="utf-8"))
        html = theme_swap_dashboard_imgs(rewrite_links(md.convert(md_text)))
        html = strip_readme_frontmatter(html) if out == "docs.html" else strip_first_h1(html)
        # the rendered page marks outbound links (↗ + new tab); the search index is built from
        # `html` (pre-mark) so the ↗ glyph never leaks into snippets.
        (OUT / out).write_text(shell(meta, mark_external(html), md.toc), encoding="utf-8")
        page_url, group, page_title = linkify(out), GROUP_OF.get(out, ""), meta[2]
        # page-level entry, then one entry per content section (heading + text, anchored)
        search.append({"url": page_url, "page": page_title, "title": page_title,
                       "group": group, "kind": "page", "text": _plain_text(html)[:1600]})
        for anchor, sec_title, sec_text in _sections(html):
            search.append({"url": f"{page_url}#{anchor}", "page": page_title, "title": sec_title,
                           "group": group, "kind": "section", "text": sec_text[:600]})
        print(f"  {src} -> {out}")
    (OUT / "404.html").write_text(make404(), encoding="utf-8")
    print("  -> 404.html")
    (OUT / "search-index.json").write_text(json.dumps(search, ensure_ascii=False), encoding="utf-8")
    print("  -> search-index.json")
    gen_sitemap()
    gen_llms()
    gen_llms_full()
    verify_jsonld()
    sync_assets()


if __name__ == "__main__":
    main()
