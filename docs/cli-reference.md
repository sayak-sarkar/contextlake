# `contextlake` command reference

Every command has scoped help via `contextlake <command> --help`. This page is the at-a-glance map; each
command's own page (linked below) covers it in depth.

## Knowledge-layer commands

| Command | What it does |
| --- | --- |
| `source` | add / list / remove / test / enable / disable knowledge-source connectors |
| `index` | Build the code/dependency graph (`--workspace`, incremental, `--watch`) |
| `connect` | Link repos to Atlassian / Figma / GitLab items (`--watch` to keep refreshing) |
| `enrich` | Query connected sources with codebase-derived terms and store enrichment docs (`--workspace`, incremental) |
| `embed` | Build semantic-search vectors (zero-config built-in CPU model, Ollama, or an API; incremental, `--watch`) |
| `ingest` | Aggregate external docs into the graph + semantic store (built-in `files`/`web`/`api`/`mcp` sources, or plugins) |
| `wiki` | LLM-synthesized, council-verified wiki pages (per-repo, or a cluster page with `--namespace <prefix>` / `--namespaces --depth N`); `--llm builtin\|ollama\|openai\|anthropic\|cli` enables the LLM tier inline |
| `query` | Search the index (`--kind`, `--repo`, `--as-of <commit>`) |
| `owners` | Likely owners / SMEs for a repo or path, ranked from git history (alias `who-knows`) |
| `impact` | Change-impact / blast radius: what depends on a symbol (alias `blast-radius`) |
| `graph` | Visualize the graph, offline interactive HTML / DOT / Mermaid / JSON, or a composed namespace C4 diagram with `--c4` |
| `dashboard` | Local knowledge-system dashboard UI (`--serve`; `--sample` for a bundled demo) |
| `eval` | Measure retrieval quality: precision / recall / MRR against a golden-query set |
| `lint` | Graph health audit: stale repos, dangling edges |
| `doctor` | Environment check: FTS5, git, glab, the store, embeddings, per-source reachability |
| `bootstrap` | Run the whole pipeline end to end (sync, index, connect, embed, enrich, wiki, steer) |
| `serve` | Expose the graph over MCP (stdio or `--transport http`) |
| `steer` | Write per-editor steering (`AGENTS.md`, `.mcp.json`, and so on) |

The mirror-tier commands (`fetch`, `clone`, `update`, `branches`, `verify`, `sync`, `status`, `audit`)
are covered under [Usage and configuration](usage.md).

## See also

- [Index the code graph](index-code-graph.md)
- [Serve it to your editor](serve.md)
- [Reading the console output](console-output.md)
