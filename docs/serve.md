# Serve it to your editor (MCP)

The third layer. Once the [knowledge layer](knowledge-layer.md) is built, `contextlake serve`
exposes it as an **MCP server** — so any MCP client (Claude Code, Windsurf, Kiro, Cursor,
Postman, …) can query the graph directly instead of grepping.

**Most of it needs no model.** The graph tools work on their own — `search_code`,
`find_definition`, `find_callers`, `find_dependents`, `shortest_path`, `graph_stats`,
`repo_dependencies`, `repo_flow`, `repo_event_flow`, `blast_radius`, `who_knows`, `get_wiki`,
`get_readme`, `get_repo_brief`, `list_repos`, `get_repo_links`, `graph_health`. Only
`semantic_search` / `hybrid_search` need embeddings.

## The quick way: let contextlake wire your editors

From your workspace root:

```bash
contextlake steer --config ~/.contextlake/kb.toml
```

This writes the per-tool steering files so agents pick up the workspace context and the MCP
server natively:

- **`AGENTS.md`** (overview, the knowledge tools, and guardrails), a thin **`CLAUDE.md`** that
  imports it, **`.windsurfrules`**, and **`.kiro/steering/`**.
- A merged **`.mcp.json`** entry for the `contextlake serve` server.
- A generic library of **agent skills / workflows** (`.claude/skills/`, `.windsurf/workflows/`):
  investigate-root-cause, plan-before-coding, surgical-change, review-before-landing,
  ship-safely, use-knowledge-graph — a strong operating playbook even for a small-context model.

**It never corrupts your existing files.** If you already have an `AGENTS.md`, `CLAUDE.md`,
`.windsurfrules`, or `.kiro/steering`, your content is preserved and only a clearly-delimited
managed block is appended (and just that block is refreshed on re-runs). `.mcp.json` is merged
so your other servers stay; a skill file you wrote with the same name is kept as-is; custom
layers like `.devin/` are left untouched.

## Wiring it by hand

Claude Code:

```bash
claude mcp add contextlake-kb -- contextlake serve --config ~/.contextlake/kb.toml
```

Windsurf / Devin — add the same server in its MCP config (Cascade's *MCP Servers* panel, or
`~/.codeium/windsurf/mcp_config.json`):

```json
{
  "mcpServers": {
    "contextlake-kb": {
      "command": "contextlake",
      "args": ["serve", "--config", "~/.contextlake/kb.toml"]
    }
  }
}
```

## Once connected

Ask the agent things like *"where is `OrderService` defined?"*, *"who calls `charge`?"*, or
*"which repos depend on `shared-core`?"* and it calls the graph tools directly — you can even
have it draft wiki pages from the graph without the built-in `wiki` command.
