# Knowledge layer

An optional subsystem (`contextlake.kb`) turns your mirrored repositories into a
queryable **knowledge graph** and serves it to AI agents over **MCP**, so an
assistant can ask "where is `X` defined?", "who calls `Y`?", or "which repos
depend on package `Z`?" instead of grepping hundreds of repos. It's generic: it
indexes *any* repositories and connects to *any* configured knowledge sources; no
organization-specific data lives in the package (your sites, keys, and rules go in
a private config file).

## Command reference

Each command has scoped help via `contextlake <command> --help`. The knowledge-layer
commands are:

| Command | What it does |
| --- | --- |
| `source` | add / list / remove / test / enable / disable knowledge-source connectors |
| `index` | Build the code/dependency graph (`--workspace`, incremental, `--watch`) |
| `connect` | Link repos to Atlassian / Figma / GitLab items (`--watch` to keep refreshing) |
| `enrich` | Query connected sources with codebase-derived terms and store enrichment docs (`--workspace`, incremental) |
| `embed` | Build semantic-search vectors (zero-config built-in CPU model, Ollama, or an API; incremental, `--watch`) |
| `ingest` | Aggregate external docs into the graph + semantic store (built-in `files`/`web`/`api`/`mcp` sources, or plugins) |
| `wiki` | LLM-synthesized, council-verified wiki pages (per-repo, or a cluster page with `--namespace <prefix>` / `--namespaces --depth N`); `--llm builtin|ollama|openai|anthropic|cli` enables the LLM tier inline |
| `query` | Search the index (`--kind`, `--repo`, `--as-of <commit>`) |
| `owners` | Likely owners / SMEs for a repo or path, ranked from git history (alias `who-knows`) |
| `impact` | Change-impact / blast radius: what depends on a symbol (alias `blast-radius`) |
| `graph` | Visualize the graph, offline interactive HTML / DOT / Mermaid / JSON, or a composed namespace C4 diagram with `--c4` |
| `dashboard` | Local knowledge-system dashboard UI (`--serve`; `--sample` for a bundled demo) |
| `eval` | Measure retrieval quality: precision / recall / MRR against a golden-query set |
| `lint` | Graph health audit: stale repos, dangling edges |
| `doctor` | Environment check: FTS5, git, glab, the store, embeddings, per-source reachability |

## Setup

Install the extra (requires Python ≥ 3.10):

```bash
pip install "contextlake[kb]"               # knowledge layer (parse + graph + serve)
# ...or get everything for local semantic search in one step (no Ollama / API key):
pip install "contextlake[kb-full]"          # = kb + built-in CPU embedder + sqlite-vec ANN
contextlake doctor                          # check the environment
contextlake index --source ./my-repo        # index one repository
contextlake index --workspace ~/work        # index every git repo (incremental; --force to rebuild)
contextlake connect --workspace ~/work      # link repos to their issues/docs (see below)
contextlake embed                           # build semantic vectors (optional, see below)
contextlake lint                            # graph health: stale repos + dangling edges
contextlake wiki acme/orders-api --llm builtin      # wiki for one repo; --llm enables the LLM tier inline
contextlake wiki --namespace delivery/dcs --llm builtin   # a cluster page for a whole namespace
contextlake steer                           # write per-tool steering: AGENTS.md, .mcp.json, …
contextlake query "OrderService"            # cited search across the index
contextlake graph --overview --open         # visualize the graph (HTML/dot/mermaid/classdiagram/json; offline)
contextlake serve                           # expose the graph over MCP (stdio or --transport http)
```

`contextlake doctor` verifies the whole layer in one pass — FTS5, `git`/`glab` on PATH, the
store's real counts, the built-in CPU embedder, and the ANN index — and exits non-zero if
anything is wrong, so it doubles as a CI health gate:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-doctor.png" alt="contextlake doctor output: green ticks for SQLite FTS5, git and glab on PATH, config loads, a reachable store with 4 repos / 29 nodes / 28 edges, the built-in embedder, and the sqlite-vec ANN index, ending in OK." width="820">
</p>

## The code graph

Indexing, the incremental build, and the full node-and-edge model (languages, Terraform, SQL, web
topology, and cross-repo dependencies) now have their own page: **[Index the code
graph](index-code-graph.md)**.

## One-command setup

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/pipeline-bootstrap.png" alt="The contextlake bootstrap pipeline: sync, then index, then connect, then embed, then enrich, then wiki, then steer." width="760">
</p>

Rather than running the steps by hand, `bootstrap`
chains them, mirror repos → index → connect → embed → enrich → wiki → write editor steering, 
skipping anything not enabled, so a teammate goes from nothing to a fully-wired
workspace in one step:

```bash
contextlake bootstrap --llm builtin
```

`--llm builtin` powers the wiki stage with a zero-setup CPU model, so this single
command builds the **whole** knowledge layer — graph, vectors, and wiki — for every
repo. (`--llm ollama` | `openai` | `auto` for higher-quality prose; the pre-command
form `contextlake --llm builtin bootstrap` also works.) Without any `--llm`, and with
`[llm]` disabled in `kb.toml`, the wiki stage no-ops and the rest still runs. Because
everything generated lives under one `store_dir`, setting it to a folder in your
workspace keeps the entire knowledge base in a single, easy-to-access location.

**Cluster (namespace) wiki.** Beyond per-repo pages, `contextlake wiki --namespace
delivery/dcs` writes one **cluster page** for a whole group of repos (everything under
that repo-id prefix), narrating how they fit together: which services call which over
HTTP, publish/consume which events, and share which packages, split into coupling
*within* the namespace and coupling to repos *outside* it. Use `--namespaces --depth N`
to generate one page per namespace at that prefix depth. It grounds strictly in the
cross-repo edges the graph already resolved (no new extraction) and reuses the same
review council + provenance footer as the per-repo wiki, so it stays advisory and cited;
when the graph shows no coupling it says so rather than inventing a link. Cluster pages
are served over MCP by passing a namespace to `get_wiki`, and shown per group in the
dashboard's fleet overview.

Both config files are read from their default locations (`~/.contextlake.ini` and
`~/.contextlake/kb.toml`); pass `--config` / `--kb-config` to point elsewhere. The valid
`[kb]` keys are `store_dir`, `languages`, `skip_generated`, `max_file_bytes`, and
`index_workers` (plus the `[embeddings]`, `[llm]`, `[sources]`, `[rules]` tables); an
unrecognized key or table is warned and ignored, so a typo like `store` (for `store_dir`)
is surfaced rather than silently dropping the run into the wrong place. Skip stages with
`--no-sync` / `--no-embed` / `--no-wiki` / `--no-connect` / `--no-enrich`. For an isolated
CLI, install with `pipx install "contextlake[kb]"`, or run ad-hoc with `uvx`.

### Command composition

Every stage is standalone, idempotent, and composable. Use these flows to build exactly what you need:

| Use case | Command(s) |
|---|---|
| Blank to fully enriched workspace | `contextlake init` then `contextlake bootstrap` |
| Add a connector, re-enrich the wiki | `contextlake source add jira ...` then `contextlake enrich` then `contextlake wiki` |
| Single repo, enriched | `contextlake index .` then `contextlake source add ...` then `contextlake enrich` then `contextlake wiki` then `contextlake serve` |
| Refresh enrichment only | `contextlake enrich` then `contextlake wiki --force` |
| Manage or inspect sources | `contextlake source list` or `contextlake source test <name>` or `contextlake doctor` |
| Disable a noisy source | `contextlake source disable <name>` then re-run `contextlake enrich` |

`contextlake bootstrap` runs the full pipeline (mirror, index, connect, embed, enrich, wiki, steer) end to end, so `init` plus `bootstrap` takes a blank workspace to a mirrored, indexed, embedded, connector-enriched, wiki'd, editor-wired workspace in one command (skip enrich with `--no-enrich`).

### Keep it fresh on a schedule

`bootstrap` is incremental and branch-safe, so it's
safe to run repeatedly, it re-mirrors, re-indexes only the repos whose HEAD moved,
refreshes the knowledge layer, and rewrites the steering, without touching an
in-progress working tree. Run it from cron:

```cron
*/30 * * * * contextlake bootstrap >> ~/.contextlake/refresh.log 2>&1
```

or as a systemd user timer, see [`examples/contextlake.service`](../examples/contextlake.service)
and [`examples/contextlake.timer`](../examples/contextlake.timer).

### Re-index on commit (git hook)

For continuous freshness without a schedule, install a git `post-commit` hook that
re-indexes a repo the moment you commit to it:

```bash
contextlake hook install                     # the repo in the current directory
contextlake hook install --workspace ~/src   # every git repo under a mirror
contextlake hook status  --workspace ~/src   # which repos are wired
contextlake hook uninstall                   # remove it (any pre-existing hook is kept)
```

The hook runs `contextlake index <repo>` detached (so the commit returns immediately)
and re-uses the repo's stored id, so it updates the same graph node rather than a
duplicate. Mirror-wide syncing (fetch new clones, prune) still belongs to `bootstrap`
on a schedule; the hook keeps *local edits* current between syncs.

If two contextlake processes ever target one store at once (say a `bootstrap` and a
hook-triggered `index`), the second takes an advisory single-writer lock
(`<store_dir>/.contextlake.lock`) and refuses rather than interleaving SQLite writes —
naming the process that holds it. A lock left by a crashed run is reclaimed
automatically; override (rarely correct) with `CONTEXTLAKE_ALLOW_CONCURRENT=1`.

## Reading the console output

A `bootstrap` (or a standalone `index` / `embed` / `wiki`, and the mirror-tier `clone` /
`update` / `branches`) prints progress as it goes. Most lines are self-explanatory; a few
are worth decoding.

- **The live progress bar** is one shared renderer used by every long-running command, so
  it looks the same everywhere: `[████████░░░░] 42/678 (6%) · 12:30 elapsed · ~2:58:14
  left · 3.4/min` (bar, done/total, percent, elapsed time, estimated time remaining, and
  rate in items/min). The ETA is a moving-average estimate over recent items (that's what
  the `~` marks), and it's count-based, each item counts equally rather than being weighted
  by size. When a run's total isn't known up front, the bar drops the percent/ETA and shows
  `done · elapsed · rate` instead, rather than guessing. Across every long-running command
  (including `connect`, `ingest`, and `enrich`, which don't use the shared bar), the clock
  only shows up on the bar itself (where there is one) and on section/summary lines; the
  per-item detail lines scrolling beneath don't repeat it, so they don't flicker as the
  timestamp ticks over.
- **One status vocabulary, everywhere.** Every command (mirror-tier `clone`/`update`/
  `branches`, `index`, `embed`, `wiki`, `enrich`, `ingest`, `connect`, `lint`, `sync`) marks
  each line with the same seven glyphs, so once you know the glyph you know the outcome
  without reading the rest of the line:

  | Glyph | Meaning | Color |
  | --- | --- | --- |
  | `✓` | ok | green |
  | `⚠` | warn | yellow |
  | `✗` | fail | red |
  | `⊘` | skip | dim |
  | `=` | unchanged | dim |
  | `↝` | switched | cyan |
  | `~` | dry-run | yellow |

  Multi-stage commands (`bootstrap` and `sync`) also print `▶ <Phase>` section headers
  (e.g. `▶ Mirror repositories from GitLab`, `▶ Audit repositories (health & age)`) so a
  long run reads as sections rather than one undifferentiated scroll, and every
  long-running command ends with a one-line, glyph-prefixed summary (`✓ Embed complete:
  ...`, `✓ Lint: ...`, and so on) you can skim straight to.
- **The bar renders on stderr; the per-item result lines below it (`✓`/`⚠` and the like)
  stay on stdout.** That split means `contextlake wiki >> run.log` (or any stdout redirect)
  captures clean detail lines with no bar artifacts or `\r` clutter, since the bar never
  touches stdout. When output isn't a TTY (piped, cron, a redirected stderr), the bar itself
  auto-downgrades to periodic plain summary lines instead of repainting in place. When both
  streams share one terminal (the default interactive case), the bar and the detail lines
  interleave as the run scrolls (the bar reprints below each new detail line rather than
  repainting perfectly in place); redirect stdout to a file to keep the bar as a single live
  line with the detail captured separately.
- **`✓ <repo>: X nodes, Y edges`** is the incremental indexer's per-repo detail line
  (stdout; the `index` progress bar above it lives on stderr). **`0 nodes, 0 edges`** is
  normal and not an error: that repo has no code in a supported language (config-only,
  docs-only, IaC/scripts, or empty). Only repos whose HEAD moved are re-indexed; the rest
  are reported as *unchanged*.
- **`Embed complete: 0 vector(s) written (N total in store), M already up to date`** —
  embedding is incremental too. `0 written` with a large `already up to date` count means
  nothing changed since the last run; the `N total` is the whole store, not this run.
- **`Fetching 10 files: 100% … Download complete: 0.00B`** appears once when the wiki
  (or built-in embedder) model loads. It is Hugging Face resolving the model repo's files
  (several GGUF quantizations + tokenizer/config) in your local cache — **`0.00B` means
  nothing was downloaded, everything was already cached**. It fires once per run at model
  load, not per repo.
- **`✓ <repo>: written (score 0.98)`** — a wiki page passed the review council and was
  saved. **`⚠ <repo>: rejected by council (score 0.31)`** — it did not clear the accept
  threshold; the indented `- accuracy/completeness/clarity: …` lines are the per-lens
  reasons. **`unparseable review`** means the model returned a review the council couldn't
  score (common with the tiny built-in 0.5B model); those lenses are excluded from the
  mean rather than counted as zero. A capable backend (`--llm ollama`/`anthropic`/`openai`)
  produces far fewer rejections — see [Model providers](#model-providers).
- **`contextlake serve --transport http` prints its bind URL** once it starts listening
  (`✓ MCP server on http://127.0.0.1:8765  (Ctrl-C to stop)`), so you don't have to guess
  the host/port before pointing an editor at it. `stdio` transport has no address to
  report and stays quiet on that line.
- **`graph --overview` on an empty store warns instead of reporting silent success.** It
  still writes the (empty) artifact, but now says `⚠ Wrote html (0 nodes, 0 edges) -> ...:
  the store is empty.` followed by a hint to run `contextlake index` first, instead of
  logging the same success line it would for a populated graph.
- **A single-writer lock message** naming another process means two runs targeted one
  store at once (see the git-hook note above).

Warnings from the model download itself (Hugging Face symlink/auth notices) are silenced;
the real progress still shows.

## Ownership & SMEs

`contextlake owners <repo>` (or `--path SUBDIR` for a sub-tree) answers **"who owns
this / who do I ask?"** straight from git history — no config, no index required. It
ranks contributors by a **recency-weighted** blend of commit volume and lines changed,
so someone active in that area lately outranks a long-departed prolific author:

```bash
contextlake owners acme/payments-api                 # top contributors for the whole repo
contextlake owners acme/payments-api --path src/auth  # …scoped to the auth module
```

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-owners.png" alt="contextlake owners acme/orders-api output: a recency-weighted SME ranking from git history — Ada Lovelace (2 commits, 29 lines, 94%) above Grace Hopper (1 commit, 6%)." width="820">
</p>

The same ranking is available to agents over MCP as **`who_knows(repo, path?, limit?)`**.

## Connectors and enrichment

Linking repos to their issues, docs, and designs, managing sources, and query-driven enrichment now live
on their own page: **[Connect and enrich](connect-enrich.md)**.

## Semantic search

Natural-language retrieval on top of the graph, the `embed` step, the two retrieval tools, backends, and
tuning now live on their own page: **[Semantic search](semantic-search.md)**.

## Aggregating documents (RAG)

Ingesting external documents (files, web, API, and MCP sources) into the knowledge layer now lives with
**[Connect and enrich](connect-enrich.md)**.

## Measuring retrieval quality

`contextlake eval` keeps retrieval falsifiable. Point it at a **golden-query JSON file** —
each entry pairs a query with the node ids it should return:

```json
{
  "queries": [
    {"query": "OrderService", "expected": ["demo_app_orderservice"]},
    {"query": "charge", "expected": ["charge"], "match": "name", "kind": "function"}
  ]
}
```

Then `contextlake eval --golden queries.json` reports **precision@k / recall@k / MRR** plus a
**cost** dimension — estimated tokens per query, and precision per 1k tokens — so "route to the
cheapest sufficient source" becomes a number, not a vibe. Score any retriever with
`--retriever fts|semantic|hybrid` (semantic/hybrid need embeddings built); a change like
embed-bodies or a reranker is then judged by whether the numbers move.

## Curated wiki

Turning the graph into grounded, council-verified prose per repo now has its own page:
**[Generate the wiki](generate-wiki.md)**.

## Model providers

Both the embeddings and wiki tiers are pluggable and take a
`provider`, defaulting to **`"auto"`**:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/provider-resolution.png" alt="How provider=auto resolves: if a local Ollama is reachable, use it; else if the built-in extra is installed, use the built-in CPU model; else skip the tier." width="720">
</p>

- **`auto`** (default), resolves to a reachable local **Ollama**, else the
  **built-in** CPU model if its extra is installed, else it skips that tier. So the
  semantic/wiki tiers Just Work the moment you set `enabled = true`, with no daemon
  and no API key.
- **`builtin`**, a small model that runs **in-process on CPU**, auto-downloaded once to
  `cache_dir` (default `~/.contextlake/models`). Zero daemon, zero API key.
  - *Embeddings*, `engine = "model2vec"` (default): static `potion-base-8M`
    (~30MB, MIT), numpy inference, very fast at scale, `pip install
    "contextlake[kb-local]"`. Or `engine = "fastembed"`: ONNX `bge-small` (~90MB,
    MIT, higher quality), `pip install "contextlake[kb-fastembed]"`.
  - *Wiki LLM*, a `Qwen2.5-0.5B-Instruct` GGUF (Apache-2.0) via `llama-cpp-python`,
    `pip install "contextlake[llm-local]"`. Fast to set up, but **0.5B is a modest
    writer** — good for coverage, not polished prose — and CPU generation is **slow**
    (~4 calls/repo). Prefer Ollama at any real scale. See
    [Why the built-in LLM needs a prebuilt wheel](#why-the-built-in-llm-needs-a-prebuilt-wheel-or-a-compiler)
    and [How much does the model matter?](#how-much-does-the-model-matter).
- **`ollama`**, a local [Ollama](https://ollama.com) daemon (`base_url`, default
  `http://127.0.0.1:11434`) — the **recommended** wiki backend: no Python native build,
  and a 3B–8B model writes markedly better pages than the built-in 0.5B. See
  [Using Ollama for the wiki](#using-ollama-for-the-wiki).
- **`openai`**, **any OpenAI-compatible chat API** (a hosted key, or a local server like
  LM Studio, Jan, llama.cpp, vLLM). Best prose, per-token cost. The key is read from the
  env var named by `api_key_env` (default `OPENAI_API_KEY`), never stored in config.
- **`anthropic`**, the Anthropic **Messages API** (a hosted key). Best-in-class wiki
  prose and reliable structured council reviews. The key is read from the env var named
  by `api_key_env` (default `ANTHROPIC_API_KEY`), never stored in config. `model` selects
  the tier: default `claude-opus-4-8`; set `model = "claude-haiku-4-5"` or
  `"claude-sonnet-5"` for a much cheaper high-volume fleet run (the council makes many
  calls). `max_tokens` (default 4096) caps each response.
- **`cli`**, a locally-installed **agent CLI** you already pay for: `claude`, `gemini`,
  or `codex`. contextlake shells out to it (`command`, default `claude`; `args` overrides
  the per-CLI preset) and feeds the prompt on stdin. No API key touches contextlake;
  data goes to whatever provider that CLI uses. Reuses your subscription, offline-adjacent
  (still a network call by that tool), and mirrors how contextlake already shells out to
  `git` and `glab`.

**Data-sharing posture per backend.** Pick by what may leave your machine:

| Backend | Data leaves the machine? | Auth |
|---|---|---|
| `builtin` / `auto`→builtin | No: fully local CPU model | none |
| `ollama` | No: local daemon | none |
| `cli` | Yes: to whatever provider that CLI uses | reuses the CLI's own login |
| `anthropic` / `openai` | Yes: to the API endpoint | env-var key (never stored) |

### Configuring the wiki LLM

Two lines is enough — passing `--llm` on the CLI implies `enabled = true`, or set both in
`~/.contextlake/kb.toml`:

```toml
[llm]
enabled  = true
provider = "ollama"        # auto | builtin | ollama | openai | anthropic | cli
model    = "qwen2.5:3b"    # provider-specific model id (table below)
# base_url    = "http://127.0.0.1:11434"   # ollama, or a local openai-compatible server
# api_key_env = "OPENAI_API_KEY"           # openai: env var holding the key (never the key)
# timeout    = 300          # seconds per model call; raise it for a slow CPU (ollama/openai)
council_size = 3           # review lenses that run (1-3); fewer = fewer calls per page
accept_score = 0.7         # mean council score a page must clear to be written
```

| provider  | example `model`                          | notes |
|-----------|------------------------------------------|-------|
| `builtin` | `Qwen/Qwen2.5-0.5B-Instruct-GGUF`        | a HF GGUF repo id; `model_file` picks the quant |
| `ollama`  | `qwen2.5:3b`, `llama3.1`, `llama3.2:3b`  | must be `ollama pull`ed first |
| `openai`  | `gpt-4o-mini`, or your server's model id | `base_url` = the API's `/v1` |

CLI flags override the toml and now work on **`bootstrap`** too:
`contextlake bootstrap --llm ollama --llm-model qwen2.5:3b`.

### Why the built-in LLM needs a prebuilt wheel (or a compiler)

The `builtin` wiki model runs a **GGUF** model through
[`llama-cpp-python`](https://github.com/abetlen/llama-cpp-python) — Python bindings around
`llama.cpp`, a **C++** inference engine. Native (C/C++) packages ship as **prebuilt binary
wheels**, one per (OS, CPU, Python version). Two consequences explain why an extra step is
sometimes needed and why contextlake can't do it for you:

1. **A dependency can't carry an index URL.** `contextlake[llm-local]` can only *name*
   `llama-cpp-python`; Python packaging (PEP 508) deliberately forbids pinning an
   `--extra-index-url` in a package's metadata (for reproducibility and supply-chain
   safety). So contextlake cannot make `pip` look anywhere but your configured indexes
   (PyPI by default) — only *your* `pip` command can add one.
2. **PyPI lags brand-new Pythons.** Wheels are uploaded per interpreter version; a
   just-released Python (e.g. **3.14**) often has **no wheel on PyPI yet**, so `pip` falls
   back to the source tarball and tries to **compile** — which needs `cmake` + a C/C++
   compiler you may not have installed. That is the build failure you saw.

The maintainer also publishes a **prebuilt CPU wheel index** carrying wheels PyPI doesn't
have yet, so pointing pip at it skips compilation entirely — no compiler needed:

```bash
pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

**Belt-and-suspenders: `--only-binary :all:`.** On a compiler-less machine, add this flag
so pip *refuses* to fall back to a source build for **any** package — you get a clean "no
matching distribution" message instead of a wall of `cmake`/compiler errors. `:all:` is the
all-packages token (its opposite is `--no-binary`). Combined with the CPU-wheel index, this
installs the built-in LLM on a brand-new Python with no toolchain:

```bash
pip install --only-binary :all: llama-cpp-python \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

The trade-off is deliberate: if a wheel genuinely doesn't exist for your platform, the
command stops with an actionable error rather than attempting a build that can't succeed.

On a mainstream Python (3.10–3.13) none of this applies — `pip install
"contextlake[llm-local]"` finds a PyPI wheel and Just Works. It is specifically the
bleeding-edge-interpreter case that needs the extra index. The cleanest way to avoid the
native build altogether is to **use Ollama** (below), a standalone binary with no Python
compile step.

### Using Ollama for the wiki

[Ollama](https://ollama.com) is a standalone local model server. It sidesteps the native
Python build, and a 3B–8B model writes much better wiki pages than the 0.5B built-in.

**A) Ollama inside WSL / Linux (simplest — `localhost` just works):**

```bash
curl -fsSL https://ollama.com/install.sh | sh   # installs + starts the daemon
ollama pull qwen2.5:3b                            # ~1.9GB, one-time
contextlake bootstrap --llm ollama --llm-model qwen2.5:3b   # whole layer in one command
# or per repo:  contextlake wiki <repo> --llm ollama --llm-model qwen2.5:3b
```

contextlake defaults `base_url` to `http://127.0.0.1:11434`, so usually nothing else to set.

**B) Ollama on Windows, contextlake in WSL (the cross-boundary case).** WSL2 is a separate
network namespace, so a Windows Ollama bound to `127.0.0.1` is **not reachable from WSL**
(`localhost:11434` → connection refused). Two fixes:

- *Easiest — mirrored networking.* In `%UserProfile%\.wslconfig` add:
  ```ini
  [wsl2]
  networkingMode=mirrored
  ```
  then `wsl --shutdown` and reopen. Now `localhost` is shared, so the default
  `base_url = "http://127.0.0.1:11434"` works from WSL unchanged.
- *Or expose Ollama and use the host IP.* On Windows set `OLLAMA_HOST=0.0.0.0` (System
  Environment Variables) and restart Ollama so it listens on all interfaces; allow it
  through the Windows firewall. From WSL, the Windows host is your **default-route
  gateway** — *not* the `nameserver` in `/etc/resolv.conf` (that is a DNS stub):
  ```bash
  ip route show default | awk '{print $3}'   # e.g. 172.24.224.1  (NOT 10.255.255.254)
  curl http://172.24.224.1:11434/api/tags    # confirm reachability
  ```
  Set `base_url = "http://172.24.224.1:11434"` in `[llm]` (your IP will differ).

Pull the model on whichever side runs Ollama: `ollama pull qwen2.5:3b`.

### How much does the model matter?

The wiki's quality is bounded by the model behind it. The graph facts fed in are identical;
the difference is how well the model turns them into prose (and the verification council
rejects weak pages regardless, so a smaller model mostly means *more rejections* and
blander accepted pages).

A measured A/B on one repo (`contextlake`, 1810 graph nodes, identical facts + 3-lens
council) on a **CPU-only** host (no GPU — e.g. WSL2 without GPU passthrough):

| model | where | speed | result |
|-------|-------|-------|--------|
| built-in `Qwen2.5-0.5B` (GGUF) | in-process CPU | fast enough | page written (~119 words) — accurate but **thin and generic** |
| Ollama `qwen2.5:1.5b` | CPU (no GPU) | ~1.7 tok/s | **timed out** — a full page + reviews needs ~10 min/repo |
| Ollama `qwen2.5:3b` | CPU (no GPU) | ~0.85 tok/s | **timed out** — ~20 min/repo |

The lesson is about **hardware, not model quality**: a 1.5B–3B model writes better prose
than the 0.5B, but on a CPU-only box it is too slow to finish a page in reasonable time
(and, at fleet scale, wholly impractical). Those models shine when Ollama has a **GPU** —
e.g. Ollama on a Windows host with a discrete GPU generates in *seconds*, not minutes. So:

- **CPU-only, offline, quick:** the **built-in 0.5B** — fast to set up, basic prose. Or a
  **hosted API** if quality matters and you accept per-token cost.
- **GPU available (incl. Ollama on your Windows host):** **Ollama 3B–8B** — the sweet spot
  for readable pages at fleet scale.
- **Best prose regardless of local hardware:** an **API model** (`openai` provider).

If your local model is slow, raise the per-call timeout instead of letting every page fail
silently — `timeout` in `[llm]` (seconds, default 300):

```toml
[llm]
provider = "ollama"
model    = "qwen2.5:3b"
timeout  = 1200        # give a slow CPU room; default is 300s (5 min)
```

Notes: behind a TLS-inspecting corporate proxy the first built-in download needs
your OS CA bundle (`export REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE`; see
`docs/releasing.md`). Don't switch the embedder model/dimension against an existing
vector store without re-embedding from scratch, a guard refuses the mismatch. The
prebuilt Docker image (`ghcr.io/sayak-sarkar/contextlake`) bundles these models so
nothing downloads at runtime. See `examples/kb.toml.example`.

## Visualizing the graph

`contextlake graph` draws a **bounded** slice of the graph, the whole thing
(hundreds of thousands of nodes) is far too large to render, so every view is
scoped from a seed and capped:

```bash
contextlake graph --overview --open                 # repos-as-nodes: the architecture map
contextlake graph --name OrderService --kind class  # a symbol's neighbourhood (default 2 hops)
contextlake graph --node <id> --hops 3              # expand around an exact node id
contextlake graph --search "payment" --open         # seed from a full-text search
contextlake graph --repo acme/orders-api           # one repo's internal code graph
```

`contextlake graph --repo <repo>` renders one repo's internal code graph to a single
self-contained HTML page — nodes coloured by kind and sized by degree, edges by relation,
with an in-page layout switcher, search, and a minimap; it opens straight from `file://`:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/graph-repo.png" alt="The offline HTML code graph for acme/orders-api: file, class, and method nodes (OrderService, PaymentClient, place_order, charge, refund) coloured by kind and linked by calls/contains edges, with a legend, layout switcher, and corner minimap." width="820">
</p>

Seed with one of `--node` / `--name` (+`--kind`) / `--search` / `--repo` /
`--overview`. Bound the result with `--hops` (default 2), `--max-nodes` (500),
`--max-fanout` (50, a per-node cap that stops hub nodes from exploding),
`--relation`, and `--direction {in,out,both}`, whatever is dropped is **logged**,
never silently truncated.

Output is chosen with `--format`:

- **`html`** (default), a single **self-contained, offline** page (cytoscape.js is
  inlined, so it opens from `file://` with no network, handy air-gapped / behind a
  proxy). Nodes are coloured by kind and sized by degree; edges are styled by
  relation/confidence with their labels hidden until you click a node (so the view
  stays readable). Pan, zoom, drag, and a **layout switcher** (`cose`, `concentric`,
  `breadthfirst`, `circle`, `grid`) in the page, set the initial one with `--layout`.
  `--open` launches the browser; `--cdn` produces a small online-only file instead.
- **`dot`**, Graphviz (`contextlake graph … --format dot | dot -Tsvg > g.svg`).
- **`mermaid`**, the relation graph, pastes into Markdown / GitHub.
- **`classdiagram`**, a **Mermaid UML class diagram** for a repo (or a seeded slice):
  classes / interfaces / structs with their methods as members, and `inherits` edges
  as inheritance arrows (`<|--` extends, `<|..` implements). Great for a PR or design
  doc: `contextlake graph --repo acme/app --format classdiagram`.
- **`json`**, the raw `{nodes, edges, meta}` for Gephi / cytoscape / custom tooling.

For interactive exploration of a large graph, `contextlake graph --serve` runs a
local web UI where clicking a node **expands** it (fetches its neighbours on
demand) so you can walk the graph without pre-rendering all of it.

### Composed namespace C4 diagram

`contextlake graph --c4` renders a different kind of view: a composed **C4-Context/
Container** diagram over the whole fleet, namespaces are the boundaries, repos are the
containers inside them, and the aggregated `depends_on`, HTTP `flow`, and event `flow`
edges become the labeled inter-service connections (grouped by flavor and weight, e.g.
`http x3`). It renders graph data that `index`/`connect` already extracted, so it runs
fully offline and adds no new extraction pass. `--group-depth N` (default `1`) controls
how deep into the namespace path the boundaries are drawn, and `--repos <glob>` scopes
the diagram to matching repos. Because it only draws coupling the graph already
resolved (weight-ranked), it doesn't invent links, and folding event-flow in alongside
HTTP keeps it from telling an HTTP-only half story:

```bash
contextlake graph --c4 --group-depth 2 --open       # HTML, open in the browser
contextlake graph --c4 --format dot > c4.dot        # clustered DOT, copy-pasteable
```

Output is chosen with `--format`: `html` (default, an interactive page with namespace
boundaries as compound nodes, written to `<store>/graphs/c4.html`), `dot` (Graphviz
clustered DOT with `subgraph cluster_*` boundaries), or `json` (the raw payload).
`--format mermaid` and `--format classdiagram` aren't supported with `--c4` (the
command exits with an error), and `--serve` doesn't apply either, the C4 view is a
generated file, not a live server.

## The dashboard

Where `graph` shows one graph, **`contextlake dashboard`** is the human UI into the whole
knowledge system — a local, offline-first, read-only app over your store:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/fleet-cards.png" alt="The contextlake dashboard fleet overview: stat cards, a knowledge-confidence bar, and repos grouped by namespace, with a Cards/List/Table layout switcher." width="820">
</p>

```bash
contextlake dashboard --serve            # live, against your store
contextlake dashboard --serve --sample   # a generic demo fleet — no data needed
contextlake dashboard --site ./out       # a static, offline (file://) export
```

It surfaces per-repo anatomy, README/wiki, owners, and connector links; repo→repo dependency
and flow (with confidence + provenance); an interactive architecture graph; change-impact;
health; and search — every fact with its receipt.

**[The dashboard — a guided tour](dashboard.md)** walks all of it step by step, with
screenshots, plus the sharing/privacy guidance.

## Serve it to your editor (MCP)

Once the graph is built, **`contextlake serve`** exposes it over MCP so agents query it
directly instead of grepping — and `contextlake steer` wires your editors for you in one
command.

**[Serve it to your editor](serve.md)** covers the full tool list, the one-command steering
setup, and manual wiring for Claude Code / Windsurf / Kiro.
