<p align="center">
  <img src="docs/branding/glyph.svg" alt="" width="76" height="76">
</p>
<h1 align="center">contextlake</h1>
<p align="center"><em>All your real context, in one local lake.</em></p>

<p align="center">
  <a href="https://github.com/sayak-sarkar/contextlake/actions/workflows/ci.yml"><img src="https://github.com/sayak-sarkar/contextlake/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/contextlake/"><img src="https://img.shields.io/pypi/v/contextlake?color=137A8B" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
</p>

> **A local context layer for your AI tools — your repositories mirrored, indexed into a knowledge graph, and served over MCP, so agents work from real source instead of guessing.**

You have access to dozens — maybe hundreds — of repositories scattered across a
GitLab group and its subgroups. You want them all on your laptop, in the same
shape they have on GitLab, each sitting on the branch where the real work is
happening, and you want a single command to keep it that way.

That's the foundation. `contextlake` enumerates everything you can reach, clones
what's missing into a faithful mirror of the namespace tree, pulls what's stale,
and parks each repo on its most active branch — concurrently, with retries, and
**without ever stomping on the feature branch you're in the middle of.**

On top of that mirror, an optional [knowledge layer](#knowledge-layer-optional)
indexes everything into a graph and serves it to your AI tools over MCP — so they
answer from real source. (Today the source is GitLab; the design is source-agnostic.)

It carries no credentials of its own: authentication rides entirely on your
existing [`glab`](https://gitlab.com/gitlab-org/cli) login and `git` setup.

```bash
pip install .
contextlake status      # see where you stand
contextlake sync        # fetch → clone → update → branches → verify
```

> **New here?** [**QUICKSTART.md**](QUICKSTART.md) takes you from install to a
> fully-wired AI workspace (mirror → knowledge graph → Claude Code / Windsurf) in a
> few minutes.

## What's in the box

**The core loop**

- **Discovers everything** in a GitLab group and its subgroups via the API.
- **Clones what's missing**, preserving GitLab's exact directory structure.
- **Updates what's stale** with a fast-forward pull, honestly reporting whether
  anything actually changed.
- **Rides the active branch** — picks each repo's liveliest branch by commit
  count, recency, or a hybrid of both (your call).
- **Verifies the mirror** against GitLab and flags drift, orphans, and
  repos-nested-inside-repos.

**Because it runs across hundreds of repos**

- **Concurrent** by default, with an **adaptive worker pool** that backs off when
  the network starts misbehaving and ramps back up when it recovers.
- **Resilient** — exponential backoff with jitter on transient failures, fail-fast
  on the ones that won't recover (DNS, TLS).

**Because it's your working machine**

- **Branch safety**: never yanks you off a working branch or clobbers uncommitted
  changes — skip, or `--auto-stash`, your choice.
- **`--dry-run`** everything first if you're the cautious type.
- **Configurable** via INI files (local + global) with sensible precedence, plus
  per-run CLI overrides.

## Installation

The fastest, zero-config path is [`uv`](https://docs.astral.sh/uv/) — it fetches
the right Python and an isolated environment for you, so there's nothing to set up:

```bash
uv tool install "contextlake[kb]"        # install the CLI on your PATH
# or run it once, ephemerally, without installing:
uvx --from "contextlake[kb]" contextlake --help
```

Prefer pipx or pip? Those work too:

```bash
pipx install "contextlake[kb]"
# pip install "contextlake[kb]"          # into an active virtualenv
```

The **`[kb]` extra** pulls in the knowledge layer (graph index, embeddings,
LLM-wiki, MCP server). Plain `contextlake` is just the GitLab-mirroring CLI.

**Other prerequisites:** `git`, and — for mirroring — an authenticated
[`glab`](https://gitlab.com/gitlab-org/cli) (`glab auth login`). Once installed,
`contextlake`, `python -m contextlake`, and `python3 contextlake.py` are equivalent.

**Configure** — copy the example and set your group + workspace:

```bash
cp .contextlake.ini.example ~/.contextlake.ini
```
```ini
[contextlake]
work_dir = ~/work
gitlab_group = your-gitlab-group
```

The tool carries no credentials of its own — auth rides on `glab` — so
`.contextlake.ini` holds only non-secret settings and is gitignored by default. The
full option reference is in [docs/usage.md](docs/usage.md).

> **Behind a slow / TLS-inspecting corporate proxy** (e.g. Zscaler) where `glab`'s API
> calls time out, set `GITLAB_TOKEN` (a `read_api` token) — contextlake then enumerates
> projects via its own HTTP client, which tolerates the slow DNS where `glab`'s short
> dial timeout fails.

## Usage

Run commands as `contextlake <command>` — full per-command docs are in
**[docs/usage.md](docs/usage.md)**.

### Commands at a glance

| Command | What it does |
| --- | --- |
| `status` | Show the workspace sync state vs GitLab (read-only) |
| `fetch` | Cache the GitLab project list |
| `clone` | Clone repos that exist on GitLab but not locally |
| `update` | Pull updates for local repos (skips only repos with a dirty working tree) |
| `branches` | Switch each repo to its most active branch |
| `verify` | Check the local mirror matches GitLab (drift, orphans, nesting) |
| `sync` | The full pipeline: fetch → clone → update → branches → verify → audit |
| `audit` | Repo health & age: empty/README-only repos + creation & last-commit dates (JSON + CSV) |
| `bootstrap` | **Turnkey**: sync + index + connect + embed + wiki + steer |
| `index` | Build the code/dependency graph (`--workspace`, incremental, `--watch`) |
| `connect` | Link repos to Atlassian / Figma / GitLab sources |
| `embed` | Build semantic-search vectors (zero-config built-in CPU model, or Ollama / an API) |
| `lint` | Graph health — stale repos (HEAD moved) and dangling edges; exits non-zero if any |
| `wiki` | LLM-synthesized, council-verified wiki pages (zero-config built-in model, or Ollama / an API) |
| `steer` | Write editor steering — `AGENTS.md`, `.mcp.json`, `.windsurfrules`, skills |
| `serve` | Expose the graph over MCP (`--transport stdio`/`http`) |
| `query` | Search the index (`--kind`, `--repo`, `--limit`, `--as-of <commit>`) |
| `graph` | Visualize the graph — offline interactive HTML / DOT / Mermaid / JSON (`--overview`, `--serve`) |
| `doctor` | Check the knowledge-layer environment (SQLite FTS5, git/glab, store, embeddings) |

The first eight are the core sync (detailed below); the rest are the optional
**[knowledge layer](#knowledge-layer)**. Run any command with `--config` (sync INI)
and, for the knowledge layer, `--config`/`--kb-config` pointing at your `kb.toml`.

### Global options

These apply to any command:

- `--dry-run` — preview clone/update/branch actions without changing anything.
- `-v` / `--verbose`, `-q` / `--quiet` — control console verbosity.
- `--log-file PATH` — append a full timestamped audit log (rotating).
- `--config PATH` — use a specific config file (highest precedence).
- `--version` — print the version and exit.

Output is colorized on a terminal (status glyphs, a progress bar); set `NO_COLOR`
to disable or `FORCE_COLOR` to keep colours when piping. Colours are dropped
automatically for non-TTY output (pipes, cron, log files).

A read-only `status` followed by a `--dry-run sync` is the safest way to preview
what a sync would do:

```bash
contextlake status
contextlake --dry-run sync
```

## Knowledge layer (optional)

Beyond mirroring, an optional layer (`contextlake.kb`) turns your repos into a
**knowledge graph** and serves it to AI tools over **MCP** — so Claude Code, Windsurf,
or Kiro can answer *"where is `X` defined?"* or *"who calls `Y`?"* instead of grepping.
It can also link repos to their Atlassian / Figma / GitLab items, add semantic search,
write a curated wiki, **visualize the graph** (`contextlake graph` → an offline, interactive
HTML — fleet overview, a symbol's neighbourhood, or a single repo), and generate per-tool
steering files + a skills library. Most of it needs no model; the rest works with a local
Ollama or any OpenAI-compatible endpoint.

One command sets it all up:

```bash
contextlake bootstrap --kb-config ~/.contextlake/kb.toml
```

→ Full guide: **[docs/knowledge-layer.md](docs/knowledge-layer.md)**.

## Documentation

- **[QUICKSTART.md](QUICKSTART.md)** — install → bootstrap → wire your editor, in minutes
- **[docs/usage.md](docs/usage.md)** — every command, configuration, branch safety, scheduling
- **[docs/knowledge-layer.md](docs/knowledge-layer.md)** — the graph, connectors, search, wiki, steering
- **[docs/internals.md](docs/internals.md)** — architecture & internals
- **[docs/releasing.md](docs/releasing.md)** — maintainer runbook: versioning, tagging, publishing to PyPI
- **[BRANDING.md](BRANDING.md)** — brand guide (name, palette, logo, mascot)
- **[CHANGELOG.md](CHANGELOG.md)** · **[ROADMAP.md](ROADMAP.md)** · **[CONTRIBUTING.md](CONTRIBUTING.md)**

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For issues or questions:

1. Check this documentation first
2. Review log files for error messages
3. Test individual commands to isolate issues
4. Verify `glab` authentication: `glab auth status`
5. Check GitLab access permissions in web interface
