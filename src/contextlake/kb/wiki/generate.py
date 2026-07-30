"""Generate a curated wiki page for a repo from its knowledge graph.

A page is grounded strictly in facts extracted from the repo's shard (top symbols
by degree, kinds, languages, packages, files) so the model summarizes rather than
invents. Every page ends with a provenance footer citing the commit and sources.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

from ..store.shards import read_shard

# Conventional entry-point/config filenames -- presence-only signal for the
# "Setup & Run" section (never file contents beyond the README excerpt below,
# so there's no hallucination surface: the model is told these files exist,
# not what's in them).
_SETUP_FILENAMES = {
    "package.json", "pyproject.toml", "dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "manage.py", "main.py", "__main__.py",
    "program.cs", "makefile",
}

def _grounding_cap(node_count: int) -> int:
    """How many symbols to sample into the wiki prompt's ranked lists.

    Grows with repo size (a fixed 15 is ~0.03% of a 50k-node repo) but stays
    bounded -- more symbols means a longer, more expensive LLM call, so this is
    a deliberate depth-vs-cost tradeoff, not a free win.
    """
    return max(15, min(80, node_count // 1500))


SYSTEM = (
    "You are a precise staff engineer writing a short wiki page about a code "
    "repository for other engineers. Use ONLY the facts provided. Do not invent "
    "APIs, files, or behavior; if a fact is not given, omit it. Be concise. "
    "External context from connected sources must always be attributed to its "
    "source when used; never present it as a fact about the code."
)


def external_context(
    store_dir, repo_id: str, *, max_items: int = 8, max_chars: int = 300
) -> list[dict]:
    """Cited snippets from ``repo_id``'s connector-enrichment documents (the
    ``@enrich:<repo_id>`` partition), or ``[]`` if it hasn't been enriched.

    Each item carries its source (issue tracker, docs, design tool, …), title,
    and uri so the wiki prompt can attribute it rather than presenting it as a
    fact about the code.
    """
    from ..connectors.enrich import enrich_partition

    shard = read_shard(store_dir, enrich_partition(repo_id))
    if shard is None:
        return []
    items = []
    for n in shard.nodes:
        if n.kind != "document":
            continue
        snippet = " ".join(((n.attrs or {}).get("snippet") or "").split())[:max_chars]
        title = (n.name or "").strip()
        uri = (n.file or "").strip()
        if not snippet and not title:
            continue
        items.append({
            "source": (n.attrs or {}).get("source"),
            "title": title,
            "uri": uri,
            "snippet": snippet,
        })
        if len(items) >= max_items:
            break
    return items


def _symbol_row(n, *, count: int | None = None) -> dict:
    row = {"kind": n.kind, "name": n.name, "file": n.file,
           "doc": (n.attrs or {}).get("doc"),
           "signature": (n.attrs or {}).get("signature")}
    if count is not None:
        row["count"] = count
    return row


def _is_setup_filename(base: str) -> bool:
    base = base.lower()
    return base in _SETUP_FILENAMES or base.startswith("readme") or base.endswith(".csproj")


def _setup_signals(all_files: set, store=None, repo_id: str | None = None) -> list[str]:
    """Which conventional entry-point/config filenames exist.

    Two sources, merged: (1) every file already in the shard (not the
    truncated 20-file sample below) -- catches source-tree entry points like
    ``main.py``/``manage.py``/``Program.cs``, which tree-sitter indexes like
    any other source file; (2) a top-level-only listing of the live checkout,
    when ``store`` is given -- catches config/manifest files (``package.json``,
    ``Dockerfile``, ``pyproject.toml``, ...) that aren't source code and so
    never become graph nodes at all. Same store-optional, degrade-to-nothing
    guard as :func:`_readme_excerpt` -- omit ``store`` and you just get (1).
    """
    found = {f for f in all_files if _is_setup_filename(f.rsplit("/", 1)[-1])}
    if store is not None and repo_id is not None:
        r = store.get_repo(repo_id)
        base = Path(r.path) if r and getattr(r, "path", None) else None
        if base and base.is_dir():
            for entry in base.iterdir():
                if entry.is_file() and _is_setup_filename(entry.name):
                    found.add(entry.name)
    return sorted(found)[:20]


def _readme_excerpt(store, repo_id: str, *, max_chars: int = 2000) -> str | None:
    """First ``max_chars`` of the repo's own README, read from its live checkout.

    Degrades to ``None`` (never raises) when ``store`` is omitted or the
    checkout is missing/moved -- mirrors ``dashboard.data._readme_html``'s
    guard for the exact same reason.
    """
    if store is None:
        return None
    r = store.get_repo(repo_id)
    base = Path(r.path) if r and getattr(r, "path", None) else None
    if not base or not base.is_dir():
        return None
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        f = base / name
        if f.is_file():
            return f.read_text(encoding="utf-8", errors="replace")[:max_chars]
    return None


def repo_brief(store_dir, repo_id: str, *, store=None) -> dict | None:
    """Salient, grounded facts about a repo, or None if it has no shard.

    ``store`` is optional and only used for the ``readme_excerpt`` field (a
    live-checkout filesystem read, unlike everything else here which comes
    from the indexed shard alone) -- omit it and the field is simply ``None``.
    """
    shard = read_shard(store_dir, repo_id)
    if shard is None:
        return None
    nodes = shard.nodes
    by_id = {n.id: n for n in nodes}
    degree: Counter = Counter()
    in_degree: Counter = Counter()   # callers -- a hub, worth protecting with tests
    out_degree: Counter = Counter()  # callees -- a dispatcher, where behavior branches
    for e in shard.edges:
        degree[e.src] += 1
        degree[e.dst] += 1
        in_degree[e.dst] += 1
        out_degree[e.src] += 1
    cap = _grounding_cap(len(nodes))
    top = [by_id[i] for i, _ in degree.most_common(cap) if i in by_id]
    all_files = {n.file for n in nodes if n.file}
    return {
        "repo": repo_id,
        "head": shard.head_commit,
        "node_count": len(nodes),
        "edge_count": len(shard.edges),
        "kinds": dict(Counter(n.kind for n in nodes)),
        "langs": dict(Counter(n.lang for n in nodes if n.lang)),
        "top_symbols": [_symbol_row(n) for n in top],
        # Split combined-degree ranking above into fan-in/fan-out separately --
        # the dashboard's own risk view (Anatomy tab's hotspots section), not
        # folded into top_symbols so existing consumers of that field are
        # unaffected.
        "hubs": [_symbol_row(by_id[i], count=c)
                for i, c in in_degree.most_common(cap) if i in by_id],
        "dispatchers": [_symbol_row(by_id[i], count=c)
                        for i, c in out_degree.most_common(cap) if i in by_id],
        "packages": [n.name for n in nodes if n.kind == "package"][:20],
        "files": sorted(all_files)[:20],
        "decisions": [{"title": n.name, "file": n.file,
                       "doc": (n.attrs or {}).get("doc")}
                      for n in nodes if n.kind == "adr"][:20],
        "external": external_context(store_dir, repo_id),
        "readme_excerpt": _readme_excerpt(store, repo_id),
        "setup_signals": _setup_signals(all_files, store, repo_id),
    }


def render_prompt(brief: dict) -> str:
    lines = [
        f"Repository: {brief['repo']}",
        f"Indexed commit: {brief['head']}",
        f"{brief['node_count']} symbols, {brief['edge_count']} relations.",
        f"Languages: {brief['langs']}",
        f"Symbol kinds: {brief['kinds']}",
        "Key symbols (kind, name, file — with signature/docstring where known):",
    ]
    for t in brief["top_symbols"]:
        sig = t.get("signature") or ""
        line = f"  - {t['kind']} {t['name']}{sig} ({t.get('file') or '?'})"
        if t.get("doc"):
            line += f" — {t['doc'][:160]}"
        lines.append(line)
    if brief["packages"]:
        lines.append("Depends on packages: " + ", ".join(brief["packages"]))
    if brief["files"]:
        lines.append("Notable files: " + ", ".join(brief["files"]))
    if brief.get("readme_excerpt") or brief.get("setup_signals"):
        lines.append("")
        lines.append("Setup/run signal (from the repo's own files):")
        if brief.get("setup_signals"):
            lines.append("  Entry-point/config files present: "
                         + ", ".join(brief["setup_signals"]))
        if brief.get("readme_excerpt"):
            lines.append("  From the repo's own README:")
            lines.append(f"  \"{brief['readme_excerpt']}\"")
    if brief.get("hubs"):
        lines.append("")
        lines.append("Most-depended-on symbols (highest caller count in the graph — "
                     "treat changes to these with extra care):")
        for h in brief["hubs"][:8]:
            lines.append(f"  - {h['kind']} {h['name']} ({h.get('file') or '?'}), "
                         f"{h['count']} caller(s)")
    if brief.get("decisions"):
        lines.append("")
        lines.append("Recorded decisions (from the repo's own ADR/decision docs, "
                     "authored facts, not to be reworded as speculation):")
        for d in brief["decisions"]:
            doc = (d.get("doc") or "")[:200]
            lines.append(f"  - {d['title']} ({d.get('file') or '?'}): \"{doc}\"")
    if brief.get("external"):
        lines.append("")
        lines.append("External context (from connected sources):")
        for item in brief["external"]:
            lines.append(
                f"  - [source: {item.get('source')}] {item.get('title')} "
                f"({item.get('uri')}): \"{item.get('snippet')}\""
            )
        lines.append(
            "The External context items come from connected sources (issue trackers, "
            "docs, design tools). You MAY use them to enrich the page, but you MUST "
            "attribute each such statement to its source (name the source/link). "
            "Never present external claims as facts about the code without attribution."
        )
    sections = "Overview"
    if brief.get("readme_excerpt") or brief.get("setup_signals"):
        sections += ", Setup & Run"
    sections += ", Architecture, Dependencies"
    if brief.get("hubs"):
        sections += ", Gotchas"
    if brief.get("decisions"):
        sections += ", Decisions"
    lines += [
        "",
        f"Write a wiki page in Markdown with sections: {sections}, in that order. "
        "Ground every statement in the facts above; do not speculate. Omit a section "
        "entirely if the facts above give you nothing to say for it — do not write a "
        "heading with no content.",
    ]
    return "\n".join(lines)


def provenance_footer(brief: dict, verified_at: date | None = None) -> str:
    cites = ", ".join(f"`{f}`" for f in brief["files"][:10])
    return (
        "\n\n---\n"
        f"*Generated from the knowledge graph of `{brief['repo']}` at commit "
        f"`{brief['head']}` on {verified_at or date.today()}."
        + (f" Sources: {cites}." if cites else "")
        + "*"
    )


def generate_page(llm, store_dir, repo_id: str, *, verified_at: date | None = None,
                  store=None) -> str | None:
    """Generate a provenance-stamped wiki page (Markdown), or None without a shard."""
    brief = repo_brief(store_dir, repo_id, store=store)
    if brief is None:
        return None
    body = llm.generate(render_prompt(brief), system=SYSTEM).strip()
    return f"# {repo_id}\n\n{body}{provenance_footer(brief, verified_at)}"
