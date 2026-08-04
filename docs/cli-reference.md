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
for `owners`), then shows the canonical, namespaced verb, matching what `--help` teaches. This is also
what answers the retired flat spellings: `contextlake fetch` no longer parses, so it fails here like any
other unknown command and is pointed at `mirror fetch`.

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

## Global flags

These work on every command, before or after it:

| Flag | What it does |
| --- | --- |
| `--config PATH` | the sync INI for `mirror` commands, `kb.toml` for knowledge commands |
| `-v`, `--verbose` | debug output, and, on a crash, the traceback rather than just `Error: <message>` |
| `-q`, `--quiet` | warnings and errors only |
| `--log-file PATH` | append a full timestamped copy of the run (redacted by default, see below) |
| `--log-format text\|json` | `json` prints one JSON object per line, carrying the run id, command, repo and duration |
| `--metrics-file PATH` | after the run, write Prometheus textfile-collector metrics |
| `--redact` / `--no-redact` | scrub workspace paths, group and repo names from the console too / from nothing |
| `--access-log` | log every request the local HTTP servers answer (off by default) |
| `--plain` | no colour, even on a TTY (same as `NO_COLOR=1`) |

The last five exist for unattended operation, the systemd service + timer in `examples/`, a cron
wrapper, CI, where nobody watches the run and what it leaves behind is all there is.
[Reading the console output](console-output.md) has the JSON shape, the redaction placeholders, and
the metric names.

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
| `kb eval` | Measure retrieval quality: precision / recall / MRR against a golden-query set (`--json`) |
| `kb lint` | Graph health audit: stale repos, dangling edges, and (advisory, not in the exit code) repos built by an older parser (`--json`) |
| `doctor` | Environment check: FTS5, git, glab, the store, embeddings, per-source reachability, parser-version staleness. `--fix` installs what is missing (see below) |
| `bootstrap` | Run the whole pipeline end to end (sync, index, connect, embed, enrich, wiki, steer) |
| `kb serve` | Expose the graph over MCP (stdio, `--transport http`, or legacy `--transport sse`; the network transports print a bearer token and need `--allow-remote` for a non-loopback `--host`) |
| `kb steer` | Write per-editor steering (`AGENTS.md`, `.mcp.json`, and so on) |

The `mirror`-tier commands (`mirror fetch`, `mirror clone`, `mirror update`, `mirror branches`,
`mirror verify`, `mirror sync`, `mirror status`, `mirror audit`) are covered under
[Mirror repositories](usage.md).

### `doctor --fix`: install what is missing

`doctor` reports; `doctor --fix` repairs. With no value it installs only what your **resolved**
configuration actually calls for, so a `[llm]` block that is disabled or set to `ollama` never pulls
the local llama-cpp runtime. Name a capability to install it regardless of config.

| Flag | Effect |
| --- | --- |
| `--fix` | Install every missing dependency the resolved config calls for |
| `--fix <capability>` | Install one: `git`, `embedder`, `vectors`, `llm-local` |
| `--dry-run` (`-n`) | Print the full plan, exact commands included, and change nothing |
| `--skip-interactive` | Never prompt: privileged commands are printed, not run |

Two privilege tiers, and the split is deliberate:

- **Python packages** install into the interpreter contextlake is running in, with
  `sys.executable -m pip` (never a bare `pip`, which can belong to another environment). Unprivileged
  and reversible, so `--fix` runs them after printing them. For `llm-local` it attaches the upstream
  CPU wheel index automatically and says why.
- **System packages** (currently just git) need administrator rights. The exact command is printed
  in full and offered with a **y/N prompt at a real terminal only**. With `--skip-interactive`, or
  when stdin is not a TTY, it is printed and nothing runs, so a CI job or a scripted invocation can
  never trip a sudo prompt.

`--fix` also explains, rather than re-raising, the failures that actually happen: a PEP 668
externally-managed environment (use a venv or pipx), a proxy timeout, an untrusted intercepting CA,
or no matching distribution. Nothing planned is ever run before it has been printed.

## See also

- [Index the code graph](index-code-graph.md)
- [Serve it to your editor](serve.md)
- [Reading the console output](console-output.md)
- [Install and upgrade](install.md), the commands `--fix` runs for you, written out
- [Troubleshooting](troubleshooting.md), when one of them fails
