# Semantic search

Semantic search (optional) adds natural-language retrieval on top of the graph, so you can find code by
what it does even when you don't know its name. Enable `[embeddings]` in the config (local-first, vectors
come from an Ollama model by default, so code never leaves the machine), run `contextlake embed` to
vectorize the indexed nodes into a local store, and `serve` then exposes two retrieval tools:

- **`semantic_search`** for queries where the exact symbol name is unknown.
- **`hybrid_search`**, which seeds Personalized PageRank with the embedding hits and propagates relevance
  across the graph (HippoRAG-style) to surface structurally related nodes, a function's callers, a
  package's dependents, that a pure semantic match would miss.

## Backends and tuning

The vector store uses an exact pure-Python cosine scan by default; install the optional ANN backend with
`pip install "contextlake[kb-vec]"` (sqlite-vec) for larger workspaces. Three `[embeddings]` keys tune it:

- **`vector_backend`** (default `auto`) picks `sqlite-vec` when that extra is installed and falls back to
  the pure-Python `brute` scan otherwise; force one with `vector_backend = "sqlite-vec"` or `"brute"`.
- **`vector_chunk_size`** (the sqlite-vec `vec0` KNN chunk size, default 1024; clamped to a multiple of 8)
  is applied when the vector store is first created, so re-embed from scratch to change an existing store.
- **`batch_size`** (default `64`) sets how many nodes are embedded per batch.

## What gets embedded

The code **definitions** (classes, functions, methods, interfaces, structs, enums) and HTTP endpoints,
each with its name, qualified name, file path, and captured **signature and docstring**, so a
natural-language query like *"refund a payment to the original card"* finds the right function even when
its name says nothing of the sort. (Measured on the golden-query harness, adding signature + docstring
doubled MRR and took hit-rate to 100% on natural-language queries.) File, module, and package nodes are
deliberately not embedded: a path or a shared package name is low semantic signal, and skipping them keeps
results clean and avoids re-embedding cross-repo shared nodes once per referencing repo.

## Which model?

With that content embedded, the tiny static models punch far above their weight: on a 24-query
natural-language bake-off, the default `potion-base-8M` (~30MB, ~1ms per query) outscored the ONNX
`bge-small` transformer, and `minishlab/potion-base-32M` (~120MB, same engine and extra) scored best of
all, MRR 0.95 with a perfect hit-rate, at a tenth of the ONNX query latency. If you want the quality bump,
it's one config line: `model = "minishlab/potion-base-32M"` under `[embeddings]` (on a fresh vector store,
the identity guard refuses to mix models).

Like `index`, `embed` is **incremental**: it re-embeds only repos whose indexed HEAD moved since they were
last embedded, so a scheduled refresh over a large fleet stays cheap. Pass `--force` to re-embed
everything. When an upgrade changes the embedded text format itself, `embed` detects the stale store and
re-embeds everything once, announcing why, then incremental behavior resumes.

A single query returns cited hits (`repo · file:line · kind · name`) that span repos *and* languages, here
the C# and Python payment paths together. `--retriever fts|semantic|hybrid` picks keyword, vector, or
graph-propagation ranking:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-query.png" alt="contextlake query payment --retriever hybrid output: ten cited hits spanning acme/orders-api (Python PaymentClient, charge, refund) and acme/payments-api (C# PaymentProcessor, Charge, Refund, CardGateway), each with repo, file:line, kind, and name." width="820">
</p>

## See also

- [Index the code graph](index-code-graph.md)
- [Connect and enrich](connect-enrich.md)
- [Serve it to your editor](serve.md)
