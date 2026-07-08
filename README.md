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
  <img src="https://img.shields.io/badge/python-3.10%2B%20(3.9%2B%20core)-blue" alt="Python 3.10+ for the knowledge layer, 3.9+ for the mirror core">
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
   **14 languages** plus **Terraform** infrastructure, **SQL** schema, and package manifests
   (npm / PyPI / NuGet / Maven), add **semantic search**, a council-verified **wiki** (each page
   reviewed and scored before publishing, low-confidence pages dropped), and **connectors** to
   Atlassian / Figma / GitLab.
3. **Serve**: expose it all over **MCP** and an offline interactive **graph visualizer**, so
   agents can answer *"where is `X` defined?"* or *"who calls `Y`?"* instead of grepping.

Each layer has its own guide: the mirror in **[Usage & config](https://github.com/sayak-sarkar/contextlake/blob/main/docs/usage.md)**, the knowledge
layer and serving in **[Knowledge layer](https://github.com/sayak-sarkar/contextlake/blob/main/docs/knowledge-layer.md)**, and the whole flow start to
finish in **[QUICKSTART](https://github.com/sayak-sarkar/contextlake/blob/main/QUICKSTART.md)**.

## Install

```bash
pip install "contextlake[kb]"       # the full tool: mirror + graph, search, wiki, MCP server
pip install contextlake             # mirror-only core (no pip dependencies at all)
```

Everything in the quickstart below needs the `[kb]` extra (Python 3.10+); the plain
install is just the mirroring CLI and runs on Python 3.9+.

Prefer an isolated, zero-setup install? [`uv`](https://docs.astral.sh/uv/) fetches the right
Python and an isolated environment for you:

```bash
uv tool install "contextlake[kb]"            # install the CLI on your PATH
uvx --from "contextlake[kb]" contextlake --help   # …or run it once, without installing
# pipx install "contextlake[kb]"             # pipx works too
```

<details>
<summary>Install extras (the mirror needs none — add these for the knowledge layer)</summary>

| Extra | Adds | When you need it |
| --- | --- | --- |
| `[kb]` | The knowledge layer: parse → graph → wiki → MCP server | Anything beyond mirroring |
| `[kb-full]` | `[kb]` + the built-in CPU embedder + sqlite-vec ANN | One-step local semantic search, no Ollama or API key |
| `[kb-vec]` | The sqlite-vec ANN backend | Faster vector search than the pure-Python fallback |
| `[kb-local]` | The built-in CPU embedder (model2vec, ~30 MB) | Semantic search with no Ollama or API key |
| `[kb-fastembed]` | A higher-quality ONNX embedder (~90 MB) | Better semantic ranking |
| `[llm-local]` | A built-in CPU model for the wiki (llama-cpp) | `wiki --llm builtin` with no Ollama or API key |

</details>

<details>
<summary>Docker (turnkey / air-gapped: models baked in)</summary>

The published image bundles the knowledge layer plus the built-in CPU models
(embedder + a small wiki LLM), so it runs with no Ollama, no API key, and no
model download at runtime. The PyPI wheel stays the primary install; reach for
the image on locked-down or offline machines.

```bash
docker run -v "$PWD:/work" ghcr.io/sayak-sarkar/contextlake doctor
docker run -v "$PWD:/work" ghcr.io/sayak-sarkar/contextlake index
```
</details>

<details>
<summary>From source (for contributors)</summary>

```bash
git clone https://github.com/sayak-sarkar/contextlake && cd contextlake
pip install -e ".[kb]"
```
</details>

<details>
<summary>Update &amp; uninstall</summary>

Upgrade in place (whichever installer you used):

```bash
pipx upgrade contextlake                       # pipx
pip install --upgrade "contextlake[kb-full]"   # pip
uv tool upgrade contextlake                     # uv
docker pull ghcr.io/sayak-sarkar/contextlake   # image
```

Your store and config carry forward — the graph re-indexes incrementally on the next
`index`/`sync`, so nothing needs migrating. Confirm with `contextlake --version` and
`contextlake doctor`.

Uninstall the tool, then optionally remove what it created (it never writes inside your
repos, so your source is never touched):

```bash
pipx uninstall contextlake        # or: pip uninstall contextlake
rm -rf ~/.contextlake             # store + kb.toml + graph/dashboard exports (optional)
rm -f  ~/.contextlake.ini         # mirror config (optional)
# mirrored repos live in your work_dir (default ~/work) — delete only if unwanted
```
</details>

**Prerequisites:** `git`, and, only for fleet mirroring, the platform's token env var
(`GITLAB_TOKEN` with `read_api` + `read_repository`, or `GITHUB_TOKEN` /
`BITBUCKET_TOKEN` / `GITEA_TOKEN`); on GitLab an authenticated
[`glab`](https://gitlab.com/gitlab-org/cli) works instead. The knowledge layer needs
neither. Once installed, `contextlake`, `python -m contextlake`, and
`python3 contextlake.py` are equivalent.

## Quickstart: one repo, no setup

You don't need GitLab or any config to try contextlake on a repo you already have.
No install? Run it once with [`uvx`](https://docs.astral.sh/uv/): prefix any command
below with `uvx --from "contextlake[kb]"` (e.g. `uvx --from "contextlake[kb]" contextlake index --source .`).

```bash
contextlake index                     # parse the current repo into a local knowledge graph
contextlake graph --overview --open   # open the interactive graph in your browser
contextlake serve                     # …or serve it to your AI IDE over MCP
```

**Wire it into your editor in one line**, no config file needed (it uses the local
`~/.contextlake/kb` store you just built):

```bash
claude mcp add contextlake-kb -- contextlake serve      # Claude Code
# zero-install variant: claude mcp add contextlake-kb -- uvx --from "contextlake[kb]" contextlake serve
```

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/graph.jpg" alt="The contextlake graph visualizer showing a repository's symbols as a navigable node graph, with a type-glyph legend, search, and a corner minimap" width="840">
</p>
<p align="center"><em><code>contextlake graph</code>, a whole codebase as one offline, navigable graph.</em></p>

Everything lands in a local store (`~/.contextlake/kb`), nothing leaves your machine. Index
any path with `--source PATH`, or every git repo under a directory with `--workspace DIR`.

> **Want the full path**, mirror a GitLab fleet → graph → wired editor in a few minutes?
> [**QUICKSTART.md**](https://github.com/sayak-sarkar/contextlake/blob/main/QUICKSTART.md) walks the whole flow.

## Fleet mode: mirror a whole org

Where contextlake goes beyond single-repo tools is mirroring and cross-referencing a *whole
fleet* — a GitLab group, a GitHub org, a Bitbucket workspace, or a Gitea/Codeberg/Forgejo
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
contextlake status      # see where you stand (read-only)
contextlake sync        # fetch → clone → update → branches → verify → audit
```

Auth is one env var — the platform's token (`GITLAB_TOKEN` / `GITHUB_TOKEN` /
`BITBUCKET_TOKEN` / `GITEA_TOKEN`), carried in headers and the child environment, never in
URLs or argv — so `.contextlake.ini` holds only non-secret settings and is gitignored by
default. (On GitLab, an authenticated `glab` works too; public orgs on other platforms need
no token at all.) It runs across hundreds of repos **concurrently**, with an adaptive worker
pool, retries with backoff, and **never stomps on the feature branch you're in the middle
of**.

> **Behind a slow / TLS-inspecting corporate proxy** (e.g. Zscaler) where `glab`'s API calls
> time out? Set `GITLAB_TOKEN` (a `read_api` token) and contextlake enumerates projects via
> its own HTTP client, which tolerates the slow DNS where `glab`'s short dial timeout fails.

## Commands at a glance

Run any command as `contextlake <command>`; each has scoped help via
`contextlake <command> --help`. Per-command docs live with their layer: the
**mirror** commands in **[usage.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/usage.md)**;
the **knowledge-layer** commands (`index`, `embed`, `connect`, `wiki`, `query`, `owners`,
`impact`, `graph`, …) in **[knowledge-layer.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/knowledge-layer.md)**,
and `serve`/`steer` in **[serve.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/serve.md)**.

| Command | What it does |
| --- | --- |
| `init` | **Guided setup**: write your mirror + knowledge-layer config (`--yes` for non-interactive) |
| `status` | Show the workspace sync state vs GitLab (read-only) |
| `sync` | The full pipeline: fetch → clone → update → branches → verify → audit |
| `fetch` · `clone` · `update` | The sync steps, individually |
| `branches` | Switch each repo to its most active branch |
| `verify` · `audit` | Check the mirror vs GitLab; report repo health, age & drift (JSON + CSV) |
| `bootstrap` | **Turnkey**: sync + index + connect + embed + wiki + steer |
| `index` | Build the code/dependency graph (`--workspace`, incremental, `--watch`) |
| `connect` | Link repos to Atlassian / Figma / GitLab items (`--watch` to keep refreshing) |
| `embed` | Build semantic-search vectors (zero-config built-in CPU model, Ollama, or an API; incremental, `--watch`) |
| `ingest` | Aggregate external docs into the graph + semantic store (built-in `files`/`web`/`api`/`mcp` sources, or plugins) |
| `wiki [<repo>…]` | LLM-synthesized, council-verified wiki pages (all repos, or just the named ones); `--llm builtin\|ollama\|openai\|anthropic\|cli` enables the LLM tier inline |
| `query` | Search the index (`--kind`, `--repo`, `--as-of <commit>`) |
| `owners` (alias `who-knows`) | Likely owners / SMEs for a repo (or `--path`), ranked from git history |
| `impact` (alias `blast-radius`) | Change-impact / blast radius: what depends on a symbol (`--hops`, `--repo` to disambiguate) |
| `graph` | Visualize the graph, offline interactive HTML / DOT / Mermaid / JSON |
| `dashboard` | Local knowledge-system dashboard UI (`--serve`; `--sample` for the bundled demo fleet; `--site DIR` for a static offline export) |
| `serve` | Expose the graph over MCP (`--transport stdio`/`http`) |
| `steer` | Write editor steering, `AGENTS.md`, `.mcp.json`, `.windsurfrules`, skills |
| `lint` · `doctor` · `eval` | Graph health · environment check · retrieval-quality scoring |

Global options apply to any command: `--dry-run` (preview without changing anything),
`-v`/`-q` (verbosity), `--log-file PATH`, `--config PATH`, `--version`. Output is colorized on
a TTY and plain when piped; set `NO_COLOR` to force-disable.

## Knowledge layer

Beyond mirroring, the optional `contextlake.kb` layer turns your repos into a **knowledge
graph** and serves it to AI tools over **MCP**. It can link repos to their Atlassian / Figma /
GitLab items, add **semantic search**, write a curated **wiki**, **visualize** the graph
(offline interactive HTML, fleet overview, a symbol's neighbourhood, or a single repo), and
generate per-tool **steering files** + a skills library. Most of it needs no model; the rest
works with a local Ollama or any OpenAI-compatible endpoint.

One command sets it all up (configs are read from their default locations):

```bash
contextlake bootstrap
```

Full guide: **[docs/knowledge-layer.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/knowledge-layer.md)**.

### The dashboard

`contextlake dashboard --serve` opens a local, offline-first window into everything the
knowledge layer builds — a fleet overview, per-repo anatomy, the cross-repo architecture
graph, change-impact (blast radius), health, and search. Try it with zero setup via
`contextlake dashboard --serve --sample`.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/fleet-cards.png" alt="The contextlake dashboard fleet overview: stat cards, a knowledge-confidence bar, and repos grouped by namespace, with a Cards/List/Table layout switcher." width="820">
</p>

**[The dashboard — a guided tour](https://github.com/sayak-sarkar/contextlake/blob/main/docs/dashboard.md)** — step by step, with screenshots.

## Documentation

- **[QUICKSTART.md](https://github.com/sayak-sarkar/contextlake/blob/main/QUICKSTART.md)**, install → bootstrap → wire your editor, in minutes
- **[docs/dashboard.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/dashboard.md)**, the dashboard, a guided tour with screenshots
- **[docs/usage.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/usage.md)**, every command, configuration, branch safety, scheduling
- **[docs/knowledge-layer.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/knowledge-layer.md)**, the graph, connectors, search, wiki
- **[docs/serve.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/serve.md)**, serve the graph over MCP + wire your editor
- **[docs/benchmarks.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/benchmarks.md)**, an honest, measured look at the token/cost/correctness impact
- **[docs/internals.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/internals.md)**, architecture & internals
- **[docs/releasing.md](https://github.com/sayak-sarkar/contextlake/blob/main/docs/releasing.md)**, maintainer runbook: versioning, tagging, publishing
- **[CHANGELOG.md](https://github.com/sayak-sarkar/contextlake/blob/main/CHANGELOG.md)** · **[ROADMAP.md](https://github.com/sayak-sarkar/contextlake/blob/main/ROADMAP.md)** · **[CONTRIBUTING.md](https://github.com/sayak-sarkar/contextlake/blob/main/CONTRIBUTING.md)** · **[BRANDING.md](https://github.com/sayak-sarkar/contextlake/blob/main/BRANDING.md)**

## License

MIT, see [LICENSE](https://github.com/sayak-sarkar/contextlake/blob/main/LICENSE). Pebble the otter is the project mascot; *deep context, clear answers.*
