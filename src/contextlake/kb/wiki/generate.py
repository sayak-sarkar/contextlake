"""Generate a curated wiki page for a repo from its knowledge graph.

A page is grounded strictly in facts extracted from the repo's shard (top symbols
by degree, kinds, languages, packages, files) so the model summarizes rather than
invents. Every page ends with a provenance footer citing the commit and sources.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from ..store.shards import read_shard

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


def repo_brief(store_dir, repo_id: str) -> dict | None:
    """Salient, grounded facts about a repo, or None if it has no shard."""
    shard = read_shard(store_dir, repo_id)
    if shard is None:
        return None
    nodes = shard.nodes
    by_id = {n.id: n for n in nodes}
    degree: Counter = Counter()
    for e in shard.edges:
        degree[e.src] += 1
        degree[e.dst] += 1
    top = [by_id[i] for i, _ in degree.most_common(15) if i in by_id]
    return {
        "repo": repo_id,
        "head": shard.head_commit,
        "node_count": len(nodes),
        "edge_count": len(shard.edges),
        "kinds": dict(Counter(n.kind for n in nodes)),
        "langs": dict(Counter(n.lang for n in nodes if n.lang)),
        "top_symbols": [{"kind": n.kind, "name": n.name, "file": n.file,
                         "doc": (n.attrs or {}).get("doc"),
                         "signature": (n.attrs or {}).get("signature")} for n in top],
        "packages": [n.name for n in nodes if n.kind == "package"][:20],
        "files": sorted({n.file for n in nodes if n.file})[:20],
        "external": external_context(store_dir, repo_id),
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
    lines += [
        "",
        "Write a wiki page in Markdown with sections: Overview, Key components, "
        "Dependencies. Ground every statement in the facts above; do not speculate.",
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


def generate_page(llm, store_dir, repo_id: str, *, verified_at: date | None = None) -> str | None:
    """Generate a provenance-stamped wiki page (Markdown), or None without a shard."""
    brief = repo_brief(store_dir, repo_id)
    if brief is None:
        return None
    body = llm.generate(render_prompt(brief), system=SYSTEM).strip()
    return f"# {repo_id}\n\n{body}{provenance_footer(brief, verified_at)}"
