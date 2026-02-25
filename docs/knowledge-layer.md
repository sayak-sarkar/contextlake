# Knowledge layer

An optional subsystem (`contextlake.kb`) turns your mirrored repositories into a
queryable **knowledge graph** and serves it to AI agents over **MCP** — so an
assistant can ask "where is `X` defined?", "who calls `Y`?", or "which repos
depend on package `Z`?" instead of grepping hundreds of repos. It's generic: it
indexes *any* repositories and connects to *any* configured knowledge sources; no
organization-specific data lives in the package (your sites, keys, and rules go in
a private config file).

Install the extra (requires Python ≥ 3.10):

```bash
pip install "contextlake[kb]"
contextlake doctor                          # check the environment
contextlake index --source ./my-repo        # index one repository
contextlake index --workspace ~/work        # index every git repo (incremental; --force to rebuild)
contextlake connect --workspace ~/work      # link repos to their issues/docs (see below)
contextlake embed                           # build semantic vectors (optional, see below)
contextlake lint                            # graph health: stale repos + dangling edges
contextlake wiki                            # LLM-synthesized, council-verified wiki pages (optional)
contextlake steer                           # write per-tool steering: AGENTS.md, .mcp.json, …
contextlake query "OrderService"            # cited search across the index
contextlake serve                           # expose the graph over MCP (stdio or --transport http)
```

`index --workspace` is **incremental** — it re-indexes only repos whose git HEAD
moved since their last index, so a scheduled (cron) run stays cheap; pass `--force`
to rebuild everything, or `--watch [--interval N]` to keep re-indexing in a loop.
Every indexed snapshot is kept, so `query "<text>" --repo R --as-of <commit>` does
**time-travel** — it searches repo `R` as it was at a previously-indexed commit.

**Health & maintenance.** `contextlake doctor` is a quick environment check — SQLite
FTS5, `git`/`glab` on `PATH`, the store's reachability and counts, and the embeddings
status — and exits non-zero if something's wrong. `contextlake lint` audits the graph
itself, reporting **stale repos** (HEAD moved since they were indexed, so the index is
behind) and **dangling edges** (an edge whose endpoint node is missing); it exits
non-zero when it finds problems, so it's CI-friendly.

**One command to set it all up.** Rather than running the steps by hand, `bootstrap`
chains them — mirror repos → index → connect → embed → wiki → write editor steering —
skipping anything not enabled, so a teammate goes from nothing to a fully-wired
workspace in one step:

```bash
contextlake bootstrap --kb-config ~/.contextlake/kb.toml
```

Skip stages with `--no-sync` / `--no-embed` / `--no-wiki` / `--no-connect`. For an
isolated CLI, install with `pipx install "git+https://github.com/sayak-sarkar/contextlake"`
(add the `[kb]` extra for the knowledge layer), or run ad-hoc with `uvx`.

**Keep it fresh on a schedule.** `bootstrap` is incremental and branch-safe, so it's
safe to run repeatedly — it re-mirrors, re-indexes only the repos whose HEAD moved,
refreshes the knowledge layer, and rewrites the steering, without touching an
in-progress working tree. Run it from cron:

```cron
*/30 * * * * contextlake bootstrap --config ~/.contextlake.ini --kb-config ~/.contextlake/kb.toml >> ~/.contextlake/refresh.log 2>&1
```

or as a systemd user timer — see [`examples/contextlake.service`](examples/contextlake.service)
and [`examples/contextlake.timer`](examples/contextlake.timer).

**Code indexing** uses tree-sitter to extract files, classes, functions/methods,
interfaces, imports, and an intra-repo **call graph** from **Python, JavaScript,
TypeScript/TSX, and C#** (the parser registry is pluggable). It also reads
manifests (`pyproject.toml`, `package.json`, `*.csproj`) to build a **cross-repo
dependency graph** through shared package nodes. Over MCP, agents get tools to
traverse it: `search_code`, `find_definition`, `find_callers`, `find_dependents`,
`get_neighbors`, `shortest_path`, and `graph_stats`.

**Knowledge connectors** (`connect`) enrich the graph with external context. The
**Atlassian** connector links each repo to the Jira issues and Confluence pages it
references — issue keys harvested from branch/commit names are confirmed against
the live tracker (a single batched JQL call per site prunes false-positives and
fetches each issue's summary/status), and Atlassian URLs found in docs are
classified into issue/page links. It talks to one or more Atlassian sites over
MCP, each independently authenticated. The **Figma** connector links repos to the
design files they reference, classifying `figma.com` URLs to a stable file key. The
**GitLab** connector links each repo to its open **merge requests and issues** (read
through your authenticated `glab`). Connectors share one seam, so adding another is a
small, self-contained module; output lands in an isolated graph partition, so
re-indexing a repo's code never disturbs its external links.

Configure it by copying [`examples/kb.toml.example`](examples/kb.toml.example) to
`~/.contextlake/kb.toml`. Every fact is provenance-stamped (source file + verified
date) and confidence-tagged (`EXTRACTED` for AST facts, `INFERRED` for resolved
calls/links, `AMBIGUOUS` for unconfirmed candidates), and all output is sanitized
before it reaches an agent.

**Semantic search** (optional) adds natural-language retrieval on top of the graph.
Enable `[embeddings]` in the config (local-first — vectors come from an Ollama model
by default, so code never leaves the machine), run `contextlake embed` to vectorize
the indexed nodes into a local store, and `serve` then exposes two tools:
`semantic_search` for queries where the exact symbol name is unknown, and
`hybrid_search`, which seeds Personalized PageRank with the embedding hits and
propagates relevance across the graph (HippoRAG-style) to surface structurally
related nodes — a function's callers, a package's dependents — that a pure semantic
match would miss. The vector store uses an exact pure-Python cosine scan by default;
install the optional ANN backend with `pip install "contextlake[kb-vec]"` (sqlite-vec)
for larger workspaces.

**Curated wiki** (optional, local-first) turns the graph into prose. Enable
`[llm]` in the config (generation runs on a local Ollama model by default — prompts
never leave the machine) and run `contextlake wiki`: for each repo it synthesizes a
Markdown page grounded strictly in graph facts (top symbols, dependencies, files)
with a provenance footer citing the commit and sources, then puts the draft through
a **verification council** — reviewers score it for accuracy, completeness, and
clarity and a chairman publishes only pages above a configurable threshold. Nothing
that fails review is written.

**Model providers are pluggable.** The embeddings and wiki tiers default to a local
Ollama (`provider = "ollama"`); set `provider = "openai"` to use **any
OpenAI-compatible API** instead — a hosted key, or a local server like LM Studio,
Jan, llama.cpp, or vLLM. The key is read from an environment variable named by
`api_key_env` (default `OPENAI_API_KEY`) and is never stored in config; local
servers that need no key work with it unset. See `examples/kb.toml.example`.

### Use it from your editor or agent (MCP)

`contextlake serve` is an MCP server, so any MCP client can query the graph — and
**most of it needs no model**: the graph tools (`search_code`, `find_definition`,
`find_callers`, `find_dependents`, `shortest_path`, `graph_stats`) work on their
own; only `semantic_search`/`hybrid_search` need embeddings.

**The quickest way** is to let the tool wire your editors for you. From your
workspace root:

```bash
contextlake steer --config ~/.contextlake/kb.toml
```

This writes workspace-specific **`AGENTS.md`** (overview, the knowledge tools, and
guardrails), a thin **`CLAUDE.md`** that imports it, **`.windsurfrules`**,
**`.kiro/steering/`**, and merges a **`.mcp.json`** entry — so Claude Code, Windsurf,
Kiro, and other agents pick up the workspace context and the MCP server natively. It
also installs a generic library of **agent skills/workflows** (`.claude/skills/`,
`.windsurf/workflows/`) — investigate-root-cause, plan-before-coding,
surgical-change, review-before-landing, ship-safely, use-knowledge-graph — so even a
small-context model has a strong operating playbook. **It never corrupts your
existing files**: if you already have an `AGENTS.md`, `CLAUDE.md`, `.windsurfrules`,
or `.kiro/steering`, your content is preserved and a clearly-delimited managed block
is appended (and only that block is refreshed on re-runs); `.mcp.json` is merged so
your other servers stay; a skill file you wrote with the same name is kept as-is.
Custom layers like `.devin/` are left untouched.

To wire Claude Code by hand instead:

```bash
claude mcp add gitlab-kb -- contextlake serve --config ~/.contextlake/kb.toml
```

**Windsurf / Devin** — add the same server in its MCP config (Cascade's *MCP
Servers* panel, or `~/.codeium/windsurf/mcp_config.json`):

```json
{
  "mcpServers": {
    "gitlab-kb": {
      "command": "contextlake",
      "args": ["serve", "--config", "~/.contextlake/kb.toml"]
    }
  }
}
```

Once connected, ask the agent things like "where is `OrderService` defined?", "who
calls `charge`?", or "which repos depend on `shared-core`?" and it calls the graph
tools directly — you can even have it draft wiki pages from the graph without the
built-in `wiki` command.
