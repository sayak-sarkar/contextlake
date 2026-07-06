# Quick start

From a fresh machine to a fully-wired AI workspace, your GitLab repos mirrored,
indexed into a local knowledge graph, and exposed to Claude Code / Windsurf / Kiro, 
in a few minutes. Everything beyond the mirror is optional and off by default.

## 1. Prerequisites

- **Python 3.10+** (the knowledge layer needs 3.10; the core sync works on 3.9+)
- **`git`**, plus your platform's token: **`GITLAB_TOKEN`** (a PAT with `read_api` +
  `read_repository`), or `GITHUB_TOKEN` / `BITBUCKET_TOKEN` / `GITEA_TOKEN` with
  `platform = github|bitbucket|gitea` in the config. On GitLab, an authenticated
  **`glab`** (`glab auth login`) works instead of a token.

## 2. Install

```bash
pipx install "contextlake[kb]"
# or from a clone:  pip install ".[kb]"
# optional ANN backend for semantic search:  add the [kb-vec] extra
```

This gives you the `contextlake` command. (`python -m contextlake` and
`python3 contextlake.py` work too.)

## 3. Configure

The fast path, `contextlake init` writes both config files for you (interactive, or
`--yes` for defaults):

```bash
contextlake init                       # prompts for platform, group, workspace
contextlake init --platform github --group my-org --yes   # non-interactive
```

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
contextlake bootstrap
```

Both config files are read from the default locations above; pass `--config` /
`--kb-config` only if you keep them elsewhere.

It mirrors your repos, indexes them into the graph, runs your connectors, and writes
the editor steering. Useful toggles:

- already have the repos cloned? add `--no-sync`
- no model configured yet? add `--no-embed --no-wiki`

## 5. Wire your editor

`bootstrap` already wrote `.mcp.json`, `AGENTS.md`, `CLAUDE.md`, `.windsurfrules`,
`.kiro/steering/`, and a `.claude/skills/` + `.windsurf/workflows/` library into your
workspace. To register the server with **Claude Code** explicitly:

```bash
claude mcp add contextlake-kb -- contextlake serve --config ~/.contextlake/kb.toml
```

**Windsurf / Devin** and **Kiro** pick up the generated config and rules
automatically. Now ask your agent: *"where is `OrderService` defined?"*, *"who calls
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
