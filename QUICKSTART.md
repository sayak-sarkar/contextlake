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
pipx install "contextlake[kb-full]"   # graph + semantic search, zero config (recommended)
# lighter:  pipx install "contextlake[kb]"   # graph + search, but bring your own embedder
# or from a clone:  pip install ".[kb-full]"
```

**`[kb-full]`** is the batteries-included install: the knowledge layer plus the
built-in CPU embedder (no Ollama, no API key) and the fast `sqlite-vec` backend — so
semantic search and the `ask` tool work the moment you turn embeddings on, with no
extra downloads or warnings. Plain **`[kb]`** is the graph + full-text search only; add
`[kb-local]` for the embedder and `[kb-vec]` for the ANN backend if you prefer to pick.

This gives you the `contextlake` command. (`python -m contextlake` and
`python3 contextlake.py` work too.)

### Update to a newer version

```bash
pipx upgrade contextlake                       # if you installed with pipx
pip install --upgrade "contextlake[kb-full]"   # if you installed with pip
uv tool upgrade contextlake                     # if you installed with uv
docker pull ghcr.io/sayak-sarkar/contextlake   # if you use the image
```

Then confirm with `contextlake --version` and re-check your environment with
`contextlake doctor`. Your existing store and config carry forward — the graph
re-indexes incrementally on your next `index`/`sync`, so there is nothing to migrate by
hand. See the [changelog](changelog.html) for what changed between versions.

### Install scenarios & flag cheatsheet

Real setups and the exact command for each. What the flags mean:

- **`-U` / `--upgrade`** — move an already-installed contextlake to the newest version
  (without it, pip sees it installed and does nothing).
- **`--only-binary :all:`** — install from prebuilt **wheels only, never build from
  source** (`:all:` = every package; opposite is `--no-binary`). On a machine with no
  C/C++ compiler this turns a confusing build failure into a clean "no wheel available"
  message. It errors if a wheel truly doesn't exist — which is the point: a clear signal
  beats a doomed compile.
- **`--extra-index-url URL`** — also look for wheels at `URL` (e.g. the `llama-cpp-python`
  CPU-wheel index that PyPI doesn't mirror). See [Why the built-in LLM needs a prebuilt
  wheel](knowledge-layer.html#why-the-built-in-llm-needs-a-prebuilt-wheel-or-a-compiler).
- **`[extra]`** — an optional feature bundle: `[kb-full]` (recommended: graph + search +
  built-in embedder + `sqlite-vec` ANN), `[kb]` (graph + full-text only), `[kb-local]` /
  `[kb-vec]` (pick embedder / ANN yourself), `[llm-local]` (the built-in wiki model).

| Your situation | Command |
| --- | --- |
| "Just mirror my repos, nothing else." | `pipx install contextlake` |
| "Full knowledge layer, zero config." | `pipx install "contextlake[kb-full]"` (or `uvx --from "contextlake[kb-full]" contextlake`) |
| "Upgrade to the latest." | `pip install -U "contextlake[kb-full]"` (or `pipx upgrade contextlake`) |
| "Brand-new Python (e.g. 3.14), no compiler, a source build just failed." | `pip install -U --only-binary :all: "contextlake[kb-full]"` — for the built-in wiki LLM (`[llm-local]`) also add `--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu` |
| "I don't want any local toolchain at all." | Use the image: `docker pull ghcr.io/sayak-sarkar/contextlake` (bundles every dep + the model — no compiler, no wheels to chase) |

### Uninstall

Remove the tool:

```bash
pipx uninstall contextlake                     # or:  pip uninstall contextlake
docker rmi ghcr.io/sayak-sarkar/contextlake    # if you pulled the image
```

That leaves your data in place. contextlake never writes inside your repositories, so
uninstalling it can't touch your source. To also remove what it created locally —
all optional, delete only what you don't want to keep:

```bash
rm -rf ~/.contextlake        # knowledge store, kb.toml config, graph/dashboard exports, wiki
rm -f  ~/.contextlake.ini    # mirror config
# your mirrored repos live in your work_dir (default ~/work); delete only if unwanted:
# rm -rf ~/work
# the built-in CPU models are cached under ~/.cache/huggingface (shared with other HF
# tools) — remove just the contextlake ones to reclaim space:
rm -rf ~/.cache/huggingface/hub/models--minishlab--potion-base-8M
```

(If you used project-local config, also remove `.contextlake.kb.toml` /
`.contextlake.ini` from those project directories.)

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
contextlake bootstrap --llm builtin
```

Both config files are read from the default locations above; pass `--config` /
`--kb-config` only if you keep them elsewhere.

This mirrors your repos and builds the **entire** knowledge layer in one command:
graph → connectors → semantic vectors → **curated wiki** → editor steering. Everything
generated (graph, vectors, wiki pages, exports) lands under a single `store_dir`, so
pointing that at a workspace folder (e.g. `store_dir = "~/work/my-kb"`) keeps the whole
knowledge base in one easy-to-find place.

`--llm builtin` powers the wiki with a local CPU model (Qwen2.5-0.5B, downloaded once)
via the `llm-local` extra — `pip install "contextlake[llm-local]"`. If that extra fails
to build (`llama-cpp-python` has no prebuilt wheel for your Python, e.g. 3.14, and no
compiler is installed), install the CPU wheel directly, no compiler needed. The
`--only-binary :all:` flag makes pip refuse a source build, so you get a clean error
instead of a compiler-error wall:

```bash
pip install --only-binary :all: llama-cpp-python \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

Prefer `--llm ollama` or `--llm openai` for higher-quality prose; without any `--llm`
(and without `[llm]` enabled in `kb.toml`) the wiki stage is skipped. Useful toggles:

- already have the repos cloned? add `--no-sync`
- no model configured yet? drop `--llm` and add `--no-embed` (graph + search still build)

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
