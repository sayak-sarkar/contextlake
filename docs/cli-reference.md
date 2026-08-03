# `contextlake` command reference

Every command has scoped help via `contextlake <command> --help`. This page is the at-a-glance map; each
command's own page (linked below) covers it in depth.

## Typos and abbreviations

A mistyped command suggests the closest real one instead of dumping the full command list:

```
$ contextlake fetc
✗ Unknown command: 'fetc'

Did you mean: mirror fetch?

Run 'contextlake --help' to see all commands.
```

The match runs against every command name **and its aliases** (`blast-radius` for `impact`, `who-knows`
for `owners`), then shows the canonical, namespaced verb, matching what `--help` teaches -- never the
deprecated flat spelling.

Flags never match on a partial name or abbreviation. `contextlake kb index --work-d /tmp` reports
`unrecognized arguments: --work-d` rather than silently guessing you meant `--workspace`: a prefix is
treated the same as an unknown flag, so a typo fails loudly instead of doing the wrong thing.

A genuine character-level typo of a real flag on the command you invoked (a transposition, a slipped
letter -- not a shortened prefix) does get a suggestion, scoped to that command's own flags:

```
$ contextlake kb index --worksapce .
✗ Unknown flag: '--worksapce'

Did you mean: --workspace?
```

A flag that's valid, just not on the command you ran, says so and names where it does belong, rather than
reporting it as simply unrecognized:

```
$ contextlake bootstrap --local
✗ '--local' isn't a flag on 'bootstrap'

It's used by: init, kb source.

Run 'contextlake bootstrap --help' to see bootstrap's own flags.
```

A value-taking flag immediately followed by another recognized flag (its value was left out, so the next
flag lands where the value should be) names the real problem instead of arguing you forgot a value
entirely:

```
$ contextlake kb dashboard --serve --workspace --open
✗ '--workspace' needs a value, but the next token ('--open') is itself a recognized flag

Put the value right after --workspace, e.g. '--workspace <value> --open'.
```

## Shell completion

Registered automatically the first time any command runs in a real interactive terminal (no `pip
install` post-install hook exists to do this at install time -- see [Mirror repositories](usage.md)
#shell-completion for why), and never overrides an explicit decline from `contextlake init
--no-completion`. Run `contextlake completion [bash|zsh|fish]` to register on demand instead of
waiting for that first run, or `CONTEXTLAKE_NO_AUTO_COMPLETION=1` to opt out of the automatic check
entirely.

## Advanced/resilience flags

The 8 `mirror`-tier commands (`mirror fetch`/`clone`/`update`/`branches`/`verify`/`status`/`sync`/`audit`)
each take a further ~14 retry/backoff/worker-pool/safety-check flags (`--max-retries`,
`--backoff-initial`/`--backoff-max`, `--adaptive-workers`, `--protect-working-branches`,
`--safe-branches`, `--require-clean-workspace`, `--auto-stash`, and their `--no-` counterparts) --
automation levers, not something to guess at from a bare `--help`. Every one already has a
`.contextlake.ini` equivalent (see [Mirror repositories](usage.md)), so they're kept out of the
default `--help` listing; run `contextlake <command> --help-advanced` to see them.

`contextlake --help` groups all 29 commands by task (Get started / Mirror a fleet / Build the
knowledge graph / Explore & search / Serve to editors) directly in its own output -- the tables below
are the same commands, organized for reference rather than a first read. Each knowledge-layer verb
below is typed under the `kb` namespace (`contextlake kb index`, `contextlake kb query`, ...); `init`,
`bootstrap`, `version`, `completion`, and `doctor` span both tiers or neither, so they stay top-level.

## Knowledge-layer commands

| Command | What it does |
| --- | --- |
| `kb source` | add / list / remove / test / enable / disable knowledge-source connectors |
| `kb index` | Build the code/dependency graph (`--workspace`, incremental, `--watch`) |
| `kb connect` | Link repos to Atlassian / Figma / GitLab items (`--watch` to keep refreshing) |
| `kb enrich` | Query connected sources with codebase-derived terms and store enrichment docs (`--workspace`, incremental) |
| `kb embed` | Build semantic-search vectors (zero-config built-in CPU model, Ollama, or an API; incremental, `--watch`) |
| `kb ingest` | Aggregate external docs into the graph + semantic store (built-in `files`/`web`/`api`/`graphql`/`mcp` sources, or plugins) |
| `kb wiki` | LLM-synthesized, council-verified wiki pages (per-repo, or a cluster page with `--namespace <prefix>` / `--namespaces --depth N`); `--llm builtin\|ollama\|openai\|anthropic\|cli` enables the LLM tier inline |
| `kb query` | Search the index (`--kind`, `--repo`, `--as-of <commit>`, `--retriever fts\|semantic\|hybrid`, `--json`) |
| `kb owners` | Likely owners / SMEs for a repo or path, ranked from git history (alias `kb who-knows`, `--json`) |
| `kb impact` | Change-impact / blast radius: what depends on a symbol (alias `kb blast-radius`, `--json`) |
| `kb graph` | Visualize the graph, offline interactive HTML / DOT / Mermaid / JSON, or a composed namespace C4 diagram with `--c4` |
| `kb dashboard` | Local knowledge-system dashboard UI (`--serve`; `--sample` for a bundled demo) |
| `kb eval` | Measure retrieval quality: precision / recall / MRR against a golden-query set |
| `kb lint` | Graph health audit: stale repos, dangling edges (`--json`) |
| `doctor` | Environment check: FTS5, git, glab, the store, embeddings, per-source reachability, C/C++ parser-version staleness |
| `bootstrap` | Run the whole pipeline end to end (sync, index, connect, embed, enrich, wiki, steer) |
| `kb serve` | Expose the graph over MCP (stdio, `--transport http`, or legacy `--transport sse`) |
| `kb steer` | Write per-editor steering (`AGENTS.md`, `.mcp.json`, and so on) |

The `mirror`-tier commands (`mirror fetch`, `mirror clone`, `mirror update`, `mirror branches`,
`mirror verify`, `mirror sync`, `mirror status`, `mirror audit`) are covered under
[Mirror repositories](usage.md).

## See also

- [Index the code graph](index-code-graph.md)
- [Serve it to your editor](serve.md)
- [Reading the console output](console-output.md)
