# `contextlake` command reference

Every flag and every command, in one place. Each command also carries scoped help via
`contextlake <command> --help`, and each command's own page (linked below) covers it in depth.
For what the CLI says back when a name or a flag is wrong, see
[Reading the console output](console-output.md#when-you-mistype-a-command-or-a-flag).

## Shell completion

`argcomplete` is a core dependency, so `pip install contextlake` alone is enough.

A plain pip install has no post-install hook to run anything at. That is a Python packaging
limitation, not a gap here. So completion is registered instead the **first time any command runs
in a real interactive terminal**: once, idempotently, and it says so in the log.

Non-interactive contexts are skipped entirely, since there is no shell to configure. That covers
CI, Docker and a piped command.

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
| `--offline` | refuse every non-loopback connection this process makes (same as `CONTEXTLAKE_OFFLINE=1`); `mirror` network commands and the `cli` LLM provider stop before they spawn. Some subprocesses are not covered, listed below |

Five flags exist for unattended operation: `--log-file`, `--log-format`, `--metrics-file`,
`--redact` / `--no-redact`, and `--access-log`. They are for the systemd service and timer in
`examples/`, a cron wrapper, or CI: places where nobody watches the run, and what it leaves
behind is all there is.

Two more look similar and are not:

- **`--plain`** is a piping and TTY switch.
- **`--offline`** is a locality guarantee. Turn it on for a single run to prove one, or leave it
  on permanently. Its env-var form is `CONTEXTLAKE_OFFLINE=1`.

### What `--offline` covers, and what it does not

It blocks at the socket, inside this process. Every client here inherits the block: `urllib`,
`requests`, `httpx`, and the model downloaders inside the embedding and LLM libraries. Loopback
stays open on purpose, so a local Ollama, the MCP server, the dashboard and the graph viewer keep
working.

A subprocess has its own sockets, so the block cannot reach one. Three of them are handled
another way:

- `mirror fetch`, `clone`, `update`, `branches` and `sync` refuse and exit 2. `bootstrap` skips
  its mirror stage and runs the local stages.
- `[llm] provider = "cli"` is refused before the agent CLI is spawned, so `kb wiki`, `kb docs`
  and dashboard chat print the refusal and carry on without prose.
- The dashboard's MCP start/restart control, its `Regenerate wiki` control, and
  `kb refresh --refresh` spawn contextlake itself and pass `CONTEXTLAKE_OFFLINE` down, so
  the child guards itself.

These are **not** covered. Audited 2026-09-03; treat the list as open, not complete:

| Not covered | Reached by | Why |
| --- | --- | --- |
| `kb connect` running `glab api` | any `[[sources]] type="gitlab"` | `glab` is a separate program and does not read the variable |
| Dashboard `git clone` / `git pull` | adding or refreshing a repo in the dashboard | same: `git` does not read the variable |
| `contextlake doctor --fix` running `pip install` | repairing a missing extra | same: `pip` does not read the variable |
| `mcp` sources and connector tool calls over stdio | any `[[sources]] type="mcp"`, and the Atlassian / Figma / Slack connectors | the MCP SDK replaces the child's environment with a six-name whitelist, so the variable is stripped. Their `url` transport is in-process and **is** covered |
| Scheduled runs (`contextlake schedule`) | the scheduler spawning a job | the child is contextlake and reads `CONTEXTLAKE_OFFLINE`, so the env-var form works. `--offline` as a flag does not survive the spawn |

Closing these is per-call-site work, not one switch. Until it lands, `CONTEXTLAKE_OFFLINE=1` in
the environment is the stronger of the two forms: it reaches any child that reads it, while the
flag reaches only this process.

[Console output](console-output.md) has the JSON shape, the redaction placeholders and the metric
names.

## Advanced/resilience flags

The 8 `mirror`-tier commands each take a further set of retry, backoff, worker-pool and
safety-check flags:

`--max-retries`, `--backoff-initial`, `--backoff-max`, `--adaptive-workers`,
`--protect-working-branches`, `--safe-branches`, `--require-clean-workspace`, `--auto-stash`, and
their `--no-` counterparts.

They are kept out of the default listing for two reasons. They are automation levers rather than
things to guess at from a bare `--help`, and every one has a `.contextlake.ini` equivalent as its
primary home. See [Branch safety](mirroring-repositories.md#branch-safety).

Run `contextlake mirror <command> --help-advanced` to see them.

That flag exists on those 8 commands and on `bootstrap`, which takes the same group. Nowhere
else. Nothing under `kb` has a hidden tier, and there is no top-level
`contextlake --help-advanced`.

## The command surface

34 commands: 6 top-level, 8 under `mirror`, 20 under `kb`. `contextlake --help` groups them by
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
| `schedule` | Measure a run, work out an interval, and install a systemd timer or crontab entry that keeps it current on its own (`recommend`, `install`, `status`, `list`, `run`, `reset`, `uninstall`, `interval`). Core tier: works without the `[kb]` extra. See [Scheduling runs](scheduling.md) |

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
| `kb source` | add / list / remove / test / enable / disable knowledge-source connectors, and `wizard` to check every configured one and then walk an add step by step (needs a terminal) |
| `kb index` | Build the code/dependency graph, and write the API reference and design notes for the repos it indexed (`--workspace`, incremental, `--watch`; a directory holding git repos is refused with the right command, `--bundle` to index it as one repo anyway; `--no-docs` skips the documents, and a repo whose head commit has not moved is skipped) |
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
| `kb serve` | Expose the graph over MCP (stdio, `--transport http`, or legacy `--transport sse`; the network transports print a bearer token and need `--allow-remote` for a non-loopback `--host`; `--keys-file` and `--keys-only` decide which key file is read and whether a shared token may be minted; `--tool-concurrency N` bounds how many tool calls run at once, default `2`) |
| `kb keys` | Create and manage the API keys that authenticate MCP callers: `create`, `list`, `show`, `revoke`, `rotate`, `check`, `prune` |
| `kb steer` | Write per-editor steering (`AGENTS.md`, `.mcp.json`, and so on) |
| `kb hook` | Install, remove or inspect the `post-commit` hook that re-indexes a repo on commit |

`doctor --fix`'s own flags, and the two privilege tiers behind them, are on
[Install and upgrade](installing.md#installing-what-is-missing).

### `kb serve`: which key file, and whether it may mint

Two flags decide whether a network start comes up shared, comes up per-key, or refuses.

**`--keys-file PATH`** says where the keys live. Four tiers, highest wins:

| Tier | Where it is set |
| --- | --- |
| `--keys-file PATH` | the command line |
| `$CONTEXTLAKE_KEYS_FILE` | the environment |
| `[serve] keys_file` | `~/.contextlake/kb.toml`, or the file passed to `--config` |
| `~/.contextlake/mcp-keys.json` | nowhere. The default |

Naming a path in any of the top three says the keys live there, so **an absent file at a
named path is refused, exit 1**. It is not read as a first start. The case this stops: a
container whose volume mount did not appear starts with the named path empty, and the server
used to mint one unscoped shared token and print stderr that read like a fresh machine. Only
the default tier, with nobody naming anything, may mint.

A `[serve] keys_file` in a `.contextlake.kb.toml` found by walking up from the current
directory is ignored, and the run says so on one line. That file sits inside a repository
checkout, so anything it points at gets committed. Pass `--config <file>`, or set the key in
`~/.contextlake/kb.toml`, to have it honoured.

**`--keys-only`** refuses to start rather than mint an unscoped shared token. Use it where an
open server is worse than no server. It refuses in two cases:

- No key file with a live key was found. Minting is the thing the flag exists to prevent, so
  the server exits `1` and names `contextlake kb keys create`.
- `$CONTEXTLAKE_MCP_TOKEN` is set. That is one credential every caller shares, so per-key
  revocation and attribution stop meaning anything. Unset the variable, or drop the flag.

**Every refusal prints before the line that says the server is up.** A start that is going to
refuse never prints `✓ MCP server on http://...`; the banner is the last thing written before
the socket opens.

### `kb keys`

Every key is one caller's credential. The server checks the key on each request, so
revoking one takes effect on the next request with no restart.

```
contextlake kb keys create alice-laptop --expires 90d
contextlake kb keys list
contextlake kb keys show k_4f2a91
contextlake kb keys revoke k_4f2a91 --reason "laptop returned"
contextlake kb keys rotate k_4f2a91 --overlap 24h
printf '%s' "$KEY" | contextlake kb keys check
contextlake kb keys prune --before 2026-01-01
```

**The key is shown once, and only once.** It is printed to standard error at creation and
never again. The file stores a SHA-256 digest of the key, not the key, so there is nothing
to print later. A key nobody wrote down is rotated, never recovered:
`contextlake kb keys rotate <id>` issues a replacement and keeps the old one working for
`--overlap` so the holder has time to swap.

Three ways to capture it at creation:

- Read it off the screen. This is the default.
- `--print-key` writes the bare key to standard output for a pipe, e.g.
  `contextlake kb keys create ci --print-key | pass insert -e contextlake`. It refuses a
  terminal, because on one the key lands in the scrollback instead of a secret store.
- `--out FILE` writes the key to a file at mode `0600`. The file is created with `O_EXCL`,
  so an existing path is refused rather than overwritten.

The key never reaches standard output on the default path and never reaches `--log-file`,
which is a rotating file that outlives the process.

**`check` reads the key from standard input only.** A key typed on a command line lands in
shell history and shows in `ps` to every account on the machine, so
`contextlake kb keys check <key>` is refused with exit 2. A terminal with nothing piped in
is refused the same way, rather than waiting for end-of-file behind a blank screen. `check`
opens no socket and sends no request: it compares the digest against the key file, which is
what lets it answer when the server is the thing that is down. It reports what the record
stores, not what a server would allow.

**The checksum in a key is a typo filter, not a security control.** It catches a key
mistyped or truncated in transit before a request is sent. It stops nobody from forging a
key, which is the digest comparison's job.

**The scope flags are recorded and enforced by nothing.** `--tools`, `--repos`,
`--owners`, `--rate`, `--burst` and `--cost-budget` are written onto the key and rendered
back by `create`, `list`, `show` and `check`. No code reads them. A key created with
`--tools none --repos nothing-matches/*` gets the full tool list over MCP and can call
every one of them on every indexed repository. Measured, not assumed: that key was
presented to a live `kb serve --transport http --keys-only` server and `tools/list`
answered with all 23 registered tools.

So every surface that prints them says `(recorded, not enforced)` beside the values and
carries three lines saying what that means. `show --json` and `list --json` carry
`"policy_enforced": false` for the same reason. Do not hand out a key believing the scope
limits it. Enforcement ships in a later release; `--rate` and `--cost-budget` are stored
as typed and are not validated yet either.

**`--client` prints the config snippet for the editor that will hold the key**
(`claude-code`, `cursor`, `vscode`, `windsurf`, `zed`). Every snippet reads the key from a
variable rather than inlining it, except Zed, whose `context_servers` documents no
environment expansion for a header value. VS Code has the best handling of the five: it
prompts once and keeps the value outside the config file. `claude-desktop` and `claude-web`
are refused, each naming the route that does work.

**Exit codes.** `0` on success, including `list` on a key file that does not exist yet. `1`
on an id that is not in the file, on a key file that cannot be read, and on `check` of a
key that is malformed, unknown, revoked or expired. `2` on a missing positional or a bad
flag value. Note the asymmetry with `kb source remove`, which treats a missing name as a
no-op at `0`: `revoke` on an unknown id fails, because an admin scripting a revocation
reads the exit code and "I revoked nothing" must not read as success.

No `kb keys` verb opens the store database, so every one of them runs on a machine with no
index built. `kb keys list` is the first command to run after a server refuses to start.

## Exit codes

Four: `0` nothing failed, `1` something did, `2` the invocation was wrong, `130` interrupted
mid-job. The long-running servers are not in that last one: `Ctrl-C` is how you are told to stop
them, so they exit `0`, and `kb serve --transport http`/`sse` exits `143` on `SIGTERM` after
uvicorn's graceful shutdown. The conditions behind each are on
[Reading the console output](console-output.md#what-it-exited-with).

## See also

- [Scheduling runs](scheduling.md), the `schedule` command in depth
- [Ask the graph](asking-the-graph.md), `kb query`, `kb impact` and `kb owners` in depth
- [Index the code graph](indexing-the-code-graph.md)
- [Serve it to your editor](serving-over-mcp.md)
- [Reading the console output](console-output.md)
- [Install and upgrade](installing.md), the commands `--fix` runs for you, written out
- [Troubleshooting](troubleshooting.md), when one of them fails
