# Knowledge layer

An optional subsystem (`contextlake.kb`) turns your mirrored repositories into a
queryable **knowledge graph** and serves it to AI agents over **MCP**, so an
assistant can ask "where is `X` defined?", "who calls `Y`?", or "which repos
depend on package `Z`?" instead of grepping hundreds of repos. It's generic: it
indexes *any* repositories and connects to *any* configured knowledge sources; no
organization-specific data lives in the package (your sites, keys, and rules go in
a private config file).

## Command reference

The full command list now lives on the **[`contextlake` command reference](cli-reference.md)** page.

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

Running the whole pipeline at once, command composition, scheduling, and the git hook now have their own page: **[Bootstrap and keep fresh](bootstrap.md)**.

## Reading the console output

Decoding the progress bar, the status vocabulary, and the stdout/stderr split now has its own page: **[Reading the console output](console-output.md)**.

## Ownership and SMEs

Ranking likely owners and subject-matter experts from git history (`contextlake owners` / `who_knows`) now has its own page: **[Ownership and SMEs](ownership.md)**.

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
