# Embeddings and models

Which nodes get a vector, how a long document is split into several, and which embedding
model to run. Read this when a natural-language query misses something you expected it to
find, or before you change the model on a store that already has vectors.

## What gets embedded

**19 embeddable kinds**, drawn from five different parts of the graph. Everything else in the
graph is indexed and queryable by name, but has no vector.

| Where it comes from | Kinds |
| --- | --- |
| Code symbols | `class`, `function`, `method`, `interface`, `struct`, `enum`, `field`, `macro`, `typedef`, `enum_constant`, `global_variable` |
| Web and API surface | `endpoint`, `route` |
| Data contracts | `table`, `view`, `schema_element`, `schema_type` |
| Infrastructure | `resource` |
| Decisions | `adr` |

Each one is embedded with its name, qualified name, file path, and captured **signature and
docstring**. The extra text is the point: a name alone is thin signal, and a query like
*"resend a reading that failed to upload"* tends to share its words with the signature and the
docstring rather than with the identifier.

The set is wider than code on purpose. A route, a table and an architecture decision record
are all semantically searchable, which is worth knowing before you write off a
natural-language question about your schema or your infrastructure as out of scope.

### The C and C++ trade-off, measured

Data members, macros, typedefs, enum constants and file-scope variables are the majority of
the symbols in a C or C++ tree. Before they had vectors, no semantic or hybrid query could
return one of them at all.

They are also thinner signal than a function: a data member usually carries a name and a type
and nothing else. Measured on a large legacy tree, adding them costs the kinds that were
already embedded about **five percentage points of recall@10**, in exchange for tens of
thousands of symbols going from unreachable to findable.

If your tree is C++-heavy and you would rather have the sharper ranking than the reach,
`kb query --kind` narrows any single query. The trade-off is recorded per kind in
`src/contextlake/kb/kinds.py`, so it can be revisited with the numbers in view.

### What is deliberately not embedded

File, module and package nodes. A path or a shared package name is low semantic signal, and
skipping them keeps results clean. It also avoids re-embedding a cross-repo shared node once
per referencing repository.

## Sampling a large fleet

`kb embed --limit N` caps how many nodes are embedded per repository in one pass: the first
`N` embeddable nodes of each repo's shard, in shard order.

Read it as a **sampling** knob, not a resumable one.

| Behaviour | What it means for you |
| --- | --- |
| Takes the same first `N` every time | A second `--limit` pass adds nothing new. |
| Replaces that repo's vectors rather than adding to them | The store never accumulates across passes. |
| Never stamps the repo as fully embedded | A later plain `kb embed` still does the whole repository. |

Use it to try the pipeline on a large fleet before committing to a full run, not to embed a
repository in instalments.

## How ingested documents are chunked

An **ingested document** (`kb ingest`) is embedded as several overlapping vectors rather than
one vector over the whole page: **1200 characters** each with **200 characters of overlap**,
split on paragraph boundaries so a sentence is never cut in half.

A document is still one node and `kb query` still returns nodes, so chunking is invisible from
the outside. The vector store keys each chunk separately and keeps a document's best-scoring
chunk when it ranks results.

The reason is dilution. A 14 KB page embedded as one vector is an average of everything the
page discusses, and a question about one paragraph of it matches that average poorly.

### What the measurement shows

Measured on contextlake's own 29 documents (424 KB, mean 14.6 KB) with 53 queries selected by
their position in the document.

| | One vector per document | Chunked | Delta |
| --- | ---: | ---: | ---: |
| Hit rate | 71.7% | 94.3% | +22.6pp |
| MRR | 45.2% | 80.4% | +35.2pp |
| Tokens per query | 276 | 291 | +15 |

13 queries were fixed and 1 regressed. The queries were stratified by depth before indexing,
and the unchunked arm is already worse at depth (62.5% against 79.3% at the surface). That is
the dilution the change is aimed at, visible before any chunking was applied. Chunking also
helps depth more (+29.2pp) than the surface (+17.2pp).

### What the measurement does not establish

One embedder (`model2vec`, which averages; a transformer would truncate instead, plausibly
worse for a long document, and that was not measured), one single-topic corpus, and
verbatim-sentence queries. The absolute numbers are optimistic and only the delta is sound.

The chunk size itself was never tuned. `split_document` in
`src/contextlake/kb/embeddings/chunk.py` takes `max_chars` and `overlap`, so changing them is
one argument. Changing them also makes the numbers above describe a configuration that is no
longer the one running.

### Upgrading an existing store

Document vectors are only rewritten by `kb ingest`, so an existing store keeps its old
whole-document vectors until you re-run it. `kb ingest` clears the ingest partition's vectors
before it writes, so a re-run replaces them cleanly and no stale chunk survives a document
that shrank.

## Choosing a model

| Model | How to select it | What it is |
| --- | --- | --- |
| `minishlab/potion-base-8M` | the default | A **static** model: it looks each token up in a table instead of running a transformer forward pass. Small enough to ship as a CPU default, fast enough that query latency is not the thing you notice. |
| `minishlab/potion-base-32M` | `model = "minishlab/potion-base-32M"` under `[embeddings]` | The larger sibling of the same family. |
| `bge-small` (ONNX) | the `kb-fastembed` extra plus `engine = "fastembed"` | A real transformer instead of a static lookup. |

On a store that already holds vectors the identity guard refuses to mix models, so switch on
a fresh vector store.

Which one is better **on your code** has a local answer, and
[`kb eval`](searching-semantically.md#measuring-retrieval-quality) is how to get it: build a
golden set from queries your team actually types, embed with one model, score, re-embed with
the other, score again. No published ranking against somebody else's corpus is worth as much
as that run.

## Re-embedding

`embed` is **incremental**, like `index`. It re-embeds only repositories whose indexed HEAD
moved since they were last embedded, so a scheduled refresh over a large fleet stays cheap.

- `--force` re-embeds everything.
- When an upgrade changes the embedded text format itself, `embed` detects the stale store,
  re-embeds everything once and announces why, then incremental behaviour resumes.

## See also

- [Searching semantically](searching-semantically.md)
- [Model providers](model-providers.md)
- [Indexing the code graph](indexing-the-code-graph.md)
- [The code graph model](code-graph-model.md)
