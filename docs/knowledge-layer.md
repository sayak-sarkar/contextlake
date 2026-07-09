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
| `wiki` | LLM-synthesized, council-verified wiki pages; `--llm builtin|ollama|openai|anthropic|cli` enables the LLM tier inline |
| `query` | Search the index (`--kind`, `--repo`, `--as-of <commit>`) |
| `owners` | Likely owners / SMEs for a repo or path, ranked from git history (alias `who-knows`) |
| `impact` | Change-impact / blast radius: what depends on a symbol (alias `blast-radius`) |
| `graph` | Visualize the graph, offline interactive HTML / DOT / Mermaid / JSON |
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

## Indexing

`contextlake index --workspace ~/work` walks every git repo under a folder and builds the
graph. Runs are incremental by default; `--force` rebuilds from scratch.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-index.png" alt="contextlake index --workspace output: per-repo progress bars across four acme repos, each with node and edge counts, ending in a summary of 4 repos, 29 nodes, 28 edges." width="820">
</p>

### Incremental & time-travel

`index --workspace` is **incremental**, it re-indexes only repos whose git HEAD
moved since their last index, so a scheduled (cron) run stays cheap; pass `--force`
to rebuild everything, or `--watch [--interval N]` to keep re-indexing in a loop
(the same `--watch`/`--interval` flags also drive `connect` and `embed`).
Every indexed snapshot is kept, so `query "<text>" --repo R --as-of <commit>` does
**time-travel**, it searches repo `R` as it was at a previously-indexed commit.

### Parallelism & noise-pruning

Repositories are parsed across **worker processes** (CPU-bound work) while the SQLite store
is written serially from the parent. The `spawn` start method is used on every platform, so
behaviour is identical on Linux, macOS, and Windows, with an automatic serial fallback if a
worker pool can't start. It defaults to `cpu_count - 1` workers (capped at 8); set
`[kb] index_workers` to tune it (`1` forces serial).

The parser also **skips machine-generated and derived files** — `*.designer.cs`, `*.min.js`,
`AssemblyInfo.cs`, and `@generated` / `<auto-generated>` headers — plus code files larger than
`[kb] max_file_bytes` (5 MB). That's derived noise, not real source, and every skip is reported
(no silent gaps). Set `[kb] skip_generated = false` or raise `max_file_bytes` to index them anyway.

To exclude your own paths, drop a **`.contextlakeignore`** at a repo's root: one
glob per line (`#` comments and blank lines ignored), matched against each file's
path relative to the repo and its name, so `*.lock` ignores by name anywhere and
`vendor/` ignores a directory and everything under it. It's a small, dependency-free
subset of gitignore syntax (no negation, `**`, or anchoring), enough to drop
vendored trees and lockfiles from the graph.

### Health & maintenance

`contextlake doctor` checks the environment (shown under [Setup](#setup) above).
`contextlake lint` audits the graph itself, reporting **stale repos** (HEAD moved since they
were indexed) and **dangling edges** (an edge whose endpoint node is missing). Both exit
non-zero on problems, so they're CI-friendly.

## Code indexing

Code indexing uses tree-sitter to extract files, classes, functions/methods,
interfaces, imports, an intra-repo **call graph**, and an **inheritance graph**
(`inherits` edges for `extends` / `implements` / base classes) from **Python,
JavaScript, TypeScript/TSX, C#, Go, Java, C, C++, Rust, Ruby, PHP, Scala, and Kotlin**
(the parser registry is pluggable) — so "what extends `BaseController`?" is one hop,
and changing a base class shows its subclasses in `blast_radius`. **Terraform/HCL**
(`.tf`) is indexed into an infrastructure dependency graph: `resource`/`data`/
`variable`/`output`/`module`/`local` definitions with `depends_on` edges resolving
`var.`/`module.`/`data.`/resource references across files in a repo; `resource`
nodes are semantically searchable. Resolution is repo-wide, so a block address
defined identically in separate root-module directories (for example
`environments/prod` and `environments/staging`) surfaces as an `AMBIGUOUS`
edge; directory-scoped resolution is a future refinement. **SQL DDL** (`.sql`) is
indexed into a referential graph: `table`/`view`/`procedure` definitions with
`references` edges from foreign-key `REFERENCES` clauses, resolved across files
in a repo; `table` and `view` nodes are semantically searchable. It uses a regex
DDL extractor (the fleet's T-SQL/PL-SQL defeats a tree-sitter AST), so it targets
the high-value defs and FK references and is a deliberate undercount. Frameworks
are indexed through their
base language: **React / Next.js / Node.js** are JS/TS(X), **Angular** is TS (its
templates are HTML), and **.NET** is C#. It also reads manifests (`pyproject.toml`,
`package.json`, `*.csproj`, `pom.xml`) to build a **cross-repo dependency graph** through shared
package nodes. Agents traverse all of this over MCP,
from finding a definition to cross-repo `blast_radius` ("what could break if I change
this"), see [the full tool list under Serve](serve.md). The same
change-impact walk is a one-liner from the shell: `contextlake impact <symbol> [--hops N]`
lists what calls / depends on a node, no editor needed. When a symbol name (e.g. `Node`,
`Order`) is defined in more than one repo, `impact` lists the candidates and you narrow it
with `--repo <repo>` rather than getting a silent best-guess.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-impact.png" alt="contextlake impact charge output: changing charge in acme/orders-api affects place_order at hop 1 via a calls edge, tagged inferred — hop distance, relation, and confidence for each affected node." width="820">
</p>

## One-command setup

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/pipeline-bootstrap.png" alt="The contextlake bootstrap pipeline: sync, then index, then connect, then embed, then wiki, then steer." width="760">
</p>

Rather than running the steps by hand, `bootstrap`
chains them, mirror repos → index → connect → embed → wiki → write editor steering, 
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

Both config files are read from their default locations (`~/.contextlake.ini` and
`~/.contextlake/kb.toml`); pass `--config` / `--kb-config` to point elsewhere. Skip
stages with `--no-sync` / `--no-embed` / `--no-wiki` / `--no-connect`. For an
isolated CLI, install with `pipx install "contextlake[kb]"`, or run ad-hoc with `uvx`.

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

A `bootstrap` (or a standalone `index` / `embed` / `wiki`) prints progress as it goes.
Most lines are self-explanatory; a few are worth decoding.

- **`▶ <Phase>` headers** (`▶ Mirror repositories`, `▶ Index the code graph`,
  `▶ Build semantic vectors`, `▶ Generate the curated wiki`, …) mark each pipeline stage.
  A stage that has nothing to do (no connector sources, no LLM enabled) says so and moves on.
- **`[███░░] N/M <repo>: X nodes, Y edges`** is the incremental indexer. **`0 nodes, 0
  edges`** is normal and not an error — that repo has no code in a supported language
  (config-only, docs-only, IaC/scripts, or empty). Only repos whose HEAD moved are
  re-indexed; the rest are reported as *unchanged*.
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

## Connectors

`connect` enriches the graph with external context. Three connectors ship, sharing one seam:

- **Atlassian** — links each repo to the Jira issues and Confluence pages it references. Issue
  keys harvested from branch/commit names are confirmed against the live tracker (one batched
  JQL call per site prunes false positives and fetches each issue's summary/status), and
  Atlassian URLs in docs are classified into issue/page links. It talks to one or more
  Atlassian sites over MCP, each independently authenticated.
- **Figma** — links repos to the design files they reference, classifying `figma.com` URLs to a
  stable file key.
- **GitLab** — links each repo to its open **merge requests and issues** (read through your
  authenticated `glab`).

Adding another connector is a small, self-contained module, and its output lands in an isolated
graph partition — so re-indexing a repo's code never disturbs its external links. Configure
connectors by copying [`examples/kb.toml.example`](../examples/kb.toml.example) to
`~/.contextlake/kb.toml`.

### Managing sources: the `source` command family

Editing `kb.toml` by hand works, but for everyday use `contextlake source` commands let you add,
test, and manage connectors without touching the config file. They rewrite `kb.toml` while
preserving your comments, and work alongside hand-editing if you mix approaches.

**The commands:**

- **`contextlake source add [--name NAME]`**: guided prompt to add a new connector. Asks for the
  connector type (Atlassian / Figma / GitLab), provides sane defaults, and writes the entry to
  `kb.toml`. Pass `--type`, `--name`, and other flags to bypass the prompt (`--help` shows all).
- **`contextlake source list`**: show all configured connectors (the effective merged config from
  `~/.contextlake/kb.toml`, `.contextlake/kb.toml` if present, and the built-in defaults), with
  reachability status.
- **`contextlake source test SOURCE`**: verify that a specific connector works. Reaches its API,
  reads credentials from the configured env var, lists available items. Shows you exactly what
  each source will ingest without running a full `connect`.
- **`contextlake source enable|disable SOURCE`**: toggle a connector on/off in the config by
  name, so you can pause one without deleting it.
- **`contextlake source remove SOURCE`**: delete a connector entry by name.

**Example workflow:**

```bash
contextlake source add                # interactive: what type? which workspace?
contextlake source list               # show what you've configured + status
contextlake source test my-atlassian  # does it work? what's in scope?
contextlake connect                   # now link repos to their items
```

`init` can also prompt you to connect a source during first-run setup, and `doctor` reports
per-source reachability as part of its environment check, so hand-editing is optional; the CLI
guides you through the whole flow.

**Every fact carries its receipt.** Each is provenance-stamped (source file + verified date)
and confidence-tagged as one of three tiers — **`EXTRACTED`** (read straight from source/AST),
**`INFERRED`** (a resolved call or link), or **`AMBIGUOUS`** (an unconfirmed candidate) — and
sanitized before it reaches an agent. The dashboard and the graph legend use these same tiers.

## Query-driven enrichment

`contextlake enrich` performs **query-driven enrichment**: it derives search terms from each repo's
code graph (the repo's name and its top symbols by graph degree) and queries your connected sources
(Atlassian Rovo search, or any `mcp` source with a `tool` and `arg_template` configured) with those
terms, then stores the returned documents in a searchable, embedded `@enrich:<repo>` partition,
idempotent and re-runnable across the whole fleet or a single repo:

```bash
contextlake enrich --workspace ~/work     # all indexed repos
contextlake enrich acme/orders-api         # one repo
```

Prerequisites: the code graph must be **indexed first** (`contextlake index`), and at least one
term-searchable source must be configured: either an `mcp` source with `tool` and `arg_template`
keys, or an `atlassian` source. Sources without these capabilities (e.g. a plain `files` or `web`
source) are skipped gracefully. Each repo's enrichment documents are stored in their own partition
so they can be re-fetched without clobbering prior results, and are embedded (when the semantic
tier is enabled) so they surface in semantic search results as `document` nodes tagged with their source (`attrs.source`). After `contextlake wiki` runs, enrichment docs are incorporated into the curated wiki as an attributed "External context" section, grounded to the code graph's terms.

## Semantic search

Semantic search (optional) adds natural-language retrieval on top of the graph.
Enable `[embeddings]` in the config (local-first, vectors come from an Ollama model
by default, so code never leaves the machine), run `contextlake embed` to vectorize
the indexed nodes into a local store, and `serve` then exposes two tools:
`semantic_search` for queries where the exact symbol name is unknown, and
`hybrid_search`, which seeds Personalized PageRank with the embedding hits and
propagates relevance across the graph (HippoRAG-style) to surface structurally
related nodes, a function's callers, a package's dependents, that a pure semantic
match would miss. The vector store uses an exact pure-Python cosine scan by default;
install the optional ANN backend with `pip install "contextlake[kb-vec]"` (sqlite-vec)
for larger workspaces. Tune it with `[embeddings] vector_chunk_size` (the sqlite-vec
`vec0` KNN chunk size, default 1024; clamped to a multiple of 8, applied when the vector
store is first created — re-embed from scratch to change an existing store).
`[embeddings] vector_backend` (default `auto`) picks `sqlite-vec` when that extra is
installed and falls back to the pure-Python `brute` scan otherwise; force one with
`vector_backend = "sqlite-vec"` or `"brute"`. `[embeddings] batch_size` (default `64`) sets
how many nodes are embedded per batch.

**What gets embedded:** the code **definitions** (classes, functions, methods,
interfaces, structs, enums) and HTTP endpoints — each with its name, qualified name,
file path, and captured **signature and docstring** — so a natural-language query like
*"refund a payment to the original card"* finds the right function even when its name
says nothing of the sort. (Measured on the golden-query harness, adding signature +
docstring doubled MRR and took hit-rate to 100% on natural-language queries.) File,
module, and package nodes are deliberately not embedded: a path or a shared package
name is low semantic signal, and skipping them keeps results clean and avoids
re-embedding cross-repo shared nodes once per referencing repo.

**Which model?** With that content embedded, the tiny static models punch far above
their weight: on a 24-query natural-language bake-off, the default `potion-base-8M`
(~30MB, ~1ms per query) outscored the ONNX `bge-small` transformer, and
`minishlab/potion-base-32M` (~120MB, same engine and extra) scored best of all —
MRR 0.95 with a perfect hit-rate, at a tenth of the ONNX query latency. If you want
the quality bump, it's one config line: `model = "minishlab/potion-base-32M"` under
`[embeddings]` (on a fresh vector store — the identity guard refuses to mix models).

Like `index`, `embed` is **incremental**: it re-embeds only repos whose indexed HEAD
moved since they were last embedded, so a scheduled refresh over a large fleet stays
cheap. Pass `--force` to re-embed everything. When an upgrade changes the embedded
text format itself, `embed` detects the stale store and re-embeds everything once,
announcing why — then incremental behavior resumes.

A single query returns cited hits (`repo · file:line · kind · name`) that span repos *and*
languages — here the C# and Python payment paths together. `--retriever fts|semantic|hybrid`
picks keyword, vector, or graph-propagation ranking:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-query.png" alt="contextlake query payment --retriever hybrid output: ten cited hits spanning acme/orders-api (Python PaymentClient, charge, refund) and acme/payments-api (C# PaymentProcessor, Charge, Refund, CardGateway), each with repo, file:line, kind, and name." width="820">
</p>

## Aggregating documents (RAG)

Not everything lives in code. `contextlake ingest` pulls **external documents** into the
same knowledge layer — they become `kind="document"` graph nodes and, when embeddings are
on, their bodies are embedded so semantic search spans code *and* docs together:

```bash
contextlake ingest --path ./docs        # zero-config: ingest a folder of files
```

Sources follow a tiny seam, so common ones are **built-in and config-only** while anything
heavier is a **loosely-coupled plugin** — bake in the common, plugin the rest:

```toml
# kb.toml — built-in "files" source (no code, no extra install)
[[sources]]
type = "files"
name = "handbook"
path = "~/notes"
include = ["*.md", "*.txt"]
```

**Writing a plugin** is just a class with `iter_documents()` and one entry point — no fork,
no core dependency:

```toml
# in your plugin package's pyproject.toml
[project.entry-points."contextlake.sources"]
confluence = "my_pkg.sources:ConfluenceSource"
```

```python
from contextlake.kb.sources import Document          # the whole contract

class ConfluenceSource:
    def __init__(self, space=None, **_): self.space = space
    def iter_documents(self):
        yield Document(id="123", title="Runbook", text="…", uri="https://…")
```

`contextlake ingest` then discovers `type = "confluence"` automatically. Four sources ship
built-in: `files`, `web`, `api`, and `mcp`. **`web`** fetches URLs and ingests their
readable text (stdlib-only):

```toml
[[sources]]
type = "web"
name = "changelog"
urls = ["https://example.com/changelog", "https://example.com/roadmap"]
```

An **`api`** source ships built-in too — GET a JSON endpoint and map its records to
documents, with any bearer token read from an env var (never the config file):

```toml
[[sources]]
type = "api"
name = "tickets"
url = "https://api.example.com/v1/articles"
items = "data.articles"        # dotted path to the record list
text_field = "body"            # which key holds the document text
token_env = "EXAMPLE_API_TOKEN"  # bearer token comes from this env var
```

An **`mcp`** source ships built-in as well — contextlake connects as an MCP *client*
(stdio or streamable-HTTP) to another MCP server, lists its resources, and ingests each:

```toml
[[sources]]
type = "mcp"
name = "team-kb"
command = "uvx"                 # stdio transport: a server to launch…
args = ["some-mcp-server"]
# …or an HTTP endpoint instead:
# url = "https://mcp.example.com/sse"
```

So contextlake both *serves* a knowledge graph over MCP and *consumes* other MCP servers'
resources into it — the loop closes on the same seam.

An `mcp` source may also declare a search *tool* (not just read its resources) and
template codebase-derived terms into the tool's arguments. This groundwork enables
query-driven wiki enrichment in the `enrich` stage. Declare the tool name and an
argument template with substitution placeholders:

```toml
[[sources]]
type = "mcp"
name = "team-search"
command = "uvx"
args = ["some-mcp-server"]
# Optional: call a search tool on the server, templating repo/symbol terms
tool = "search"                 # the tool name on the server
arg_template = { query = "{terms}" }  # {terms} substituted with codebase-derived terms
```

Both transports work with tool calling: `command` and `args` for stdio, or `url` for
streamable-HTTP. The tool is called with the templated arguments during enrichment,
returning documents grounded to the codebase's query context.

**Additional `[[sources]]` keys.** Beyond the per-type keys above, connector and ingest
sources also accept: `auth_dir` — an isolated OAuth-cache directory; set a distinct one per
Atlassian org so their `mcp-remote` caches never collide. `mcp_command` — a local stdio MCP
command to launch instead of a remote endpoint (e.g. `"figma-mcp --stdio"`). `group` — a
GitLab group prefixed to each repo's path to form the project id. `per_page` — API page size
(default `50`).

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

The wiki (optional, local-first) turns the graph into prose. Enable
`[llm]` in the config (generation runs on a local Ollama model by default, prompts
never leave the machine) — or skip the toml entirely and pass `--llm <provider>`
(`builtin` | `ollama` | `openai` | `anthropic` | `cli`), e.g. `contextlake wiki acme/orders-api --llm builtin`,
which enables the tier inline and scopes generation to the named repo(s). Run
`contextlake wiki`: for each repo it synthesizes a
Markdown page grounded strictly in graph facts (top symbols, dependencies, files)
with a provenance footer citing the commit and sources, then puts the draft through
a **verification council**, reviewers score it for accuracy, completeness, and
clarity and a chairman publishes only pages above a configurable threshold. Nothing
that fails review is written.

Accepted pages also become **searchable prose**: each page's sections are stored in
an isolated `@wiki:<repo>` partition and, when the semantic tier is enabled, embedded
alongside the code vectors — so a natural-language question can land on the wiki's
explanation of a subsystem, cited back to the page file and labeled advisory (kind
`wiki`), never outranking extracted code facts. Pages written before this existed are
backfilled on the next `wiki` run without any LLM calls.

**Incorporating connector enrichment.** When `contextlake enrich` has populated a repo's
`@enrich:<repo>` enrichment documents (via Atlassian, Figma, GitLab, or MCP sources), the
wiki synthesizer draws on them and incorporates an "External context" section into each repo's
curated page. Each external fact is directly quoted from its source (Confluence page, Jira issue,
or MCP search result) and attributed by source URL or name, never presented as a free assertion
or as an undisclosed code fact. The council still gates the enriched page before it is written,
ensuring external context supplements rather than displaces code-backed facts and that attribution
is clear and verifiable.

The result, rendered in the dashboard's Wiki tab — prose grounded strictly in real symbols,
with a provenance footer citing the exact commit and source files it was built from:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/wiki-rendered.png" alt="The curated wiki for acme/orders-api rendered in the dashboard Wiki tab: an advisory banner, then Overview, Key components (OrderService, PaymentClient), How a request flows, and Notes, grounded in the repo's real symbols with a provenance footer." width="820">
</p>

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
