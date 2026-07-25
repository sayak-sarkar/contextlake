# Serve it to your editor (MCP)

The third layer. Once the [knowledge layer](knowledge-layer.md) is built, `contextlake serve`
exposes it as an **MCP server**, so any MCP client (Claude Code, Windsurf, VS Code, Kiro, Cursor,
Postman, …) can query the graph directly instead of grepping.

**Start with `ask`.** One tool, natural language: `ask("who calls charge_order")` /
`ask("what breaks if I change CatalogService")` / `ask("what extends BaseController")` /
`ask("explain the catalog-api")`. It classifies the question, routes it to the right
substrate below (definition / callers / dependents / subclasses / impact / owners /
explain / search), resolves the symbol or repo, and returns one labeled answer (graph
facts cited; `explain` returns advisory wiki prose, or the repo's grounded anatomy when
no wiki exists yet). An agent that would rather not choose among the tools can just `ask`.

**Most of it needs no model.** The underlying graph tools work on their own , 
`search_code`, `find_definition`, `find_callers`, `find_dependents`, `get_node`,
`get_neighbors`, `shortest_path`, `graph_stats`, `repo_dependencies`, `repo_flow`,
`repo_event_flow`, `blast_radius`, `who_knows`, `get_wiki`, `get_readme`,
`get_repo_brief`, `list_repos`, `get_repo_links`, `graph_health`, plus a `kb://stats`
resource with the store counts.

`semantic_search` / `hybrid_search` are the two exceptions: they register **only when
embeddings exist** (an `[embeddings]` section in `kb.toml` and a `contextlake embed`
run). Without that, the server starts fine and says so, the two tools are simply
absent from the tool list, and everything above still works.

## The quick way: let contextlake wire your editors

From your workspace root:

```bash
contextlake steer --config ~/.contextlake/kb.toml
```

This writes the per-tool steering files so agents pick up the workspace context and the MCP
server natively:

- **`AGENTS.md`** (overview, the knowledge tools, and guardrails), a thin **`CLAUDE.md`** that
  imports it, **`.windsurfrules`**, and **`.kiro/steering/`**.
- A merged **`.mcp.json`** entry for the `contextlake serve` server (Claude Code, Windsurf,
  Cursor, and other clients that read this file) and a merged **`.vscode/mcp.json`** entry for
  VS Code, which uses a different top-level key (`servers`, not `mcpServers`) — a distinct
  schema, so it gets its own file rather than reusing `.mcp.json`.
- A generic library of **agent skills / workflows** (`.claude/skills/`, `.windsurf/workflows/`):
  investigate-root-cause, plan-before-coding, surgical-change, review-before-landing,
  ship-safely, use-knowledge-graph, a strong operating playbook even for a small-context model.

**It never corrupts your existing files.** If you already have an `AGENTS.md`, `CLAUDE.md`,
`.windsurfrules`, or `.kiro/steering`, your content is preserved and only a clearly-delimited
managed block is appended (and just that block is refreshed on re-runs). `.mcp.json` and
`.vscode/mcp.json` are merged so your other servers stay; a skill file you wrote with the same
name is kept as-is; custom layers like `.devin/` are left untouched.

## Wiring it by hand

Claude Code:

```bash
claude mcp add contextlake-kb -- contextlake serve --config ~/.contextlake/kb.toml
```

Windsurf, add the same server in its MCP config (Cascade's *MCP Servers* panel, or
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

VS Code, in `.vscode/mcp.json` (note the `servers` key — a different schema from the
`mcpServers` files above; `contextlake steer` writes this automatically):

```json
{
  "servers": {
    "contextlake-kb": {
      "command": "contextlake",
      "args": ["serve", "--config", "~/.contextlake/kb.toml"]
    }
  }
}
```

**Devin is different: there's no repo file to wire.** Devin's MCP connections are configured at
the account/org level (`mcp.devin.ai`, with an API key and org header), not read from a file
committed to the repo it's working in — so contextlake cannot self-register as a Devin MCP
server the way it can for the clients above. Add `contextlake serve` there yourself, once, in
Devin's own MCP settings. What `contextlake steer` *does* give Devin (and any agent that reads
plain workspace context) is `AGENTS.md` — the portable part travels; the MCP wiring itself
doesn't.

## Once connected

Ask the agent things like *"where is `CatalogService` defined?"*, *"who calls `charge`?"*, or
*"which repos depend on `shared-core`?"* and it calls the graph tools directly, you can even
have it draft wiki pages from the graph without the built-in `wiki` command.
