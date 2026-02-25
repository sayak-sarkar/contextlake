# contextlake

> **Mirror every GitLab repo you can touch — cloned, current, and on the branch that's actually alive.**

[![CI](https://github.com/sayak-sarkar/contextlake/actions/workflows/ci.yml/badge.svg)](https://github.com/sayak-sarkar/contextlake/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

You have access to dozens — maybe hundreds — of repositories scattered across a
GitLab group and its subgroups. You want them all on your laptop, in the same
shape they have on GitLab, each sitting on the branch where the real work is
happening, and you want a single command to keep it that way.

That's the whole job. `contextlake` enumerates everything you can reach, clones
what's missing into a faithful mirror of the namespace tree, pulls what's stale,
and parks each repo on its most active branch — concurrently, with retries, and
**without ever stomping on the feature branch you're in the middle of.**

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

**Prerequisites:** Python 3.9+ (3.10+ for the knowledge layer), `git`, and an
authenticated [`glab`](https://gitlab.com/gitlab-org/cli) (`glab auth login`).

```bash
pipx install "git+https://github.com/sayak-sarkar/contextlake"   # isolated CLI
# or:  pip install .          (add the [kb] extra for the knowledge layer)
```

Once installed, `contextlake`, `python -m contextlake`, and `python3 contextlake.py`
are equivalent.

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

## Usage

Run commands as `contextlake <command>` — full per-command docs are in
**[docs/usage.md](docs/usage.md)**.

### Commands at a glance

| Command | What it does |
| --- | --- |
| `status` | Show the workspace sync state vs GitLab (read-only) |
| `fetch` | Cache the GitLab project list |
| `clone` | Clone repos that exist on GitLab but not locally |
| `update` | Pull updates for local repos (skips in-progress working branches) |
| `branches` | Switch each repo to its most active branch |
| `verify` | Check the local mirror matches GitLab (drift, orphans, nesting) |
| `sync` | The full pipeline: fetch → clone → update → branches → verify |
| `bootstrap` | **Turnkey**: sync + index + connect + embed + wiki + steer |
| `index` | Build the code/dependency graph (`--workspace`, incremental, `--watch`) |
| `connect` | Link repos to Atlassian / Figma / GitLab sources |
| `embed` | Build semantic-search vectors (needs an embeddings model) |
| `lint` | Graph health — stale repos (HEAD moved) and dangling edges; exits non-zero if any |
| `wiki` | LLM-synthesized, council-verified wiki pages (needs a model) |
| `steer` | Write editor steering — `AGENTS.md`, `.mcp.json`, `.windsurfrules`, skills |
| `serve` | Expose the graph over MCP (`--transport stdio`/`http`) |
| `query` | Search the index (`--kind`, `--repo`, `--limit`, `--as-of <commit>`) |
| `doctor` | Check the knowledge-layer environment (SQLite FTS5, git/glab, store, embeddings) |

The first seven are the core sync (detailed below); the rest are the optional
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
write a curated wiki, and generate per-tool steering files + a skills library. Most of
it needs no model; the rest works with a local Ollama or any OpenAI-compatible endpoint.

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
