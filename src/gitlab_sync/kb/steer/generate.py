"""Render workspace-specific steering files from the knowledge graph.

All renderers are pure: ``(facts, ...) -> str``. ``workspace_facts`` gathers the
specifics (repos, languages, shared dependencies) from the store + shards; the
guardrails are generic and tool-agnostic.
"""

from __future__ import annotations

from collections import Counter

from ..store.shards import read_shard

# Managed-block markers. Steering markdown is wrapped between BEGIN/END so an
# existing user file is *enhanced* (our block appended/refreshed) rather than
# overwritten — content outside the block is never touched.
BEGIN = "<!-- BEGIN gitlab-sync (managed; this block is refreshed by `gitlab-sync steer`) -->"
END = "<!-- END gitlab-sync -->"
MARKER = BEGIN  # back-compat: presence marks gitlab-sync-managed content

GUARDRAILS = """\
## Guardrails (non-negotiable)

- **Cite, don't guess.** Every claim about a repo, API, file, or behavior must trace
  to a file you actually read this session — use the knowledge tools below to find
  and open it. No source means say so and go read it; do not invent.
- **Protect work in progress.** Never switch branches, reset, or discard uncommitted
  changes. Assume every checked-out branch is someone's live work.
- **Surgical changes.** Match the surrounding style; touch only what the task needs;
  no speculative refactors or drive-by rewrites.
- **Stop and ask** before destructive or irreversible actions, architecture
  decisions, scope expansion, or anything touching secrets, IAM, or production.
"""


def workspace_facts(store, store_dir) -> dict:
    """Gather workspace specifics from the store and per-repo shards."""
    repos = store.list_repos()
    langs: Counter = Counter()
    packages: Counter = Counter()
    per_repo = []
    for r in repos:
        shard = read_shard(store_dir, r.id)
        if shard is None:
            per_repo.append({"id": r.id, "nodes": 0, "langs": []})
            continue
        rlangs: Counter = Counter()
        for n in shard.nodes:
            if n.lang:
                langs[n.lang] += 1
                rlangs[n.lang] += 1
            if n.kind == "package":
                packages[n.name] += 1
        per_repo.append({"id": r.id, "nodes": len(shard.nodes),
                         "langs": [lang for lang, _ in rlangs.most_common(3)]})
    st = store.stats()
    return {
        "count": len(repos),
        "languages": [lang for lang, _ in langs.most_common()],
        "top_packages": [p for p, _ in packages.most_common(15)],
        "nodes": st.nodes,
        "edges": st.edges,
        "per_repo": sorted(per_repo, key=lambda d: d["nodes"], reverse=True),
    }


def _serve_cmd(config_path: str | None) -> str:
    return "gitlab-sync serve" + (f" --config {config_path}" if config_path else "")


def _repos(n: int) -> str:
    return f"{n} repository" if n == 1 else f"{n} repositories"


def _repo_lines(facts: dict, limit: int = 40) -> str:
    rows = facts["per_repo"][:limit]
    out = "\n".join(
        f"- `{r['id']}` — {r['nodes']} symbols"
        + (f" ({', '.join(r['langs'])})" if r["langs"] else "")
        for r in rows
    )
    if len(facts["per_repo"]) > limit:
        out += f"\n- … and {len(facts['per_repo']) - limit} more"
    return out or "- (none indexed yet — run `gitlab-sync index --workspace .`)"


def render_agents_md(facts: dict, *, config_path: str | None = None) -> str:
    langs = ", ".join(facts["languages"]) or "—"
    pkgs = ", ".join(f"`{p}`" for p in facts["top_packages"][:10]) or "—"
    return f"""# AGENTS.md — Workspace guide for AI coding agents

This directory mirrors **{_repos(facts['count'])}** in their original namespace
structure, each parked on its most active branch. A local knowledge graph
({facts['nodes']} symbols, {facts['edges']} relations) indexes them.

- **Languages:** {langs}
- **Most-shared dependencies:** {pkgs}

## Get context before grepping

A knowledge server exposes the graph over MCP — prefer it over brute-force search:

```
{_serve_cmd(config_path)}
```

Tools it provides: `search_code` (find symbols), `find_definition`, `find_callers`,
`find_dependents`, `shortest_path`, `graph_stats`, and — when embeddings are enabled
— `semantic_search` / `hybrid_search` for natural-language queries. Curated wiki
pages (if generated) live under the knowledge store's `wiki/`.

## Repositories

{_repo_lines(facts)}

{GUARDRAILS}"""


def render_claude_md(config_path: str | None = None) -> str:
    return f"""# CLAUDE.md

See @AGENTS.md for the workspace overview, the knowledge tools, and the guardrails —
they apply here verbatim.

The knowledge-graph MCP server is configured for this workspace in `.mcp.json`
(it runs `{_serve_cmd(config_path)}`). Query it before searching by hand.
"""


def render_windsurfrules(facts: dict, *, config_path: str | None = None) -> str:
    return f"""# Workspace rules (Windsurf / Devin)

This workspace mirrors {_repos(facts['count'])} with a local knowledge graph.
Reach it over MCP (`{_serve_cmd(config_path)}`, also in this workspace's MCP config)
and query it before grepping. See AGENTS.md for the full guide.

{GUARDRAILS}"""


def render_kiro_steering(facts: dict, *, config_path: str | None = None) -> str:
    return f"""# Workspace steering (Kiro)

{_repos(facts['count'])}, mirrored and indexed into a local knowledge graph reachable
over MCP (`{_serve_cmd(config_path)}`). Prefer graph queries over manual search; see
AGENTS.md for tools and repo list.

{GUARDRAILS}"""


def mcp_server_entry(config_path: str | None = None) -> dict:
    """The MCP server entry for this workspace (merged into .mcp.json)."""
    args = ["serve"]
    if config_path:
        args += ["--config", config_path]
    return {"command": "gitlab-sync", "args": args}
