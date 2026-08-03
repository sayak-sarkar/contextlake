# Serve it to your editor (MCP)

The third layer. Once the [knowledge layer](knowledge-layer.md) is built, `contextlake kb serve`
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
embeddings exist** (an `[embeddings]` section in `kb.toml` and a `contextlake kb embed`
run). Without that, the server starts fine and says so, the two tools are simply
absent from the tool list, and everything above still works.

## The quick way: let contextlake wire your editors

From your workspace root:

```bash
contextlake kb steer --config ~/.contextlake/kb.toml
```

This writes the per-tool steering files so agents pick up the workspace context and the MCP
server natively:

- **`AGENTS.md`** (overview, the knowledge tools, and guardrails), a thin **`CLAUDE.md`** that
  imports it, **`.windsurfrules`**, and **`.kiro/steering/`**.
- A merged **`.mcp.json`** entry for the `contextlake kb serve` server (Claude Code, Windsurf,
  Cursor, and other clients that read this file) and a merged **`.vscode/mcp.json`** entry for
  VS Code, which uses a different top-level key (`servers`, not `mcpServers`): a distinct
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
claude mcp add contextlake-kb -- contextlake kb serve --config ~/.contextlake/kb.toml
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

VS Code, in `.vscode/mcp.json` (note the `servers` key: a different schema from the
`mcpServers` files above; `contextlake kb steer` writes this automatically):

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

## Transports

`contextlake kb serve --transport <stdio|http|sse>` (default `stdio`):

- **`stdio`** — the default. The editor/agent spawns `contextlake kb serve` itself and talks to
  it over stdin/stdout; this is what `steer`-generated `.mcp.json`/`.vscode/mcp.json` entries use.
  No token, no network: the pipe belongs to the process that spawned it.
- **`http`** — Streamable HTTP, the MCP spec's current standard network transport (`--host`/
  `--port`, default `127.0.0.1:8765`). Point clients at `http://127.0.0.1:8765/mcp`, not the bare
  host:port: the endpoint is the `/mcp` path, and the root returns 404. Prefer this transport for
  any new remote/network wiring. **Authenticated** — see below.
- **`sse`** — the older HTTP+SSE transport from the 2024-11-05 MCP spec revision. The current spec
  marks it deprecated in favor of Streamable HTTP, but still guides servers to keep offering it
  for clients that haven't moved off it yet; contextlake follows that guidance rather than
  dropping it. Its endpoint is `http://127.0.0.1:8765/sse`. Use `sse` only if your client
  specifically requires it (some clients, e.g. Devin's custom-MCP-server setup, list SSE as a
  distinct, separate option from HTTP) — pick `http` first. Authenticated exactly like `http`.

### Authenticating the network transports

The graph answers with real file paths, symbol names, docstrings and owner identities, so the
socket transports do not serve it to anyone who connects.

**A bearer token, printed once to stderr at startup:**

```
$ contextlake kb serve --transport http
✓ MCP server on http://127.0.0.1:8765/mcp  (Ctrl-C to stop)
  Bearer token: <a fresh 43-character token>
  Clients must send: Authorization: Bearer <token>
  Pin a stable one across restarts with $CONTEXTLAKE_MCP_TOKEN.
```

Every request needs `Authorization: Bearer <token>`; without it the server answers `401`. The
token goes to stderr only — never to stdout, never to the log file — so it does not outlive the
process anywhere you did not put it.

**Pin it for a client config.** A fresh token per launch is fine when you copy it by hand and
useless when a config file has to hold it. Set `CONTEXTLAKE_MCP_TOKEN` and the server uses that
value instead of minting one (an empty or whitespace-only value is treated as unset, and a fresh
token is minted — it never turns authentication off):

```bash
export CONTEXTLAKE_MCP_TOKEN='pick-your-own-long-random-string'
contextlake kb serve --transport http
```

**Origin and Host are validated** on every request, as the MCP spec requires for HTTP transports:
a request whose `Origin` is not the bound host (or a loopback address) gets `403`, and one whose
`Host` does not name this server gets `421`. That is what stops a web page you visit from
reaching your loopback MCP server through DNS rebinding.

**Non-loopback binds must be opted into.** `--host` outside `127.0.0.1` / `localhost` / `::1` is
refused unless you pass `--allow-remote`, and prints a warning when you do:

```bash
contextlake kb serve --transport http --host 0.0.0.0        # refused, exits 1
contextlake kb serve --transport http --host 0.0.0.0 --allow-remote
```

Nothing here is encrypted in transit. For anything beyond your own machine, prefer an SSH tunnel
to a loopback bind, or put TLS in front of it. Note also that a wildcard bind (`0.0.0.0`) only
answers requests whose `Host` is a loopback name, because the Host check has no way to know which
address you meant — bind the address clients will actually name (`--host 192.0.2.10`).

**Devin is different: there's no repo file to wire.** Devin's MCP connections are configured at
the account/org level (`mcp.devin.ai`, with an API key and org header), not read from a file
committed to the repo it's working in, so contextlake cannot self-register as a Devin MCP
server the way it can for the clients above. Add `contextlake kb serve` there yourself, once, in
Devin's own MCP settings. What `contextlake kb steer` *does* give Devin (and any agent that reads
plain workspace context) is `AGENTS.md`: the portable part travels; the MCP wiring itself
doesn't.

## Once connected

Ask the agent things like *"where is `CatalogService` defined?"*, *"who calls `charge`?"*, or
*"which repos depend on `shared-core`?"* and it calls the graph tools directly, you can even
have it draft wiki pages from the graph without the built-in `wiki` command.
