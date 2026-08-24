# Semantic search

Semantic search (optional) adds natural-language retrieval on top of the graph, so you can find code by
what it does even when you don't know its name. Set `enabled = true` under `[embeddings]` in the config,
run `contextlake kb embed` to vectorize the indexed nodes into a local store, and `serve` then exposes two
retrieval tools:

- **`semantic_search`** for queries where the exact symbol name is unknown.
- **`hybrid_search`**, which seeds Personalized PageRank with the embedding hits and propagates relevance
  across the graph (HippoRAG-style) to surface structurally related nodes, a function's callers, a
  package's dependents, that a pure semantic match would miss.

```mermaid
flowchart LR
  N[("indexed definitions<br/>and endpoints")] --> EMB["kb embed"] --> V[("vectors")]
  Q(["a natural-language query"]) --> A{"any content term<br/>known to the index?"}
  A -->|no| NONE(["no results, and the terms<br/>it could not find"])
  A -->|yes| R["semantic or hybrid retrieval"]
  V --> R
  G[("the graph")] -.->|"hybrid propagates<br/>relevance across it"| R
  R --> H(["cited hits: repo, file:line,<br/>kind, name"])
```

<div class="dg-key">
  <i><b class="dg-sh-step"></b>a rectangle is something that runs</i>
  <i><b class="dg-sh-store"></b>a cylinder is something that persists</i>
  <i><b class="dg-sh-act"></b>a rounded box is a start or an end point</i>
  <i><b class="dg-sh-dec"></b>a diamond is a decision</i>
</div>

`--retriever fts` is the third path: keyword-only, so it reads no vectors and needs no floor of its own.

**Where the vectors come from.** `provider` defaults to `auto`, which uses a local Ollama when the daemon
is reachable *and* already has the configured model pulled, and otherwise the built-in CPU embedder (the
`kb-local` extra). If neither is available it embeds nothing rather than reaching for a network service.
Both of those run on your machine, so on the defaults your code never leaves it; name `provider =
"openai"` explicitly if you want a hosted model instead.

## Backends and tuning

The vector store uses an exact pure-Python cosine scan by default; install the optional ANN backend with
`pip install "contextlake[kb-vec]"` (sqlite-vec) for larger workspaces. Three `[embeddings]` keys tune it:

- **`vector_backend`** (default `auto`) picks `sqlite-vec` when that extra is installed and falls back to
  the pure-Python `brute` scan otherwise; force one with `vector_backend = "sqlite-vec"` or `"brute"`.
- **`vector_chunk_size`** (the sqlite-vec `vec0` KNN chunk size, default 1024; clamped to a multiple of 8)
  is applied when the vector store is first created, so re-embed from scratch to change an existing store.
- **`batch_size`** (default `64`) sets how many nodes are embedded per batch.

What gets embedded and which model to pick are in [Embeddings and models](embedding-reference.md).

## Measuring retrieval quality

`contextlake kb eval` keeps retrieval falsifiable. Point it at a **golden-query JSON file**, each entry pairs
a query with the node ids it should return:

```json
{
  "queries": [
    {"query": "CatalogService", "expected": ["demo_app_catalogservice"]},
    {"query": "charge", "expected": ["charge"], "match": "name", "kind": "function"}
  ]
}
```

Then `contextlake kb eval --golden queries.json` reports **precision@k / recall@k / MRR** plus a **cost**
dimension (estimated tokens per query, and precision per 1k tokens), so "route to the cheapest sufficient
source" becomes a number, not a vibe. Score any retriever with `--retriever fts|semantic|hybrid`
(semantic/hybrid need embeddings built); a change like embed-bodies or a reranker is then judged by
whether the numbers move.

### Which metric to gate on

If you wire `kb eval` into CI, gate on **MRR**, not on hit-rate alone. Hit-rate asks whether the
right node came back inside `k`. It cannot see a **ranking** regression, where the answer is still
returned but sinks beneath noise, and that is the regression a search change is most likely to
cause.

This project learned it the expensive way. A change that lifted a real definition from 32nd of 153
to 1st on a live index moved contextlake's own golden-set numbers **not at all**: the gate read
hit-rate, and the answer had been inside `k` the whole time. On the fixture as it now stands, MRR
reads 0.80 with the current ordering and 0.77 with the ordering that shipped before it, while
hit-rate reads 0.80 for both.

Two things make a golden set able to see this at all:

- **Include the noise.** A fixture of clean, uniquely named symbols cannot express the failure. Real
  repositories have `tests/test_thing.py` full of `test_thing_*` functions, which repeat a symbol's
  token in the name, the qualified name and the path at once, and that is what outranks a
  definition under an unweighted search.
- **Put the floor between measured values.** Measure the broken behaviour and the fixed behaviour,
  then set the floor between them, so the regression you are guarding against demonstrably fails.
  A floor picked to sit under today's number only proves today's number.

### Are the citations real?

Those metrics answer one question: did the right node come back? They say nothing about whether the
`file:line` it carries still points at that symbol, and the citation is what an agent is actually told to
go and read. A wrong citation is worse than a miss, because it looks like an answer.

`--verify-citations` opens every returned node's file at its recorded line and checks the symbol's name is
there:

```console
$ contextlake kb eval --golden queries.json --verify-citations
Citations: 178/180 verified (98.9%) of 184 distinct nodes
  4 unverifiable (no local checkout for the repo)
  name_absent: 2
    src/billing/refund.py:88  (name_absent)
```

Failures are named rather than counted: `file_missing` (the graph outlived the file), `line_out_of_range`
(the file shrank under a stale index), `name_absent` (the line exists, the symbol is not on it),
`no_citation` (a symbol node carrying no file or line at all). A repository whose recorded clone is not on
this machine is reported as **unverifiable** and kept out of the rate, so a run without the mirror reads as
"nothing was checked" rather than as a pass.

It is off by default: it does filesystem work proportional to the results and needs the checkout present.

## See also

- [Index the code graph](indexing-the-code-graph.md)
- [Connect and enrich](connecting-and-enriching.md)
- [Serve it to your editor](serving-over-mcp.md)
