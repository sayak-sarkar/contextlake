#!/usr/bin/env python3
"""Render contextlake's markdown docs into branded, cross-linked site pages.

Every page shares one template: a hero (layer eyebrow + title + subtitle +
Pebble accent), the doc body, an on-page TOC rail, and a Next-steps footer.
The eyebrows anchor each page to the Mirror -> Knowledge -> Serve spine."""
import re
import pathlib
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
     [("quickstart.html", "Quickstart"), ("usage.html", "Usage & config"),
      ("knowledge-layer.html", "Knowledge layer")]),
    ("quickstart.html", "QUICKSTART.md", "Quickstart", "Quickstart",
     "Start here · all three layers", "Install, bootstrap, and wire your editor, "
     "the whole Mirror -> Knowledge -> Serve path in a few minutes.",
     "pebble-doc.png",
     [("usage.html", "Usage & config"), ("knowledge-layer.html", "Knowledge layer")]),
    ("usage.html", "docs/usage.md", "Usage & config", "Usage & configuration",
     "Layer 1 · Mirror", "Every mirror command, configuration, branch safety, "
     "and scheduling, the reference for the Mirror layer.",
     "pebble-doc.png",
     [("knowledge-layer.html", "Knowledge layer"), ("internals.html", "Architecture")]),
    ("index-code-graph.html", "docs/index-code-graph.md", "Index the code graph",
     "Index the code graph",
     "Build your knowledge base", "Turn your mirrored repos into a queryable code graph: "
     "incremental indexing, and the full node and edge model across 14 languages, Terraform, "
     "SQL, and web topology.",
     "pebble-doc.png",
     [("connect-enrich.html", "Connect and enrich"), ("knowledge-layer.html", "Knowledge layer")]),
    ("connect-enrich.html", "docs/connect-enrich.md", "Connect and enrich", "Connect and enrich",
     "Build your knowledge base", "Link repos to their issues, docs, and designs, manage "
     "sources, and pull grounded external facts into the knowledge layer with query-driven "
     "enrichment.",
     "pebble-doc.png",
     [("semantic-search.html", "Semantic search"), ("knowledge-layer.html", "Knowledge layer")]),
    ("semantic-search.html", "docs/semantic-search.md", "Semantic search", "Semantic search",
     "Build your knowledge base", "Natural-language and hybrid graph-propagation retrieval: "
     "embed your code, tune the vector backend, and query across repos and languages.",
     "pebble-doc.png",
     [("generate-wiki.html", "Generate the wiki"), ("knowledge-layer.html", "Knowledge layer")]),
    ("generate-wiki.html", "docs/generate-wiki.md", "Generate the wiki", "Generate the wiki",
     "Build your knowledge base", "Turn the graph into grounded, council-verified prose per "
     "repo: searchable, enrichment-aware, with a provenance footer.",
     "pebble-doc.png",
     [("model-providers.html", "Model providers"), ("knowledge-layer.html", "Knowledge layer")]),
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
     [("dashboard.html", "Dashboard"), ("serve.html", "Serve (MCP)")]),
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
     [("benchmarks.html", "Benchmarks"), ("dashboard.html", "Dashboard")]),
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
     [("internals.html", "Architecture"), ("usage.html", "Usage & config")]),
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
    ("Get started", ["docs.html", "quickstart.html"]),
    ("Build your knowledge base", ["index-code-graph.html", "connect-enrich.html",
                                   "semantic-search.html", "generate-wiki.html",
                                   "model-providers.html", "bootstrap.html"]),
    ("Using contextlake", ["usage.html", "knowledge-layer.html", "dashboard.html",
                           "serve.html", "visualize.html", "ownership.html",
                           "benchmarks.html"]),
    ("Under the hood", ["internals.html", "storage.html"]),
    ("Writing style", ["style-guide.html", "style-guide-voice.html", "style-guide-structure.html",
                       "style-guide-formatting.html", "style-guide-reference.html"]),
    ("Brand", ["brand.html"]),
    ("Reference", ["cli-reference.html", "console-output.html", "changelog.html"]),
]
GROUP_OF = {out: g for g, outs in NAV_GROUPS for out in outs}
SUBTITLE_OF = {m[0]: m[5] for m in PAGES}
TITLES = {out: nav for out, _, nav, *_ in PAGES}

# "Next steps" are DERIVED from the reading order so every page is consistent: the next two
# pages in the docs sequence, wrapping at the end back to the top. Concise, uniform labels.
_NEXT_LABEL = {
    "docs.html": "Overview", "quickstart.html": "Quickstart", "usage.html": "Usage & config",
    "index-code-graph.html": "Index the code graph",
    "connect-enrich.html": "Connect and enrich",
    "semantic-search.html": "Semantic search",
    "generate-wiki.html": "Generate the wiki",
    "model-providers.html": "Model providers",
    "bootstrap.html": "Bootstrap and keep fresh",
    "cli-reference.html": "Command reference",
    "console-output.html": "Reading the console output",
    "knowledge-layer.html": "Knowledge layer", "dashboard.html": "Dashboard",
    "serve.html": "Serve (MCP)", "visualize.html": "Visualize the graph",
    "ownership.html": "Ownership and SMEs", "benchmarks.html": "Benchmarks",
    "internals.html": "Architecture", "storage.html": "Storage",
    "changelog.html": "Changelog",
    "style-guide.html": "Writing style",
    "style-guide-voice.html": "Voice and tone",
    "style-guide-structure.html": "Page types and structure",
    "style-guide-formatting.html": "Formatting",
    "style-guide-reference.html": "Word reference",
    "brand.html": "Brand overview",
}
_ORDER = [p[0] for p in PAGES]
NEXT_STEPS = {
    out: [(_ORDER[(i + k) % len(_ORDER)], _NEXT_LABEL[_ORDER[(i + k) % len(_ORDER)]])
          for k in (1, 2)]
    for i, out in enumerate(_ORDER)
}

GLYPH = '<img class="glyph" src="icon-64.png" width="28" height="28" alt="" aria-hidden="true">'
GH_MARK = ('<svg class="lmark" viewBox="0 0 24 24" fill="currentColor" width="15" height="15" '
           'aria-hidden="true"><path d="M12 2A10 10 0 0 0 8.8 21.5c.5.1.7-.2.7-.5v-1.7c-2.8.6-3.4-1.3-3.4-1.3-.5-1.2-1.1-1.5-1.1-1.5-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.5 2.3 1.1 2.9.8.1-.6.3-1.1.6-1.4-2.2-.2-4.6-1.1-4.6-5 0-1.1.4-2 1-2.7-.1-.3-.4-1.3.1-2.7 0 0 .8-.3 2.7 1a9.4 9.4 0 0 1 5 0c1.9-1.3 2.7-1 2.7-1 .5 1.4.2 2.4.1 2.7.6.7 1 1.6 1 2.7 0 3.9-2.3 4.8-4.6 5 .4.3.7.9.7 1.9v2.8c0 .3.2.6.7.5A10 10 0 0 0 12 2Z"/></svg>')
PYPI_MARK = ('<img src="pypi-logo.svg" class="lmark" width="15" height="13" alt="" style="vertical-align:-2px">')


def _btn(href, mark, label):
    return (f'<a class="icon-btn" href="{href}" aria-label="{label}" title="{label}" '
            f'rel="noopener" target="_blank">{mark}</a>')


GH_BTN = _btn("https://github.com/sayak-sarkar/contextlake", GH_MARK, "contextlake on GitHub")
PYPI_BTN = _btn("https://pypi.org/project/contextlake/", PYPI_MARK, "contextlake on PyPI")
FOOT_MARK = ('<img src="mark.png" width="28" height="28" alt="" aria-hidden="true" '
             'style="width:28px;height:28px;vertical-align:middle;margin-right:8px">')


def de_emdash(text: str) -> str:
    return text.replace(" — ", ", ").replace("—", ", ")


# Map each doc source to its built page by BOTH its full path and its bare basename, so
# cross-links written either way resolve — README uses `docs/foo.md`, sibling docs use a
# bare `foo.md`. Anchors (`foo.md#sec`) are preserved.
_LINK_TO_PAGE = {}
for _out, _src, *_rest in PAGES:
    _LINK_TO_PAGE[_src] = _out
    _LINK_TO_PAGE[_src.split("/")[-1]] = _out
    # README/PyPI links are absolute GitHub URLs (they must resolve on PyPI); map those
    # back to the local built page so the on-site nav stays on-site.
    _LINK_TO_PAGE[GH + _src] = _out


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
                return f'href="{_LINK_TO_PAGE[key]}{sep}{anchor}"'
        if href.startswith(("http", "#", "mailto:")):
            return m.group(0)
        if norm:  # a repo file with no built page → point at GitHub
            return f'href="{GH}{norm}{sep}{anchor}"'
        return m.group(0)
    return re.sub(r'href="([^"]+)"', repl, html)


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
            links.append(f'<a href="{out}"{cls}>{TITLES[out]}</a>')
        blocks.append(f'<h2>{group}</h2><nav aria-label="{group}">'
                      + "".join(links) + "</nav>")
    ext = f'<div class="ext"><div class="social-row">{GH_BTN}{PYPI_BTN}</div></div>'
    return '<aside class="side">' + "".join(blocks) + ext + "</aside>"


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
    cards = "".join(
        f'<a class="next-card" href="{href}"><span>{label}</span>'
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


def shell(meta, body, toc_html) -> str:
    out, _, nav_title, h_title, eyebrow, subtitle, pebble, _hand_links = meta
    links = NEXT_STEPS[out]  # derived from reading order — consistent across all pages
    url = f"{BASE}{out}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{nav_title} · contextlake docs</title>
<meta name="description" content="{subtitle}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="contextlake">
<meta property="og:title" content="{nav_title} · contextlake docs">
<meta property="og:description" content="{subtitle}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{BASE}og-card.jpg">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{nav_title} · contextlake docs">
<meta name="twitter:description" content="{subtitle}">
<meta name="twitter:image" content="{BASE}og-card.jpg">
<link rel="icon" type="image/png" sizes="32x32" href="icon-32.png">
<link rel="apple-touch-icon" href="icon-180.png">
<meta name="theme-color" content="#137A8B">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="docs.css">
</head>
<body>
<a class="skip" href="#doc">Skip to content</a>
<header><div class="nav">
  <a class="brand" href="index.html" aria-label="contextlake home">{GLYPH}contextlake</a>
  <span class="spacer"></span>
  <a class="navlink" href="docs.html">Docs</a>
  <span class="social-row">{GH_BTN}{PYPI_BTN}</span>
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
    <a href="index.html">Home</a><a href="changelog.html">Changelog</a>
    <span class="social-row">{GH_BTN}{PYPI_BTN}</span>
  </nav>
</div></footer>
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
  <a class="home" href="index.html" aria-label="contextlake home">
    <img src="icon-64.png" width="26" height="26" alt="">contextlake
  </a>
  <div class="content">
    <p class="eyebrow">404 · off the map</p>
    <h1>Lost in the fog</h1>
    <p class="sub">This page drifted off into the mist. <b>Pebble</b> can't find it
      down here either, but the way back to shore is just a click away.</p>
    <div class="actions">
      <a class="btn primary" href="index.html">Back to shore</a>
      <a class="btn ghost" href="docs.html">Read the docs</a>
    </div>
  </div>
</main>
</body>
</html>"""


def breadcrumbs(out: str) -> str:
    group = GROUP_OF.get(out, "")
    return (f'<nav class="crumbs" aria-label="Breadcrumb">'
            f'<a href="docs.html">Docs</a><span aria-hidden="true">/</span>'
            f'<span>{group}</span><span aria-hidden="true">/</span>'
            f'<span aria-current="page">{TITLES.get(out, "")}</span></nav>')


LLMS_INTRO = """# contextlake

> A local context layer for your AI tools: mirror your Git repositories, index them into a
> queryable knowledge graph + wiki, and serve it to AI editors over MCP. Offline-first, so
> agents answer from real source instead of guessing. Python CLI, published on PyPI.
"""


def gen_sitemap():
    """sitemap.xml, generated from PAGES so it never goes stale by hand."""
    urls = [f'<url><loc>{BASE}</loc><priority>1.0</priority></url>']
    for out, *_ in PAGES:
        pr = "0.9" if out == "docs.html" else "0.6"
        urls.append(f'<url><loc>{BASE}{out}</loc><priority>{pr}</priority></url>')
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
            parts.append(f"- [{TITLES[out]}]({BASE}{out}): {SUBTITLE_OF[out]}")
        parts.append("")
    parts += ["## Source\n",
              "- [GitHub repository](https://github.com/sayak-sarkar/contextlake)",
              "- [PyPI package](https://pypi.org/project/contextlake/)", ""]
    (OUT / "llms.txt").write_text("\n".join(parts), encoding="utf-8")
    print("  -> llms.txt")


def main():
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "codehilite", "toc", "sane_lists", "md_in_html"],
        extension_configs={
            "codehilite": {"guess_lang": False},
            "toc": {"permalink": "#", "permalink_class": "anchor",
                    "permalink_title": "Link to this section", "toc_depth": "2-3"},
        },
    )
    for meta in PAGES:
        out, src = meta[0], meta[1]
        md.reset()
        md_text = de_emdash((REPO / src).read_text(encoding="utf-8"))
        html = rewrite_links(md.convert(md_text))
        html = strip_readme_frontmatter(html) if out == "docs.html" else strip_first_h1(html)
        (OUT / out).write_text(shell(meta, html, md.toc), encoding="utf-8")
        print(f"  {src} -> {out}")
    (OUT / "404.html").write_text(make404(), encoding="utf-8")
    print("  -> 404.html")
    gen_sitemap()
    gen_llms()
    sync_assets()


if __name__ == "__main__":
    main()
