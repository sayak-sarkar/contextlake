<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/readme-banner.jpg" alt="contextlake, all your real context in one local lake. Pebble the otter surfacing from a misty lake cradling a glowing pebble of context." width="820">
</p>
<h1 align="center">contextlake</h1>
<p align="center"><strong>All your real context, in one local lake.</strong></p>
<p align="center">
  A local context layer for your AI tools: mirror your repositories, index them<br>
  into a knowledge graph, and serve it over MCP, so agents answer from <em>real source</em> instead of guessing.
</p>

<p align="center">
  <a href="https://github.com/sayak-sarkar/contextlake/actions/workflows/ci.yml"><img src="https://github.com/sayak-sarkar/contextlake/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/contextlake/"><img src="https://img.shields.io/pypi/v/contextlake?color=137A8B" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/offline-first-2BB3A3" alt="Offline-first">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
</p>

---

## Why contextlake

Your AI assistant is only as good as what it can actually see. Point it at one file and
it's sharp; ask it about *the system*, which service calls this API, who depends on that
package, where a symbol is really defined across dozens of repos, and it starts guessing.

**contextlake gives your tools the real source to read.** It mirrors your repositories to
your machine, indexes them into a queryable knowledge graph, and serves that graph to your
editor over [MCP](https://modelcontextprotocol.io). Everything runs locally and offline,
no code leaves your machine, and it carries no credentials of its own.

## How it works

contextlake is three layers you adopt one at a time. The mirror is useful on its own, and
each layer above it is optional.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/architecture.png" width="860" alt="contextlake architecture. On the left, your repos: a GitLab group, plus optional Figma, Jira, and other MCP connectors. In the centre, contextlake indexes and mirrors them into a graph and embeddings, a wiki, and connectors. On the right, it serves the result over MCP to your AI tools: Claude Code, Windsurf, Kiro, Cursor, and Postman.">
</p>

1. **Mirror**: clone every repo you can reach in a **GitLab group, GitHub org, Bitbucket
   workspace, or Gitea/Codeberg/Forgejo owner** into a faithful copy of its namespace tree,
   each on its most active branch, kept fresh with one command.
2. **Knowledge layer** *(optional)*: parse the mirror into a code + dependency **graph** across
   **23 languages** plus **Terraform** infrastructure, **SQL** schema, and package manifests
   (npm / PyPI / NuGet / Maven), add **semantic search**, a council-verified **wiki** (each page
   reviewed and scored before publishing, low-confidence pages dropped), and **connectors** to
   Atlassian / Figma / GitLab / Slack.
3. **Serve**: expose it all over **MCP** and an offline interactive **graph visualizer**, so
   agents can answer *"where is `X` defined?"* or *"who calls `Y`?"* instead of grepping.

Each layer has its own guide: the mirror in **[Mirror repositories](https://github.com/sayak-sarkar/contextlake/blob/main/docs/usage.md)**,
settings in **[Configuration](https://github.com/sayak-sarkar/contextlake/blob/main/docs/configuration.md)**, the knowledge
layer and serving in **[Knowledge layer](https://github.com/sayak-sarkar/contextlake/blob/main/docs/knowledge-layer.md)**, and the whole flow start to
finish in **[QUICKSTART](https://github.com/sayak-sarkar/contextlake/blob/main/QUICKSTART.md)**.

## Install

```bash
pip install "contextlake[kb]"       # the full tool: mirror + graph, search, wiki, MCP server
pip install contextlake             # mirror-only core (one dependency: argcomplete)
```

Everything in the quickstart below needs the `[kb]` extra (Python 3.10+); the plain
install is just the mirroring CLI. Both need Python 3.10 or newer: one floor for the whole
tool, since the split floor the mirror core used to allow only ever surprised people.

Prefer an isolated, zero-setup install? [`uv`](https://docs.astral.sh/uv/) fetches the right
Python and an isolated environment for you:

```bash
uv tool install "contextlake[kb]"            # install the CLI on your PATH
uvx --from "contextlake[kb]" contextlake --help   # …or run it once, without installing
# pipx install "contextlake[kb]"             # pipx works too
```

Docker, the standalone binaries, the full extras table, upgrading, and uninstalling all live
on one page: **[Install and upgrade](https://github.com/sayak-sarkar/contextlake/blob/main/docs/install.md)**.
If an install misbehaves, see
**[Troubleshooting](https://github.com/sayak-sarkar/contextlake/blob/main/docs/troubleshooting.md)**.

**Prerequisites:** `git`, and, only for fleet mirroring, the platform's token env var
(`GITLAB_TOKEN` with `read_api` + `read_repository`, or `GITHUB_TOKEN` /
`BITBUCKET_TOKEN` / `GITEA_TOKEN`); on GitLab an authenticated
[`glab`](https://gitlab.com/gitlab-org/cli) works instead. The knowledge layer needs
neither. Once installed, `contextlake` and `python -m contextlake` are equivalent;
`python3 run-contextlake.py` is a source-checkout launcher and is not part of the installed package.

## Quickstart: one repo, no setup

You don't need GitLab or any config to try contextlake on a repo you already have.
No install? Run it once with [`uvx`](https://docs.astral.sh/uv/): prefix any command
below with `uvx --from "contextlake[kb]"` (e.g. `uvx --from "contextlake[kb]" contextlake kb index --source .`).

```bash
contextlake kb index                     # parse the current repo into a local knowledge graph
contextlake kb graph --overview --open   # open the graph (it names your repo's own view next)
contextlake kb serve                     # …or serve it to your AI IDE over MCP
```

**Wire it into your editor in one line**, no config file needed (it uses the local
`~/.contextlake/kb` store you just built):

```bash
claude mcp add contextlake-kb -- contextlake kb serve      # Claude Code
# zero-install variant: claude mcp add contextlake-kb -- uvx --from "contextlake[kb]" contextlake kb serve
```

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/graph.jpg" alt="The contextlake graph visualizer showing a repository's symbols as a navigable node graph, with a type-glyph legend, search, and a corner minimap" width="840">
</p>
<p align="center"><em><code>contextlake kb graph</code>, a whole codebase as one offline, navigable graph.</em></p>

Everything lands in a local store (`~/.contextlake/kb`), nothing leaves your machine. Index
any path with `--source PATH`, or every git repo under a directory with `--workspace DIR`.

> **Want the full path**, mirror a GitLab fleet → graph → wired editor in a few minutes?
> [**QUICKSTART.md**](https://github.com/sayak-sarkar/contextlake/blob/main/QUICKSTART.md) walks the whole flow.

## Fleet mode: mirror a whole org

Where contextlake goes beyond single-repo tools is mirroring and cross-referencing a *whole
fleet*: a GitLab group, a GitHub org, a Bitbucket workspace, or a Gitea/Codeberg/Forgejo
owner. Copy the example config and set your platform, group and workspace:

```bash
cp .contextlake.ini.example ~/.contextlake.ini
```
```ini
[contextlake]
work_dir = ~/work
gitlab_group = your-gitlab-group
# or any other platform:
# platform = github
# group = your-org
```

```bash
contextlake mirror status      # see where you stand (read-only)
contextlake mirror sync        # fetch → clone → update → branches → verify → audit
```

Auth is one env var: the platform's token (`GITLAB_TOKEN` / `GITHUB_TOKEN` /
`BITBUCKET_TOKEN` / `GITEA_TOKEN`), carried in headers and the child environment, never in
URLs or argv, so `.contextlake.ini` holds only non-secret settings and is gitignored by
default. (On GitLab, an authenticated `glab` works too; public orgs on other platforms need
no token at all.) It runs across hundreds of repos **concurrently**, with an adaptive worker
pool, retries with backoff, and **never stomps on the feature branch you're in the middle
of**.

> **Behind a slow / TLS-inspecting corporate proxy** (e.g. Zscaler) where `glab`'s API calls
> time out? Set `GITLAB_TOKEN` (a `read_api` token) and contextlake enumerates projects via
> its own HTTP client, which tolerates the slow DNS where `glab`'s short dial timeout fails.

## Commands at a glance

Run any command as `contextlake <command>`; each has scoped help via
`contextlake <command> --help`. Each verb lives under the noun it belongs to, `mirror` for
mirroring git repositories, `kb` for the knowledge layer, except `init`, `bootstrap`,
`version`, `completion`, and `doctor`, which span both tiers or neither. Per-command docs live
with their layer: the **mirror** commands in **[usage.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/usage.md)**;
the **knowledge-layer** build commands one page each, `kb index` in
**[index-code-graph.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/index-code-graph.md)**,
`kb connect`/`kb ingest`/`kb enrich` in
**[connect-enrich.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/connect-enrich.md)**,
`kb embed`/`kb eval` in
**[semantic-search.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/semantic-search.md)**
and `kb wiki` in
**[generate-wiki.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/generate-wiki.md)**
(with **[knowledge-layer.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/knowledge-layer.md)**
as the map over all four); the query commands
(`kb query`, `kb impact`, `kb owners`) in **[ask-the-graph.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/ask-the-graph.md)**;
and `kb serve`/`kb steer` in **[serve.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/serve.md)**.
The full flag-by-flag list is **[cli-reference.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/cli-reference.md)**.

| Command | What it does |
| --- | --- |
| `init` | **Guided setup**: write your mirror + knowledge-layer config (`--skip-interactive` for non-interactive) |
| `mirror status` | Show the workspace sync state vs GitLab (read-only) |
| `mirror sync` | The full pipeline: fetch → clone → update → branches → verify → audit |
| `mirror fetch` · `mirror clone` · `mirror update` | The sync steps, individually |
| `mirror branches` | Switch each repo to its most active branch |
| `mirror verify` · `mirror audit` | Check the mirror vs GitLab; report repo health, age & drift (JSON + CSV) |
| `bootstrap` | **Turnkey**: sync + index + connect + embed + enrich + wiki + diagram + steer (`--no-enrich` to skip a stage) |
| `kb index` | Build the code/dependency graph (`--workspace`, incremental, `--watch`; a directory holding git repos is refused with the right command, `--bundle` to index it as one repo anyway) |
| `kb source` | **Manage connectors**: `add`/`list`/`remove`/`test`/`enable`/`disable` knowledge sources; edits `kb.toml` for you, comments preserved |
| `kb connect` | Link repos to Atlassian / Figma / GitLab items (`--watch` to keep refreshing) |
| `kb embed` | Build semantic-search vectors (zero-config built-in CPU model, Ollama, or an API; incremental, `--watch`) |
| `kb enrich` | Query connected sources with codebase-derived terms and store the results in a searchable `@enrich` partition that feeds the wiki |
| `kb ingest` | Aggregate external docs into the graph + semantic store (built-in `files`/`web`/`api`/`graphql`/`mcp` sources, or plugins) |
| `kb wiki [<repo>…]` | LLM-synthesized, council-verified wiki pages (all repos, or just the named ones); `--llm builtin\|ollama\|openai\|anthropic\|cli\|auto` enables the LLM tier inline (`builtin` needs `doctor --fix llm-local` first on a `pip` install) |
| `kb query` | Search the index (`--kind`, `--repo`, `--as-of <commit>`) |
| `kb owners` (alias `kb who-knows`) | Likely owners / SMEs for a repo (or `--path`), ranked from git history |
| `kb impact` (alias `kb blast-radius`) | Change-impact / blast radius: what depends on a symbol (`--hops`, `--repo` to disambiguate) |
| `kb graph` | Visualize the graph, offline interactive HTML / DOT / Mermaid / JSON |
| `kb dashboard` | Local knowledge-system dashboard UI (`--serve`; `--sample` for the bundled demo fleet; `--site DIR` for a static offline export) |
| `kb serve` | Expose the graph over MCP (`--transport stdio`/`http`/`sse`; `--tool-concurrency N` bounds concurrent tool calls, default `2`, raising it makes the server slower) |
| `kb steer` | Write editor steering, `AGENTS.md`, `.mcp.json`, `.vscode/mcp.json`, `.windsurfrules`, skills |
| `kb lint` · `doctor` · `kb eval` | Graph health · environment check · retrieval-quality scoring |

Global options apply to any command: `-v`/`-q` (verbosity), `--log-file PATH`, `--config PATH`.
Two more read like globals and are not. `--dry-run` (preview without changing anything) belongs to
the 8 `mirror` commands plus `bootstrap`, `doctor` and `kb forget`; `--version` belongs to the bare
`contextlake` only, and `contextlake version` is the form that works everywhere. Pass either
somewhere it does not exist and the command exits `2` and names the commands that do take it.
Output is colorized on a TTY and plain when piped; set `NO_COLOR` to force-disable.

For runs nobody watches, the systemd timer in [`examples/`](https://github.com/sayak-sarkar/contextlake/tree/main/examples), cron, CI, there is a
second set: `--log-format json` (one JSON object per line, every line stamped with a run id),
`--metrics-file PATH` (Prometheus textfile-collector output), `--redact` (the `--log-file` copy
is already scrubbed of workspace paths, group and repo names), and `--access-log`. See
[Reading the console output](https://github.com/sayak-sarkar/contextlake/blob/main/docs/console-output.md).

## Knowledge layer

Beyond mirroring, the optional `contextlake.kb` layer turns your repos into a **knowledge
graph** and serves it to AI tools over **MCP**. It can link repos directly to the Atlassian /
Figma / GitLab / Slack items and code symbols that reference them, add **semantic search**,
write a curated **wiki**, **visualize** the graph
(offline interactive HTML, fleet overview, a symbol's neighbourhood, or a single repo), and
generate per-tool **steering files** + a skills library. Most of it needs no model; the rest
works with a local Ollama or any OpenAI-compatible endpoint.

One command sets it all up (configs are read from their default locations):

```bash
contextlake bootstrap
```

Full guide: **[docs/knowledge-layer.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/knowledge-layer.md)**.

### The dashboard

`contextlake kb dashboard --serve` opens a local, offline-first window into everything the
knowledge layer builds: a fleet overview, per-repo anatomy, the cross-repo architecture
graph, change-impact (blast radius), health, search, and a **Chat** tab to ask questions
about the fleet in plain language (free graph router always on, LLM-synthesized prose
opt-in via `--llm-chat`). Try it with zero setup via `contextlake kb dashboard --serve --sample`.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/fleet-cards.png" alt="The contextlake dashboard fleet overview: stat cards, a knowledge-confidence bar, and repos grouped by namespace, with a Cards/List/Table layout switcher." width="820">
</p>

**[The dashboard: a guided tour](https://github.com/sayak-sarkar/contextlake/blob/main/docs/dashboard.md)**, step by step, with screenshots.

## Local by default, and you can prove it

There is no telemetry, no analytics, no usage reporting and no crash reporting in
contextlake. There is nothing to opt out of, because there is nothing there.

That is easy for any project to type, so there is a switch that makes it checkable:

```bash
contextlake --offline kb index         # or CONTEXTLAKE_OFFLINE=1
```

`--offline` refuses every outbound connection **at the socket**, so it covers not only
contextlake's own requests but every library in the process, including the ones that
download embedding or language models. Loopback stays open, because the MCP server, the
dashboard, the graph viewer and a local Ollama all live there.

Verified with the network blocked, on a fresh store: `kb index`, `kb query`, `kb embed`,
semantic search, and `kb graph` (whose HTML output contains no remote references at all).
The commands that genuinely need the network say so and stop rather than failing obscurely:
mirroring from a forge refuses up front, and `bootstrap` skips the mirror stage and builds
the knowledge layer from what is already on disk.

**Two caveats, because they are the honest ones.** The bundled embedding model is
downloaded from Hugging Face the first time it is used; that fetch needs the network, and
afterwards it loads from the local cache and semantic search works offline. And the wiki's
LLM tier is only as local as the provider you point it at: the built-in `openvino-genai`
model runs on your machine once cached, while `--llm openai` is a hosted API and `--offline`
will and should block it.

The boundary is worth stating plainly: this is an in-process guard, so `git` and `glab`
subprocesses have their own sockets. That is exactly why the mirror stages refuse up
front under `--offline` instead of relying on the guard. Everything above is covered by
tests that try to escape it, including one that goes out through `urllib` rather than
through any of our own helpers.

Two ways to reach outside are opt-in and named: a **hosted model provider**, if you
configure one instead of the bundled local model, and `kb graph --cdn`, which swaps the
inlined JavaScript for CDN script tags to make a smaller file. Default output inlines
everything and opens in an air-gapped browser.

## Documentation

- **[QUICKSTART.md](https://github.com/sayak-sarkar/contextlake/blob/main/QUICKSTART.md)**, install → bootstrap → wire your editor, in minutes
- **[docs/install.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/install.md)**, every install channel, upgrading, and uninstalling
- **[docs/troubleshooting.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/troubleshooting.md)**, it broke, now what
- **[docs/dashboard.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/dashboard.md)**, the dashboard, a guided tour with screenshots
- **[docs/cli-reference.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/cli-reference.md)**, every command and flag, plus shell completion
- **[docs/console-output.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/console-output.md)**, decoding a run: glyphs, exit codes, JSON logs, metrics
- **[docs/configuration.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/configuration.md)**, where settings live and which one wins
- **[docs/usage.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/usage.md)**, the mirror commands and branch safety
- **[docs/knowledge-layer.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/knowledge-layer.md)**, the map over the four build stages below
- **[docs/index-code-graph.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/index-code-graph.md)**, `kb index`, and what the graph captures
- **[docs/connect-enrich.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/connect-enrich.md)**, `kb connect` / `kb ingest` / `kb enrich`, the nine source types
- **[docs/semantic-search.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/semantic-search.md)**, `kb embed` / `kb eval`, vectors and retrieval quality
- **[docs/generate-wiki.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/generate-wiki.md)**, `kb wiki`, the review council, per-subsystem pages
- **[docs/model-providers.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/model-providers.md)**, choosing an embeddings and wiki backend
- **[docs/visualize.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/visualize.md)**, `kb graph`, all 11 formats and the C4 diagram
- **[docs/ask-the-graph.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/ask-the-graph.md)**, `kb query`, `kb impact`, `kb owners`
- **[docs/serve.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/serve.md)**, serve the graph over MCP + wire your editor
- **[docs/keep-fresh.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/keep-fresh.md)**, bootstrap, scheduling, and re-indexing on commit
- **[docs/explained.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/explained.md)**, what changes for you, and why it is built this way
- **[docs/benchmarks.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/benchmarks.md)**, where the token/cost/correctness impact comes from, and how to measure it yourself
- **[docs/internals.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/internals.md)**, architecture and internals
- **[docs/releasing.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/releasing.md)**, maintainer runbook: versioning, tagging, publishing
- **[docs/style-guide.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/style-guide.md)**, the documentation style guide (voice, structure, formatting, terms)
- **[docs/brand.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/brand.md)**, palette, mascot, and asset usage
- **[CHANGELOG.md](https://github.com/sayak-sarkar/contextlake/blob/main/CHANGELOG.md)** · **[ROADMAP.md](https://github.com/sayak-sarkar/contextlake/blob/main/ROADMAP.md)** · **[CONTRIBUTING.md](https://github.com/sayak-sarkar/contextlake/blob/main/CONTRIBUTING.md)** · **[BRANDING.md](https://github.com/sayak-sarkar/contextlake/blob/main/BRANDING.md)**

## License

MIT, see [LICENSE](https://github.com/sayak-sarkar/contextlake/blob/main/LICENSE). Pebble the otter is the project mascot; *deep context, clear answers.*
