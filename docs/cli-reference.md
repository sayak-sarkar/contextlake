# `contextlake` command reference

Every flag and every command, in one place. Each command also carries scoped help via
`contextlake <command> --help`, and each command's own page (linked below) covers it in depth.
For what the CLI says back when a name or a flag is wrong, see
[Reading the console output](console-output.md#when-you-mistype-a-command-or-a-flag).

## Shell completion

`argcomplete` is a core dependency, so `pip install contextlake` alone is enough. A plain pip
install has no post-install hook to run anything at, which is a Python packaging limitation rather
than a gap here, so completion is registered instead the **first time any command runs in a real
interactive terminal**: once, idempotently, and it says so in the log. Non-interactive contexts (CI,
Docker, a piped command) are skipped entirely, since there is no shell to configure.

```bash
contextlake completion          # auto-detect $SHELL and register now
contextlake completion zsh      # register for zsh explicitly, regardless of $SHELL
```

An interactive `contextlake init` offers the same registration up front (on by default, `--no-completion`
to skip), and an explicit decline there is remembered: the automatic check never overrides it. A
non-interactive `init` (`--skip-interactive`, or a piped stdin) never touches your shell startup
file, so pass `--completion` to opt in. `CONTEXTLAKE_NO_AUTO_COMPLETION=1` disables the automatic
check altogether.

Whichever route, one of these is written once:

```bash
# bash, appended to ~/.bashrc
eval "$(register-python-argcomplete contextlake)"

# zsh, appended to ~/.zshrc (needs bashcompinit; most zsh setups already load it)
autoload -U bashcompinit && bashcompinit
eval "$(register-python-argcomplete contextlake)"

# fish, a dedicated file, written once
register-python-argcomplete --shell fish contextlake > ~/.config/fish/completions/contextlake.fish
```

For any other shell, copy the block for the closest match and open a new shell. `contextlake <TAB>`
then completes every command and, inside a command, every one of its flags, generated live from the
same parser that runs the command, so it cannot drift out of sync with the real CLI surface.

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
| `--offline` | refuse every non-loopback connection, so you can check a command stays local (same as `CONTEXTLAKE_OFFLINE=1`); commands that need a forge or a hosted model say so and stop |

`--log-file`, `--log-format`, `--metrics-file`, `--redact` / `--no-redact` and `--access-log` exist
for unattended operation, the systemd service + timer in `examples/`, a cron wrapper, CI, where
nobody watches the run and what it leaves behind is all there is. The last two are not:
`--plain` is a piping and TTY switch, and `--offline` is a locality guarantee you can turn on for a
single run to prove one, or leave on permanently (its env-var form, `CONTEXTLAKE_OFFLINE=1`, is
there for exactly that).
[Reading the console output](console-output.md) has the JSON shape, the redaction placeholders, and
the metric names.

## Advanced/resilience flags

The 8 `mirror`-tier commands each take a further set of retry, backoff, worker-pool and
safety-check flags (`--max-retries`, `--backoff-initial` / `--backoff-max`, `--adaptive-workers`,
`--protect-working-branches`, `--safe-branches`, `--require-clean-workspace`, `--auto-stash`, and
their `--no-` counterparts). They are automation levers rather than things to guess at from a bare
`--help`, and every one has a `.contextlake.ini` equivalent as its primary home (see
[Branch safety](mirroring-repositories.md#branch-safety)), so they are kept out of the default listing. Run
`contextlake mirror <command> --help-advanced` to see them. The flag exists on those 8 commands and
on `bootstrap`, which takes the same flag group, and nowhere else: nothing under `kb` has a hidden
tier to reveal, and there is no top-level `contextlake --help-advanced` either.

## The command surface

32 commands: 5 top-level, 8 under `mirror`, 19 under `kb`. `contextlake --help` groups them by
task in its own output; the tables below are the same commands organized for lookup. Two extra
spellings exist as aliases rather than as separate commands: `kb who-knows` for `kb owners` and
`kb blast-radius` for `kb impact`.

### Top-level commands

These span both tiers, or neither, so they are not namespaced.

| Command | What it does |
| --- | --- |
| `init` | Write the config files, prompting for each value (`--skip-interactive` to accept defaults) |
| `bootstrap` | Run the whole pipeline end to end: mirror, index, connect, embed, enrich, wiki, diagrams, API reference, design notes, steer. Each stage has a `--no-<stage>` switch |
| `doctor` | Environment check: FTS5, git, glab, the store, embeddings, per-source reachability, parser-version staleness. `--fix` installs what is missing |
| `completion` | Register shell tab-completion on demand |
| `version` | Print the installed version |

### Mirror commands

| Command | What it does |
| --- | --- |
| `mirror status` | Compare the cached project list against the local workspace, changing nothing |
| `mirror fetch` | Enumerate accessible projects and refresh the cache |
| `mirror clone` | Clone repositories present remotely and missing locally |
| `mirror update` | Fetch and fast-forward each local repo's current branch |
| `mirror branches` | Switch each repo to its most active branch |
| `mirror verify` | Check the local tree matches the remote list, and flag nested `.git` directories |
| `mirror sync` | fetch, clone, update, branches, verify, audit, in that order |
| `mirror audit` | Repo health and age report, as JSON and CSV |

Covered in depth under [Mirror repositories](mirroring-repositories.md).

### Knowledge-layer commands

| Command | What it does |
| --- | --- |
| `kb source` | add / list / remove / test / enable / disable knowledge-source connectors |
| `kb index` | Build the code/dependency graph (`--workspace`, incremental, `--watch`; a directory holding git repos is refused with the right command, `--bundle` to index it as one repo anyway) |
| `kb forget` | Remove one repository from the store, in every tier it occupies: graph nodes and edges, semantic vectors, wiki pages, and its `@connect:` / `@enrich:` connector partitions. The repair for a mis-index (`--dry-run` reports what would go, and removes nothing) |
| `kb connect` | Link repos to Atlassian / Figma / GitLab items (`--watch` to keep refreshing) |
| `kb enrich` | Query connected sources with codebase-derived terms and store enrichment docs (`--workspace`, incremental) |
| `kb embed` | Build semantic-search vectors (zero-config built-in CPU model, Ollama, or an API; incremental, `--watch`) |
| `kb ingest` | Aggregate external docs into the graph + semantic store (built-in `files`/`web`/`api`/`graphql`/`mcp` sources, or plugins) |
| `kb wiki` | LLM-synthesized, council-verified wiki pages (per-repo, or a cluster page with `--namespace <prefix>` / `--namespaces --depth N`); `--llm builtin\|ollama\|openai\|anthropic\|cli\|auto` enables the LLM tier inline (`builtin` needs `doctor --fix llm-local` first on a `pip` install; `ollama` needs no compiler) |
| `kb docs` | Generated documentation, no model involved: an API reference per repository listing each symbol's real call sites, plus design notes recording the dependencies its manifests declare and the values its code reads most. `--max-symbols N` bounds the reference (default 500) and every page states what it left out |
| `kb query` | Search the index (`--kind`, `--repo`, `--as-of <commit>`, `--retriever fts\|semantic\|hybrid`, `--json`) |
| `kb owners` | Likely owners / SMEs for a repo or path, ranked from git history (alias `kb who-knows`, `--json`) |
| `kb impact` | Change-impact / blast radius: what depends on a symbol (alias `kb blast-radius`, `--json`) |
| `kb graph` | Visualize the graph. `--format` takes 11 values: `html` (offline interactive, the default), `dot`, `json`, `graphml`, `cypher`, and the six Mermaid ones (`mermaid`, `classdiagram`, `sequencediagram`, `statediagram`, `erdiagram`, `deploymentdiagram`); or a composed namespace C4 diagram with `--c4`. All of them, with what each is for, are on [Visualize the graph](visualizing-the-graph.md) |
| `kb dashboard` | Local knowledge-system dashboard UI (`--serve`; `--sample` for a bundled demo) |
| `kb eval` | Measure retrieval quality: precision / recall / MRR against a golden-query set (`--json`, `--verify-citations`) |
| `kb refresh` | Report whether the graph is current; `--refresh` updates it in the background, `--hook` prints Claude Code SessionStart JSON |
| `kb lint` | Graph health audit: stale repos, dangling edges, and (advisory, not in the exit code) repos built by an older parser (`--json`) |
| `kb serve` | Expose the graph over MCP (stdio, `--transport http`, or legacy `--transport sse`; the network transports print a bearer token and need `--allow-remote` for a non-loopback `--host`; `--tool-concurrency N` bounds how many tool calls run at once, default `2`) |
| `kb steer` | Write per-editor steering (`AGENTS.md`, `.mcp.json`, and so on) |
| `kb hook` | Install, remove or inspect the `post-commit` hook that re-indexes a repo on commit |

`doctor --fix`'s own flags, and the two privilege tiers behind them, are on
[Install and upgrade](installing.md#installing-what-is-missing).

## Exit codes

Four: `0` nothing failed, `1` something did, `2` the invocation was wrong, `130` interrupted
mid-job. The long-running servers are not in that last one: `Ctrl-C` is how you are told to stop
them, so they exit `0`, and `kb serve --transport http`/`sse` exits `143` on `SIGTERM` after
uvicorn's graceful shutdown. The conditions behind each are on
[Reading the console output](console-output.md#what-it-exited-with).

## See also

- [Ask the graph](asking-the-graph.md), `kb query`, `kb impact` and `kb owners` in depth
- [Index the code graph](indexing-the-code-graph.md)
- [Serve it to your editor](serving-over-mcp.md)
- [Reading the console output](console-output.md)
- [Install and upgrade](installing.md), the commands `--fix` runs for you, written out
- [Troubleshooting](troubleshooting.md), when one of them fails
