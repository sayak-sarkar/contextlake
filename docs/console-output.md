# Reading the console output

A `bootstrap` (or a standalone `index` / `embed` / `wiki`, and the mirror-tier `clone` / `update` /
`branches`) prints progress as it goes. Most lines are self-explanatory; a few are worth decoding.

## The live progress bar

One shared renderer is used by every long-running command, so it looks the same everywhere:
`[█░░░░░░░░░░░░░] 42/678 (6%) · 12:30 elapsed · ~3:09:17 left · 3.4/min` (bar, done/total, percent,
elapsed time, estimated time remaining, and rate in items/min). The ETA is the **cumulative mean** so far, elapsed time divided
by items done, projected over what's left (that's what the `~` marks), and it's count-based, each item
counts equally rather than being weighted by size. A trailing window was tried and rejected: with a
worker pool the gaps between completions are spiky enough that a short window swung the estimate between
seconds and half an hour, where the cumulative figure smooths itself and settles as the run goes on.
When a run's total isn't known up front, the bar drops the percent/ETA and shows `done · elapsed · rate`
instead, rather than guessing. Across every long-running command (including `connect`, `ingest`, and
`enrich`, which don't use the shared bar), the clock only shows up on the bar itself (where there is one)
and on section/summary lines; the per-item detail lines scrolling beneath don't repeat it, so they don't
flicker as the timestamp ticks over.

## One status vocabulary, everywhere

Every command (mirror-tier `clone`/`update`/`branches`, `index`, `embed`, `wiki`, `enrich`, `ingest`,
`connect`, `lint`, `sync`) marks each line with the same seven glyphs, so once you know the glyph you know
the outcome without reading the rest of the line:

| Glyph | Meaning | Color |
| --- | --- | --- |
| `✓` | ok | green |
| `⚠` | warn | yellow |
| `✗` | fail | red |
| `⊘` | skip | dim |
| `=` | unchanged | dim |
| `↝` | switched | cyan |
| `~` | dry-run | yellow |

An eighth glyph, **`•` note**, is currently only emitted by `update` and `branches`: it means something
different from `⊘` skip. Skip means something that would normally happen didn't (an unsafe working tree,
a diverged branch, an archived project) -- there was a reason to hold back. Note is not that: it
describes what the repo *is*, not something that was withheld. The one case today is a freshly-created
repo with no commits yet (`update`/`branches` can't resolve `HEAD` there because there is no history to
read) -- nothing failed and nothing was skipped, there's simply nothing to sync yet.

Multi-stage commands (`bootstrap` and `sync`) also print `▶ <Phase>` section headers (e.g. `▶ Mirror
repositories from GitLab`, `▶ Audit repositories (health & age)`) so a long run reads as sections rather
than one undifferentiated scroll, and every long-running command ends with a one-line, glyph-prefixed
summary (`✓ Embed complete: ...`, `✓ Lint: ...`, and so on) you can skim straight to.

**`--plain`** (same effect as setting `NO_COLOR=1`) strips ANSI color, even on a TTY, useful when piping
into a tool that doesn't expect escape codes. It doesn't touch the glyphs themselves, `✓`/`⚠`/`✗` and the
rest still print; only the color wrapped around them is gone.

## When you mistype a command or a flag

The error output is part of the console output, so it gets the same treatment as the rest: say what is
wrong, then say what to do about it.

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
letter, not a shortened prefix) does get a suggestion, scoped to that command's own flags:

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

## Machine-readable logs: `--log-format json`

Everything above describes output composed for a person. When the reader is a log collector instead --
the systemd timer in `examples/`, a cron wrapper, CI -- `--log-format json` prints one JSON object per
line and nothing else:

```json
{"ts": "2026-08-04T05:45:19Z", "level": "INFO", "msg": "[1/12] ✓ team/api: Cloned", "run_id": "bcd5bd3d69cb", "command": "mirror clone", "repo": "team/api", "status": "ok", "duration_ms": 812}
```

- **`msg`** is the same composed line a person would have seen, counter and glyph included, not a bare
  verb. Query the structured fields beside it (`status`, `repo`, `error_type`) rather than parsing it.
- **`run_id`** is generated once per invocation and stamped on every line, so an interleaved journal can
  be split back into runs, and one `bootstrap`'s index / connect / embed / wiki stages read as one story.
  Set `CONTEXTLAKE_RUN_ID` to pin your own (a systemd invocation id, a CI job id) and have contextlake's
  lines join up with the surrounding job's.
- **`command`** is the command as you would type it (`mirror sync`, `kb index`).
- **`repo`** and **`duration_ms`** appear on per-repo lines, which is what makes "which repo is making
  the nightly run slow" a query rather than a guess.
- Failures add **`error_type`** (`dns`, `timeout`, `auth`, `diverged`, ...) and **`error`**, so "every
  failure last night was DNS" is one aggregation.

The default (`text`) output is unchanged, character for character. Structured fields are only ever
*added* to the JSON form; they never alter a human line.

## Sharing a log: `--redact`

`--log-file PATH` keeps a full-detail copy of the run. That copy is **redacted by default**: workspace
paths, `$HOME`, the group/org name, a self-hosted forge hostname and repository names are replaced with
placeholders, so it can be attached to a bug report as-is.

```
[2026-08-04 05:48:49] Working directory: <workspace>
[2026-08-04 05:48:49] Gitlab group: <group>
[2026-08-04 05:48:49] Missing repositories:
[2026-08-04 05:48:49]   repo-5a7da0a4
```

Repository names become a stable `repo-<digest>`, so "the same three repos failed again" still reads
correctly after scrubbing, and a file path *inside* a repo keeps its tail (`<workspace>/repo-5a7da0a4/
src/main.py:42`) because which file broke is the useful half. This is obfuscation for sharing, not a
cryptographic guarantee: a short, guessable repository name can be confirmed by anyone who guesses it.

The console is left alone by default -- you need the real paths to act on what you are reading.
`--redact` scrubs the console too; `--no-redact` scrubs neither.

## Metrics for an unattended run: `--metrics-file`

`--metrics-file PATH` writes Prometheus [textfile-collector](https://github.com/prometheus/node_exporter#textfile-collector)
output after the run, so the shipped systemd timer is monitorable with no exporter process of its own.
Point node_exporter's `--collector.textfile.directory` at the file's directory and name the file `.prom`:

```
contextlake_run_duration_seconds{command="mirror sync"} 42.318
contextlake_run_exit_code{command="mirror sync"} 0
contextlake_repos{command="mirror sync",status="ok"} 480
contextlake_repos{command="mirror sync",status="failed"} 0
contextlake_repos{command="mirror sync",status="skipped"} 3
contextlake_graph_nodes 128394
contextlake_graph_edges 214005
contextlake_last_success_timestamp_seconds{command="mirror sync"} 1785802519
```

Two behaviours worth knowing. `contextlake_last_success_timestamp_seconds` is **carried forward** by a
failing run rather than erased -- that timestamp is the whole basis of a "stale for six hours" alert. And
a value that could not be measured is **omitted, never written as 0**: a `mirror sync` does not touch the
knowledge graph, so it publishes no `contextlake_graph_nodes` at all rather than a zero that reads as
"the graph was wiped". The counts come from what the run already tallied (the same numbers that decide
the exit code), so the metrics can never disagree with the summary line.

## The stdout / stderr split

The bar renders on stderr; the per-item result lines below it (`✓`/`⚠` and the like) stay on stdout. That
split means `contextlake kb wiki >> run.log` (or any stdout redirect) captures clean detail lines with no bar
artifacts or `\r` clutter, since the bar never touches stdout. When output isn't a TTY (piped, cron, a
redirected stderr), the bar itself auto-downgrades to periodic plain summary lines instead of repainting in
place. When both streams share one terminal (the default interactive case), the bar and the detail lines
interleave as the run scrolls (the bar reprints below each new detail line rather than repainting perfectly
in place); redirect stdout to a file to keep the bar as a single live line with the detail captured
separately.

## Decoding specific lines

- **`✓ <repo>: X nodes, Y edges`** is the incremental indexer's per-repo detail line (stdout; the `index`
  progress bar above it lives on stderr). **`0 nodes, 0 edges`** is normal and not an error: that repo has
  no code in a supported language (config-only, docs-only, IaC/scripts, or empty). Only repos whose HEAD
  moved are re-indexed; the rest are reported as *unchanged*.
- **`Embed complete: 0 vector(s) written (N total in store), M already up to date`**, embedding is
  incremental too. `0 written` with a large `already up to date` count means nothing changed since the last
  run; the `N total` is the whole store, not this run.
- **`Fetching 10 files: 100% ... Download complete: 0.00B`** appears once when the wiki (or built-in
  embedder) model loads. It is Hugging Face resolving the model repo's files (several GGUF quantizations +
  tokenizer/config) in your local cache, **`0.00B` means nothing was downloaded, everything was already
  cached**. It fires once per run at model load, not per repo.
- **`✓ <repo>: written (score 0.98)`**, a wiki page passed the review council and was saved. **`⚠ <repo>:
  rejected by council (score 0.31)`**, it did not clear the accept threshold; the indented `-
  accuracy/completeness/clarity: ...` lines are the per-lens reasons. **`unparseable review`** means the
  model returned a review the council couldn't score (common with the tiny built-in 0.5B model); those
  lenses are excluded from the mean rather than counted as zero. A rejection that also says
  **`N reviewer(s) returned nothing parseable`** tells you how many lenses abstained, when that count
  equals your `council_size` on every page, suspect a *misconfigured reviewer* (missing API key, review
  CLI not on PATH) rather than genuinely weak pages: a reviewer that returns nothing rejects everything
  at score 0.0, which otherwise looks identical to a very strict council. A capable backend
  (`--llm ollama`/`anthropic`/`openai`) produces far fewer rejections, see
  [Model providers](model-providers.md).
- **`contextlake kb serve --transport http`/`sse` prints its bind URL** once it starts listening --
  `✓ MCP server on http://127.0.0.1:8765/mcp  (Ctrl-C to stop)` for `http`, or the same with an `/sse`
  suffix for `sse` -- so you don't have to guess the host/port/path before pointing an editor at it.
  Both URLs include the path because neither transport is served at the bare root. Note that probing
  the root will not tell you that: the bearer-token middleware wraps the whole app, so an
  unauthenticated request to any path, the root included, answers `401` rather than `404`.
  `stdio` transport has no address to report and stays quiet on that line.
- **The network transports print their bearer token right under that URL, on stderr.** A socket that
  serves the whole graph needs a credential, and a credential you cannot find is the same as a server
  you cannot use -- so it is said once, next to the address it belongs to, rather than left to be
  discovered. Deliberately *not* through the logger: `--log-file` would otherwise leave the token on
  disk after the process is gone. Pin your own with `CONTEXTLAKE_MCP_TOKEN` and the line acknowledges
  it instead of echoing the value. `stdio` prints no token because it needs none. See
  [Serve](serve.md).
- **`graph --overview` on an empty store warns instead of reporting silent success.** It still writes the
  (empty) artifact, but now says `⚠ Wrote html (0 nodes, 0 edges) -> ...: the store is empty.` followed by
  a hint to run `contextlake kb index` first, instead of logging the same success line it would for a
  populated graph.
- **A single-writer lock message** naming another process means two runs targeted one store at once (see
  the git-hook note under [Bootstrap and keep it fresh](keep-fresh.md)).

Warnings from the model download itself (Hugging Face symlink/auth notices) are silenced; the real
progress still shows.

## What it exited with

Four codes across the CLI, plus one that only `kb serve` can produce.

| Code | Means | Typical causes |
| --- | --- | --- |
| `0` | Nothing failed | A clean run; also a run where work was deliberately skipped, a `--dry-run`, `--help`, `version`, and a search that matched nothing |
| `1` | Something failed | Any repo failed in `mirror fetch` / `clone` / `update` / `branches` / `verify` / `sync`; a failed `bootstrap` stage; a `kb` command whose target could not be resolved (unknown repo, ambiguous symbol, no snapshot at that commit); a bad `--config` path; `doctor` when the report found a problem |
| `2` | You and the CLI disagree about the command | An unknown command, an unrecognized flag, a flag whose value is missing, a `kb query` / `impact` / `owners` with no target, an unknown `--platform` or shell, or no group configured on a command that needs one |
| `130` | `Ctrl-C` on a command that was still working | Interrupted at any point, including during `init`. Not the long-running servers, see below |

**The long-running commands are the exception, and deliberately so.** `kb serve`,
`kb dashboard --serve`, `kb graph --serve` and `kb graph --site --serve` are meant to be ended by
you, and each says `Ctrl-C to stop` on the line announcing where it listens (`kb serve --transport
stdio` is the one with no address to announce). Stopping them that way is the documented ending
rather than an interruption: each catches the interrupt, prints its own stop
line (`Stopping MCP server`, `Stopping dashboard server`, `Stopping graph server`,
`Stopping graph site server`) and exits `0`. The `--watch` loops on `kb index`, `kb embed` and
`kb connect` end the same way, finishing normally rather than aborting. `130` is for a command
that was interrupted mid-job.

`kb serve` adds one code the rest of the CLI never uses:

| Code | When | Why |
| --- | --- | --- |
| `0` | `Ctrl-C`, on any transport; `SIGTERM` on `stdio` | The requested stop, completed |
| `143` | `SIGTERM` on `--transport http` / `sse` | `128 + 15`, "terminated by SIGTERM" |

`143` is what a supervisor expects from a process it asked to stop, not a fault: uvicorn drains
connections and shuts down first, then re-raises the signal it captured so the exit status
reports the termination honestly. `stdio` reaches `0` on `SIGTERM` because it installs its own
handler and routes both signals into the same shutdown `Ctrl-C` takes; before that it had none, so
a supervisor's `SIGTERM` killed it outright and its cleanup never ran. See
[Serve it to your editor](serve.md#stopping-it).

Three things worth knowing about `1`:

- **A partial run counts as a failure.** Some repositories synced and others did not is still `1`,
  which is the point: before that, a completely broken sync looked identical to a healthy one.
  `sync` aggregates across all its stages, so one failed clone fails the run, and `bootstrap`
  counts a failed mirror stage exactly as it counts a failed knowledge-layer stage.
- **Deliberate skips are not failures.** Already up to date, a protected working branch, a
  `--dry-run`: none of those affect the code. `mirror verify` fails only on a cloned path that is
  not a valid git repository, not on repos that are merely missing or extra.
- **`--exit-zero-on-partial` exits `0` anyway** when some repositories failed. The failures are
  still reported; they just do not fail the job.

`mirror status` and `mirror audit` only report, so they do not fail on what they find. An empty
result from `kb query`, `kb impact` or `kb owners` is also `0`: if you are scripting, test the
payload rather than the exit code to detect "found nothing".

`--verbose` changes what a crash leaves behind: the top-level handler re-raises instead of printing
`Error: <message>` alone, so a bug report can carry the traceback without anyone having to
reproduce the failure under a debugger.

## See also

- [Bootstrap and keep it fresh](keep-fresh.md)
- [Index the code graph](index-code-graph.md)
- [`contextlake` command reference](cli-reference.md)
