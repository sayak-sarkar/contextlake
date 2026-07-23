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

The pluggable embeddings and wiki backends (auto / builtin / ollama / openai / anthropic / cli), configuring the LLM, the prebuilt-wheel explainer, using Ollama, and the model bake-off now have their own page: **[Model providers](model-providers.md)**.

## Visualizing the graph

Drawing bounded graph slices (`contextlake graph`), the output formats, and the composed namespace C4 diagram now have their own page: **[Visualize the graph](visualize.md)**.

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
