#!/usr/bin/env python3
"""Render contextlake's markdown docs into branded, cross-linked site pages.

Every page shares one template: a hero (band eyebrow + title + subtitle +
Pebble accent), the doc body, an on-page TOC rail, and a Next-steps footer.
Each eyebrow names the page's nav band, and only that: see NAV_GROUPS."""
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


def prune_orphan_pages():
    """Delete built pages whose source is no longer in PAGES.

    `site/` is generated and gitignored, so a page retired from PAGES leaves its old
    HTML sitting there, and `deploy.sh` copies the directory wholesale. Four pages
    (`bootstrap`, `ownership`, `storage`, `comparison`) were live on the published site
    with no source file behind them: unreachable from the nav, absent from the sitemap,
    and impossible to correct.

    Only files carrying the generated-docs marker are considered, so a hand-authored
    page cannot be swept up by a typo in PAGES.
    """
    keep = {out for out, *_ in PAGES} | {"404.html", "index.html"}
    for f in sorted(OUT.glob("*.html")):
        if f.name in keep:
            continue
        if 'class="prose"' not in f.read_text(encoding="utf-8", errors="replace"):
            continue  # not one of ours
        f.unlink()
        print(f"  pruned orphan page: {f.name} (no source in PAGES)")


def sync_assets():
    import shutil
    for f in SHARED_IMG:
        shutil.copy(REPO / "docs/img" / f, OUT / f)
    for f in SHARED_BRANDING:
        shutil.copy(REPO / "docs/branding" / f, OUT / f)
    shutil.copy(REPO / "docs/branding" / "pebble-peek-web.png", OUT / "pebble-peek.png")
    # The whole docs/img tree, structure preserved: `localise_images` rewrites the
    # markdown's absolute GitHub URLs to `img/<subpath>`, and a flat copy would let
    # cli/x.png and dashboard/x.png collide.
    img_out = OUT / "img"
    if img_out.exists():
        shutil.rmtree(img_out)
    shutil.copytree(REPO / "docs/img", img_out)
    # Reuse the copy the dashboard already vendors rather than adding a second one:
    # one file, one version, and the docs cannot drift from the product.
    shutil.copy(REPO / "src/contextlake/kb/dashboard/static/mermaid.min.js",
                OUT / "mermaid.min.js")
    print(f"  synced {len(SHARED_IMG) + len(SHARED_BRANDING) + 1} shared assets from docs/")

# out, src, nav title, hero title, layer eyebrow, subtitle, pebble accent, next-steps
PAGES = [
    ("docs.html", "README.md", "Overview", "contextlake",
     "Understand it", "A local context layer for your AI tools: mirror your repos, "
     "index them into a knowledge graph, and serve it over MCP.",
     "pebble-doc.png",
     [("explained.html", "contextlake, explained"), ("install.html", "Install and upgrade"),
      ("quickstart.html", "Quickstart")]),
    ("explained.html", "docs/explained.md", "contextlake, explained", "contextlake, explained",
     "Understand it", "What changes on your screen, the three layers underneath it, the design "
     "decisions and the alternatives they turned down, the confidence model, and the honest limits.",
     "pebble-doc.png",
     [("benchmarks.html", "Benchmarks"), ("install.html", "Install and upgrade")]),
    ("install.html", "docs/install.md", "Install and upgrade", "Install and upgrade",
     "Get started", "Every way to install contextlake, pip, uv, pipx, Docker, or a standalone "
     "binary, plus the extras table, upgrading safely, and a clean uninstall.",
     "pebble-doc.png",
     [("quickstart.html", "Quickstart"), ("troubleshooting.html", "Troubleshooting")]),
    ("quickstart.html", "QUICKSTART.md", "Quickstart", "Quickstart",
     "Get started", "Install, bootstrap, and wire your editor, "
     "the whole Mirror -> Knowledge -> Serve path in a few minutes.",
     "pebble-doc.png",
     [("usage.html", "Mirror repositories"), ("knowledge-layer.html", "Knowledge layer")]),
    ("usage.html", "docs/usage.md", "Mirror repositories", "Mirror repositories",
     "Build your knowledge base", "Mirror your Git repos locally and keep them fresh: fetch, "
     "clone, update, most-active branch, verify, and audit, with branch-safety guardrails.",
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
     [("generate-docs.html", "Generate documentation"),
      ("keep-fresh.html", "Bootstrap and keep it fresh")]),
    ("generate-docs.html", "docs/generate-docs.md", "Generate documentation",
     "Generate documentation",
     "Build your knowledge base", "An API reference straight from the graph, with every "
     "symbol's real call sites and no model involved.",
     "pebble-doc.png",
     [("model-providers.html", "Model providers"), ("keep-fresh.html", "Bootstrap and keep it fresh")]),
    ("model-providers.html", "docs/model-providers.md", "Model providers", "Model providers",
     "Reference", "The pluggable embeddings and wiki backends: auto, built-in "
     "CPU, Ollama, OpenAI, Anthropic, and agent-CLI, with data-sharing posture and setup.",
     "pebble-doc.png",
     [("generate-wiki.html", "Generate the wiki"), ("install.html", "Install and upgrade")]),
    ("keep-fresh.html", "docs/keep-fresh.md", "Bootstrap and keep it fresh",
     "Bootstrap and keep it fresh",
     "Operate it", "Run the whole pipeline in one command, schedule it, re-index on commit with "
     "a git hook, and watch an unattended run.",
     "pebble-doc.png",
     [("console-output.html", "Reading the console output"), ("troubleshooting.html", "Troubleshooting")]),
    ("knowledge-layer.html", "docs/knowledge-layer.md", "Knowledge layer", "Knowledge layer",
     "Build your knowledge base", "Turn the mirror into a queryable graph with search, a wiki, "
     "and connectors.",
     "pebble-doc.png",
     [("index-code-graph.html", "Index the code graph"), ("keep-fresh.html", "Bootstrap and keep it fresh")]),
    ("ask-the-graph.html", "docs/ask-the-graph.md", "Ask the graph", "Ask the graph",
     "Use it", "Search the graph from the terminal, trace what a change would break, and find "
     "who to ask: `kb query`, `kb impact`, and `kb owners`.",
     "pebble-doc.png",
     [("serve.html", "Serve (MCP)"), ("dashboard.html", "Dashboard")]),
    ("dashboard.html", "docs/dashboard.md", "Dashboard", "The dashboard",
     "Use it", "A guided tour of the local, offline-first dashboard: "
     "the fleet overview, per-repo anatomy, the architecture graph, blast radius, and "
     "generating a wiki.",
     "pebble-doc.png",
     [("knowledge-layer.html", "Knowledge layer"), ("serve.html", "Serve (MCP)")]),
    ("serve.html", "docs/serve.md", "Serve (MCP)", "Serve it to your editor",
     "Use it", "Expose the knowledge layer over MCP and wire your editors "
     "(Claude Code, Windsurf, Kiro) in one command.",
     "pebble-doc.png",
     [("dashboard.html", "Dashboard"), ("visualize.html", "Visualize the graph")]),
    ("visualize.html", "docs/visualize.md", "Visualize the graph", "Visualize the graph",
     "Use it", "Draw bounded, offline graph slices (`contextlake kb graph`) in any of 11 formats, "
     "HTML, DOT, JSON, GraphML, Cypher and six Mermaid diagram types, plus the composed "
     "namespace C4 diagram.",
     "pebble-doc.png",
     [("dashboard.html", "Dashboard"), ("serve.html", "Serve (MCP)")]),
    ("benchmarks.html", "docs/benchmarks.md", "Benchmarks", "What it actually saves",
     "Understand it", "Where the token, cost, and correctness impact of connecting the "
     "contextlake MCP to your AI coding tools comes from, new-code grounding first, "
     "plus search, maintenance, the caveats, and how to measure it on your own repos.",
     "pebble-doc.png",
     [("explained.html", "contextlake, explained"), ("install.html", "Install and upgrade")]),
    ("internals.html", "docs/internals.md", "Architecture and internals",
     "Architecture and internals",
     "Reference", "How all three layers work inside: the store on disk, concurrency, "
     "branch selection, versioning and staleness, and the two invariants.",
     "pebble-doc.png",
     [("explained.html", "contextlake, explained"), ("cli-reference.html", "Command reference")]),
    ("cli-reference.html", "docs/cli-reference.md", "Command reference",
     "contextlake command reference",
     "Reference", "Every contextlake command at a glance, with links to the page that "
     "documents each in depth.",
     "pebble-doc.png",
     [("console-output.html", "Reading the console output"), ("docs.html", "Overview")]),
    ("console-output.html", "docs/console-output.md", "Reading the console output",
     "Reading the console output",
     "Operate it", "Decode the progress bar, the status glyph vocabulary, the JSON logs, "
     "the stdout/stderr split, and the four exit codes.",
     "pebble-doc.png",
     [("cli-reference.html", "Command reference"), ("troubleshooting.html", "Troubleshooting")]),
    ("troubleshooting.html", "docs/troubleshooting.md", "Troubleshooting", "Troubleshooting",
     "Operate it", "Install and mirror problems that have actually been hit, each with the "
     "fix and the reason behind it.",
     "pebble-doc.png",
     [("install.html", "Install and upgrade"), ("cli-reference.html", "Command reference")]),
    ("changelog.html", "CHANGELOG.md", "Changelog", "Changelog",
     "Reference", "Release history for contextlake.",
     "pebble-doc.png",
     [("docs.html", "Overview"), ("quickstart.html", "Quickstart")]),
    ("style-guide.html", "docs/style-guide.md", "Writing style", "Documentation style guide",
     "Project", "The spirit, the checklist, and links to the focused pages: voice, "
     "structure, formatting, and the word reference.",
     "pebble-doc.png",
     [("style-guide-voice.html", "Voice and tone"), ("style-guide-structure.html", "Page types and structure")]),
    ("style-guide-voice.html", "docs/style-guide-voice.md", "Voice and tone", "Voice and tone",
     "Project", "Second person, present tense, warm and grounded: the voice defaults, "
     "word choice, and writing for every reader.",
     "pebble-doc.png",
     [("style-guide-structure.html", "Page types and structure"), ("style-guide-formatting.html", "Formatting")]),
    ("style-guide-structure.html", "docs/style-guide-structure.md", "Page types and structure",
     "Page types and structure",
     "Project", "The concept, how-to, reference, and tutorial page types, each with a "
     "fixed skeleton, and how to structure a page.",
     "pebble-doc.png",
     [("style-guide-formatting.html", "Formatting"), ("style-guide-reference.html", "Word reference")]),
    ("style-guide-formatting.html", "docs/style-guide-formatting.md",
     "Formatting", "Formatting, accessibility, and inclusive language",
     "Project", "Headings, lists, code, callouts, links, accessibility, and inclusive "
     "language: the mechanics that keep every page consistent.",
     "pebble-doc.png",
     [("style-guide-reference.html", "Word reference"), ("brand.html", "Brand overview")]),
    ("style-guide-reference.html", "docs/style-guide-reference.md", "Word reference",
     "Word and term reference",
     "Project", "The house-style decision cache, before and after rewrites, and the "
     "A-to-Z term reference.",
     "pebble-doc.png",
     [("brand.html", "Brand overview"), ("style-guide.html", "Writing style")]),
    ("brand.html", "docs/brand.md", "Brand overview", "Brand overview",
     "Project", "contextlake's brand in one page: essence, voice, the lake metaphor, Pebble, "
     "the palette, and the mark, with the full spec linked.",
     "pebble-doc.png",
     [("style-guide.html", "Writing style"), ("docs.html", "Overview")]),
    ("contributing-languages.html", "docs/contributing-languages.md", "Adding a language",
     "Adding a language",
     "Project", "The ordered contributor recipe for teaching contextlake a new source "
     "language: the nine edits, the parts to leave alone, and the commands that prove the "
     "grammar works.",
     "pebble-doc.png",
     [("index-code-graph.html", "Index the code graph"), ("style-guide.html", "Writing style")]),
]
TO_PAGE = {src: out for out, src, *_ in PAGES}
TO_GH = ["docs/releasing.md", "ROADMAP.md", "CONTRIBUTING.md", "BRANDING.md", "LICENSE"]

# Sidebar navigation, organized into labeled bands (ordered). Every PAGES `out` appears in
# exactly one band; the band heading reuses the existing `.side h2` styling.
#
# The bands are DEPTH bands, read top to bottom: understand it, install it, build the knowledge
# base, use what you built, keep it running, look a detail up. A stakeholder can stop after the
# first band, an operator after the second, and each band's first page is sufficient on its own.
# The five style-guide pages and the brand page are last, in one Project band: they are about how
# contextlake's own documentation is written, so a reader evaluating the tool should not scroll
# past them to reach the command reference.
#
# INVARIANT (checked below): every page's hero eyebrow equals its band name. One vocabulary, so
# the hero and the sidebar can never tell a reader two different things about where they are.
NAV_GROUPS = [
    ("Understand it", ["docs.html", "explained.html", "benchmarks.html"]),
    ("Get started", ["install.html", "quickstart.html", "configuration.html"]),
    ("Build your knowledge base", ["knowledge-layer.html", "usage.html", "index-code-graph.html",
                                   "connect-enrich.html", "semantic-search.html",
                                   "generate-wiki.html", "generate-docs.html"]),
    ("Use it", ["ask-the-graph.html", "serve.html", "dashboard.html", "visualize.html"]),
    ("Operate it", ["keep-fresh.html", "console-output.html", "troubleshooting.html"]),
    ("Reference", ["internals.html", "cli-reference.html", "model-providers.html",
                   "changelog.html"]),
    ("Project", ["style-guide.html", "style-guide-voice.html", "style-guide-structure.html",
                 "style-guide-formatting.html", "style-guide-reference.html", "brand.html",
                 "contributing-languages.html"]),
]
GROUP_OF = {out: g for g, outs in NAV_GROUPS for out in outs}
# Per-page-type hero accent: the learning-journey bands each get one brand hue (reused from the
# landing page's own step icons / CTA), so the hero eyebrow signals "where am I" at a glance --
# not a new illustration per page (that's new art, a hand-to-user decision, see
# `planning/design/brand-image-prompts.md`), just an existing-palette accent already meaningful
# elsewhere on the site. The meta bands (Reference/Project) aren't part of that journey, so they
# keep the original default (lake) rather than a color chosen for the sake of having one.
#
# "Operate it" shares "use"'s hue deliberately: the palette (tokens.css) carries exactly four
# accent hues and all four are spoken for, so a fifth would mean introducing a new brand color.
# That is a brand decision, not a docs one -- BRANDING.md is its system of record. Operating and
# using are the two adjacent post-build bands, so sharing is the least misleading pairing until
# someone decides otherwise.
GROUP_KIND = {
    "Get started": "start", "Build your knowledge base": "build",
    "Use it": "use", "Operate it": "use", "Understand it": "understand",
}
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

# Nav completeness and the one-eyebrow-vocabulary invariant, both checked at import for the same
# reason the next-steps check is: a page that slips out of the sidebar, or an eyebrow that
# contradicts it, is invisible in review and obvious to a reader.
_NAV_OUTS = [out for _g, outs in NAV_GROUPS for out in outs]
if len(_NAV_OUTS) != len(set(_NAV_OUTS)):
    raise SystemExit("build_docs: a page appears in more than one NAV_GROUPS band")
if set(_NAV_OUTS) != _VALID_OUT:
    _missing = sorted(_VALID_OUT - set(_NAV_OUTS))
    _extra = sorted(set(_NAV_OUTS) - _VALID_OUT)
    raise SystemExit(f"build_docs: NAV_GROUPS mismatch, missing {_missing}, unknown {_extra}")
for _p in PAGES:
    if _p[4] != GROUP_OF[_p[0]]:
        raise SystemExit(f"build_docs: {_p[0]} eyebrow {_p[4]!r} is not its band "
                         f"{GROUP_OF[_p[0]]!r}")

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


# GitHub's native alert syntax (`> [!NOTE]` etc.) is the markdown SOURCE OF TRUTH for callouts --
# it degrades to a plain blockquote on any renderer that doesn't know it and renders as a native
# alert box on github.com, so docs/*.md stay correct when browsed directly in the repo (and in
# llms-full.txt, built from this same raw source). Python-Markdown's `admonition` extension syntax
# (`!!! type`) is NOT GitHub-safe -- it renders as a literal `!!! type` paragraph followed by the
# indented body as a CODE BLOCK -- so it's never written to the .md source, only produced here as
# a pre-`md.convert` transform, the same seam `de_emdash` already uses.
_GH_ALERT = re.compile(r"(?m)^> \[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\n((?:^>.*\n?)+)")


def convert_github_alerts(text: str) -> str:
    def repl(m):
        kind = m.group(1).lower()
        body = []
        for line in m.group(2).splitlines():
            content = line[1:]  # drop the leading '>'
            if content.startswith(" "):
                content = content[1:]  # the conventional "> text" single space
            body.append(("    " + content) if content else "")
        return f"!!! {kind}\n" + "\n".join(body) + "\n"
    return _GH_ALERT.sub(repl, text)


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


_RAW_IMG = re.compile(
    r'src="https://raw\.githubusercontent\.com/[^/]+/[^/]+/main/docs/img/([^"]+)"')
# docs/branding/ is synced flat to the site root (see SHARED_BRANDING), and
# pebble-peek-web.png is renamed on the way, so these map by name rather than subpath.
_RAW_BRANDING = re.compile(
    r'src="https://raw\.githubusercontent\.com/[^/]+/[^/]+/main/docs/branding/([^"]+)"')
_BRANDING_RENAMES = {"pebble-peek-web.png": "pebble-peek.png"}


def localise_images(html: str) -> str:
    """Point every docs/img reference at the site's own copy.

    The markdown carries the absolute raw.githubusercontent URL on purpose: that is
    what makes an image render when someone reads the `.md` on github.com or on PyPI,
    where a repo-relative path resolves to nothing. On the SITE, though, the same URL
    is an external request per image, to a host the reader may not be able to reach.
    Behind a TLS-inspecting corporate proxy every one of them fails, which is how the
    published site ended up with broken images while every locally-served copy was
    fine.

    So the source stays absolute and the built page is rewritten to `img/<subpath>`,
    with `sync_assets` copying the tree in beside it. The site then depends on no
    external host for its own pictures, which is also what it already claims about
    the graph embed.
    """
    html = _RAW_IMG.sub(r'src="img/\1"', html)
    return _RAW_BRANDING.sub(
        lambda m: 'src="%s"' % _BRANDING_RENAMES.get(m.group(1), m.group(1)), html)


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


def hero(title: str, eyebrow: str, subtitle: str, pebble: str, kind: str = "") -> str:
    attr = f' data-kind="{kind}"' if kind else ""
    return (f'<header class="doc-hero"{attr}>'
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
document.querySelectorAll(".prose pre:not(.mermaid)").forEach(function(pre){if(pre.querySelector(".copy-btn"))return;
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
    # Case-insensitive, and tolerant of space before the closing bracket. NOT a security
    # boundary: the input is this repo's own rendered Markdown, and `cmdk.js` escapes every
    # index field before it reaches innerHTML. So this is correctness only, and a block that
    # slipped past would leak its BODY into the search index as noise rather than as markup,
    # because the next line strips all tags regardless of case. Written this way because the
    # identical two weaknesses in a test helper drew a CodeQL `py/bad-tag-filter` alert, and
    # `re.I` alone does not close them: plain lowercase `</script >` escapes the intolerant
    # form as well.
    t = re.sub(r"(?s)<(script|style)\b.*?</\1\s*>", " ", t, flags=re.I)
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


def _mermaid_fence(source, language, css_class, options, md, **kw) -> str:
    """Render a ```mermaid fence as <pre class="mermaid"> for the browser to draw.

    The source is emitted verbatim rather than escaped: mermaid parses its own
    text, and escaping `-->` breaks every edge. That is safe here because every
    page is built from a `.md` file in this repository, so the fence content is
    ours. It would NOT be safe for user-supplied markdown.

    github.com renders the same fence natively, so a diagram stays readable in the
    source tree and in a pull request, which is the reason to author diagrams as
    fences instead of committing SVGs.
    """
    return f'<pre class="mermaid">{source}</pre>'


MERMAID_JS = (
    '<script src="mermaid.min.js"></script>\n'
    '<script>(function(){\n'
    '  if(!window.mermaid) return;\n'
    '  // Read the real tokens from tokens.css and docs.css so a diagram follows the\n'
    '  // theme toggle and a palette change for free. Every name below is one that\n'
    '  // actually exists: an absent custom property returns "" and would silently\n'
    '  // fall back, which is how a diagram ends up off-brand and nobody notices.\n'
    '  var read = function(){\n'
    '    var cs = getComputedStyle(document.documentElement);\n'
    '    var v = function(n, d){ return (cs.getPropertyValue(n) || d).trim(); };\n'
    '    return {\n'
    '      background:         v("--surface", "#ffffff"),\n'
    '      primaryColor:       v("--dg-step", "#dceaef"),\n'
    '      primaryBorderColor: v("--dg-step-line", "#137A8B"),\n'
    '      primaryTextColor:   v("--ink", "#0E2A33"),\n'
    '      secondaryColor:     v("--dg-store", "#f3ead5"),\n'
    '      tertiaryColor:      v("--dg-ext", "#e9f1f2"),\n'
    '      lineColor:          v("--muted", "#41606a"),\n'
    '      textColor:          v("--ink", "#0E2A33"),\n'
    '      fontFamily:         v("--ff", "system-ui, sans-serif"),\n'
    '      fontSize:           "14px"\n'
    '    };\n'
    '  };\n'
    '  mermaid.initialize({ startOnLoad: true, securityLevel: "strict",\n'
    '                       maxEdges: 2000, theme: "base", themeVariables: read() });\n'
    '  // Re-draw on a theme change. mermaid replaces the <pre>\'s content with SVG,\n'
    '  // so the source has to be kept to redraw from; without this the diagrams stay\n'
    '  // in the old palette until a reload.\n'
    '  document.querySelectorAll("pre.mermaid").forEach(function(el){\n'
    '    el.dataset.src = el.textContent;\n'
    '  });\n'
    '  var redraw = function(){\n'
    '    document.querySelectorAll("pre.mermaid").forEach(function(el){\n'
    '      el.removeAttribute("data-processed");\n'
    '      el.textContent = el.dataset.src || el.textContent;\n'
    '    });\n'
    '    mermaid.initialize({ startOnLoad: false, securityLevel: "strict",\n'
    '                         maxEdges: 2000, theme: "base", themeVariables: read() });\n'
    '    mermaid.run({ querySelector: "pre.mermaid" });\n'
    '  };\n'
    '  new MutationObserver(redraw).observe(document.documentElement,\n'
    '    { attributes: true, attributeFilter: ["data-theme"] });\n'
    '})();</script>'
)


def shell(meta, body, toc_html) -> str:
    out, src, nav_title, h_title, eyebrow, subtitle, pebble, hand_links = meta
    # 3.5 MB of vendored mermaid: only the pages that draw something pay for it.
    MERMAID = MERMAID_JS if 'class="mermaid"' in body else ""
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
<link rel="stylesheet" href="tokens.css">
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
    {hero(h_title, eyebrow, subtitle, pebble, GROUP_KIND.get(GROUP_OF.get(out), ""))}
    {body}
    {next_steps(links)}
  </main>
  {toc_rail(toc_html)}
</div>
<footer><div class="f-in">
  <span class="tagline">{FOOT_MARK}Deep context. Clear answers.</span>
  <nav class="f-links" aria-label="Footer">
    <a href="./">Home</a><a href="demo/">Live demo</a><a href="changelog">Changelog</a>{FOOT_GH}{FOOT_PYPI}
  </nav>
</div></footer>
{THEME_JS}
{COPY_JS}
{MERMAID}
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
        extensions=["tables", "pymdownx.superfences", "codehilite", "toc", "sane_lists",
                    "md_in_html", "admonition"],
        extension_configs={
            "pymdownx.superfences": {"custom_fences": [
                {"name": "mermaid", "class": "mermaid", "format": _mermaid_fence},
            ]},
            "codehilite": {"guess_lang": False},
            "toc": {"permalink": "#", "permalink_class": "anchor",
                    "permalink_title": "Link to this section", "toc_depth": "2-3"},
        },
    )
    search = []
    for meta in PAGES:
        out, src = meta[0], meta[1]
        md.reset()
        md_text = convert_github_alerts(de_emdash((REPO / src).read_text(encoding="utf-8")))
        html = localise_images(theme_swap_dashboard_imgs(rewrite_links(md.convert(md_text))))
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
    prune_orphan_pages()
    gen_sitemap()
    gen_llms()
    gen_llms_full()
    verify_jsonld()
    sync_assets()


if __name__ == "__main__":
    main()
