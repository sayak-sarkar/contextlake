# Roadmap

`gitlab-sync` mirrors the GitLab repositories you can reach and builds an optional,
local-first knowledge layer over them. The mirroring core and the knowledge layer
(indexing, connectors, semantic search, a curated wiki, incremental/bi-temporal
sync) are shipped. What follows is a list of **future good-to-haves** — none are
required for day-to-day use, and everything optional stays off by default.

## Shipped

- **Core sync** — discover, clone, update, branch-select, verify; resilient and
  concurrent, with branch-safety, `--dry-run`, logging, and a colorful CLI.
- **Knowledge layer (optional `[kb]` extra)** — tree-sitter code graph
  (Python/JS/TS/C#), cross-repo dependency graph, SQLite + FTS5 index, an MCP
  server, and `index / connect / embed / lint / wiki / query / serve / doctor`.
- **Connectors** — Atlassian (issues/pages) and Figma (designs), config-driven.
- **Semantic tier** — local-first embeddings (Ollama), a vector store with an
  optional `sqlite-vec` ANN backend, and `semantic_search` + `hybrid_search`
  (graph-PPR) MCP tools.
- **Curated wiki** — LLM-synthesized, provenance-stamped pages gated by a
  verification council (local-first Ollama).
- **Freshness** — incremental `index --workspace`, an `--watch` refresh loop, and
  bi-temporal `query --as-of <commit>` over per-commit shard snapshots.

## Future good-to-haves

- **Hosted provider options.** The embedding and LLM tiers are pluggable behind
  the `Embedder` / `LlmClient` interfaces; add hosted providers (e.g. an
  OpenAI-compatible or Bedrock client) as opt-in alternatives to local Ollama,
  selected by `provider` in config. Local-first stays the default.
- **Full bi-temporal validity windows.** Today "as of commit X" is served from
  per-commit shard snapshots. A deeper model would stamp every node/edge with a
  validity interval (valid-from / valid-to) and *invalidate* rather than overwrite
  superseded facts, enabling cross-repo "as of a point in time" and diffs between
  two commits.
- **Connector breadth.** Run `connect`/`embed` under `--watch` alongside `index`;
  add more knowledge sources (e.g. Slack, Confluence spaces, more trackers) on the
  same generic connector seam; deepen Figma enrichment when an authenticated MCP
  is reachable.
- **CLI flair everywhere.** Extend the colorful status output to the remaining core
  flows (`status`, `fetch`) and add a single live-updating progress bar (instead of
  per-line bars) for very large workspaces.
- **ANN at scale.** Make `sqlite-vec` the default when present, expose its tuning,
  and batch-embed incrementally so only changed repos re-embed.
- **Scheduled service.** A first-class daemon/service mode (systemd unit / cron
  recipe) wrapping `--watch` for index + connect + embed + wiki refresh.

Have an idea or a use case? Open an issue — the design goal is a generic,
product-grade tool, so concrete needs shape what lands next.
