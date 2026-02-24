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
    "APIs, files, or behavior; if a fact is not given, omit it. Be concise."
)


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
        "top_symbols": [(n.kind, n.name, n.file) for n in top],
        "packages": [n.name for n in nodes if n.kind == "package"][:20],
        "files": sorted({n.file for n in nodes if n.file})[:20],
    }


def render_prompt(brief: dict) -> str:
    lines = [
        f"Repository: {brief['repo']}",
        f"Indexed commit: {brief['head']}",
        f"{brief['node_count']} symbols, {brief['edge_count']} relations.",
        f"Languages: {brief['langs']}",
        f"Symbol kinds: {brief['kinds']}",
        "Key symbols (kind, name, file):",
    ]
    lines += [f"  - {kind} {name} ({f or '?'})" for kind, name, f in brief["top_symbols"]]
    if brief["packages"]:
        lines.append("Depends on packages: " + ", ".join(brief["packages"]))
    if brief["files"]:
        lines.append("Notable files: " + ", ".join(brief["files"]))
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
