# Quick start

From a fresh machine to a fully-wired AI workspace, your GitLab repos mirrored,
indexed into a local knowledge graph, and exposed to Claude Code / Windsurf / Kiro,
in a few minutes. Everything beyond the mirror is optional and off by default.

## 1. Prerequisites

- **Python 3.10+**, one floor for the whole tool, mirror and knowledge layer alike
- **`git`**, plus your platform's token: **`GITLAB_TOKEN`** (a PAT with `read_api` +
  `read_repository`), or `GITHUB_TOKEN` / `BITBUCKET_TOKEN` / `GITEA_TOKEN` with
  `platform = github|bitbucket|gitea` in the config. On GitLab, an authenticated
  **`glab`** (`glab auth login`) works instead of a token.

## 2. Install

```bash
pipx install "contextlake[kb-full]"
```

`pipx` keeps contextlake in its own environment and still puts the command on your PATH.
`[kb-full]` is the batteries-included bundle: the knowledge layer plus the built-in CPU
embedder (no Ollama, no API key) and the fast `sqlite-vec` backend, so semantic search works
the moment you turn embeddings on. That gives you the `contextlake` command
(`python -m contextlake` and `python3 run-contextlake.py` work too).

Using pip, uv, Docker, or a standalone binary instead, or picking extras individually? Every
channel, the extras table, upgrading, and uninstalling are on one page:
**[Install and upgrade](docs/install.md)**. Verify whichever you chose with:

```bash
contextlake --version
contextlake doctor
```

`doctor` reports what is present and what is missing. If it names something you cannot
resolve, [Troubleshooting](docs/troubleshooting.md) covers the failures that come up most.

## 3. Configure

The fast path, `contextlake init` writes both config files for you (interactive, or
`--skip-interactive` for defaults):

```bash
contextlake init                       # prompts for platform, group, workspace
contextlake init --platform github --group my-org --skip-interactive   # non-interactive
contextlake init --local               # scope config to this project instead of ~/
```

Working across more than one org or project? `--local` writes config into the current directory instead
of `~/`; every subdirectory underneath it inherits it automatically (see
[Directory-scoped config](docs/configuration.md#directory-scoped-config)).

Prefer to write them by hand? **Mirror config**, `~/.contextlake.ini`:

```ini
[contextlake]
work_dir = ~/work
# platform = github          # gitlab (default) | github | bitbucket | gitea | codeberg
gitlab_group = your-gitlab-group
```

**Knowledge-layer config**, `~/.contextlake/kb.toml` (copy
[`examples/kb.toml.example`](examples/kb.toml.example) and keep what you need):

```toml
[kb]
store_dir = "~/.contextlake/kb"

# Link each repo to its open GitLab merge requests + issues (uses your glab login):
[[sources]]
type = "gitlab"
name = "gitlab"
group = "your-gitlab-group"
```

Semantic search and the curated wiki need a model, enable `[embeddings]` / `[llm]`
in `kb.toml` pointing at a local Ollama **or any OpenAI-compatible endpoint** (hosted
key or a local server like LM Studio / Jan). The key is read from an env var, never
stored in config. Everything here is optional; the graph and search work with no
model at all.

## 4. Bootstrap, one command

```bash
contextlake bootstrap --llm builtin
```

Both config files are read from the default locations above; pass `--config` /
`--kb-config` only if you keep them elsewhere.

This mirrors your repos and builds the **entire** knowledge layer in one command:
graph → connectors → semantic vectors → **curated wiki** → editor steering. Everything
generated (graph, vectors, wiki pages, exports) lands under a single `store_dir`, so
pointing that at a workspace folder (e.g. `store_dir = "~/work/my-kb"`) keeps the whole
knowledge base in one easy-to-find place.

`--llm builtin` powers the wiki with a local CPU model (Qwen2.5-0.5B, downloaded once) via
the `llm-local` extra. The **standalone binary** already has it configured (it installs it on
first run) and the **full Docker image** ships it baked in, so neither needs anything here.
On a pip install:

```bash
contextlake doctor --fix llm-local
```

That installs into the interpreter contextlake is running in, with the upstream CPU wheel
index attached, and prints the exact command before running it (`--dry-run` prints it and
stops). The index is not optional, because `llama-cpp-python` publishes no wheels to PyPI at
all; for the command by hand see [Install and
upgrade](docs/install.md#the-built-in-wiki-llm-needs-one-extra-flag), and for why, see
[Installing the built-in
LLM](docs/model-providers.md#installing-the-built-in-llm-and-why-it-needs-a-wheel-index).

Prefer `--llm ollama` (no compiler needed at all) or `--llm openai` for higher-quality prose; without any `--llm`
(and without `[llm]` enabled in `kb.toml`) the wiki stage is skipped. Useful toggles:

- already have the repos cloned? add `--no-sync`
- no model configured yet? drop `--llm` and add `--no-embed` (graph + search still build)

## 5. Wire your editor

`bootstrap` already wrote `.mcp.json`, `AGENTS.md`, `CLAUDE.md`, `.windsurfrules`,
`.kiro/steering/`, and a `.claude/skills/` + `.windsurf/workflows/` library into your
workspace. To register the server with **Claude Code** explicitly:

```bash
claude mcp add contextlake-kb -- contextlake kb serve --config ~/.contextlake/kb.toml
```

**Windsurf / Devin** and **Kiro** pick up the generated config and rules
automatically. Now ask your agent: *"where is `CatalogService` defined?"*, *"who calls
`charge`?"*, *"which repos depend on `shared-core`?"*, it queries the graph and cites
files instead of guessing. The installed skills give even a small-context model a
strong operating playbook.

## 6. Keep it fresh

`bootstrap` is **incremental and branch-safe**, it re-indexes only repos whose HEAD
moved and never touches an in-progress working tree, so it's safe to run on a
schedule. Use cron:

```cron
*/30 * * * * contextlake bootstrap >> ~/.contextlake/refresh.log 2>&1
```

or the systemd user units in [`examples/`](examples/). See the
[README](README.md) for the full command reference and configuration.
