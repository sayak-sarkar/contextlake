# Connect and enrich

Beyond code, contextlake links each repo to its external context (issues, docs, and designs) and can pull
grounded facts from those sources into the knowledge layer. `connect` links repos to items; `enrich`
queries connected sources with codebase-derived terms and stores what comes back.

## Connectors

`connect` enriches the graph with external context. Three connectors ship, sharing one seam:

- **Atlassian**: links each repo to the Jira issues and Confluence pages it references. Issue keys
  harvested from branch/commit names are confirmed against the live tracker (one batched JQL call per site
  prunes false positives and fetches each issue's summary/status), and Atlassian URLs in docs are
  classified into issue/page links. It talks to one or more Atlassian sites over MCP, each independently
  authenticated.
- **Figma**: links repos to the design files they reference, classifying `figma.com` URLs to a stable file
  key.
- **GitLab**: links each repo to its open **merge requests and issues** (read through your authenticated
  `glab`).

Adding another connector is a small, self-contained module, and its output lands in an isolated graph
partition, so re-indexing a repo's code never disturbs its external links. Configure connectors by copying
[`examples/kb.toml.example`](../examples/kb.toml.example) to `~/.contextlake/kb.toml`.

## Managing sources: the `source` command family

Editing `kb.toml` by hand works, but for everyday use `contextlake source` commands let you add, test, and
manage connectors without touching the config file. They rewrite `kb.toml` while preserving your comments,
and work alongside hand-editing if you mix approaches.

The commands:

- **`contextlake source add [--name NAME]`**: guided prompt to add a new connector. Asks for the connector
  type (Atlassian / Figma / GitLab), provides sane defaults, and writes the entry to `kb.toml`. Pass
  `--type`, `--name`, and other flags to bypass the prompt (`--help` shows all).
- **`contextlake source list`**: show all configured connectors (the effective merged config from
  `~/.contextlake/kb.toml`, `.contextlake/kb.toml` if present, and the built-in defaults), with
  reachability status.
- **`contextlake source test SOURCE`**: verify that a specific connector works. Reaches its API, reads
  credentials from the configured env var, lists available items. Shows you exactly what each source will
  ingest without running a full `connect`.
- **`contextlake source enable|disable SOURCE`**: toggle a connector on/off in the config by name, so you
  can pause one without deleting it.
- **`contextlake source remove SOURCE`**: delete a connector entry by name.

An example workflow:

```bash
contextlake source add                # interactive: what type? which workspace?
contextlake source list               # show what you've configured + status
contextlake source test my-atlassian  # does it work? what's in scope?
contextlake connect                   # now link repos to their items
```

`init` can also prompt you to connect a source during first-run setup, and `doctor` reports per-source
reachability as part of its environment check, so hand-editing is optional; the CLI guides you through the
whole flow.

**Every fact carries its receipt.** Each is provenance-stamped (source file + verified date) and
confidence-tagged as one of three tiers, **`EXTRACTED`** (read straight from source/AST), **`INFERRED`** (a
resolved call or link), or **`AMBIGUOUS`** (an unconfirmed candidate), and sanitized before it reaches an
agent. The dashboard and the graph legend use these same tiers.

## Query-driven enrichment

`contextlake enrich` performs **query-driven enrichment**: it derives search terms from each repo's code
graph (the repo's name and its top symbols by graph degree) and queries your connected sources (Atlassian
Rovo search, or any `mcp` source with a `tool` and `arg_template` configured) with those terms, then stores
the returned documents in a searchable, embedded `@enrich:<repo>` partition, idempotent and re-runnable
across the whole fleet or a single repo:

```bash
contextlake enrich --workspace ~/work     # all indexed repos
contextlake enrich acme/orders-api         # one repo
```

Prerequisites: the code graph must be **indexed first** (`contextlake index`), and at least one
term-searchable source must be configured: either an `mcp` source with `tool` and `arg_template` keys, or
an `atlassian` source. Sources without these capabilities (e.g. a plain `files` or `web` source) are
skipped gracefully. Each repo's enrichment documents are stored in their own partition so they can be
re-fetched without clobbering prior results, and are embedded (when the semantic tier is enabled) so they
surface in semantic search results as `document` nodes tagged with their source (`attrs.source`). After
`contextlake wiki` runs, enrichment docs are incorporated into the curated wiki as an attributed "External
context" section, grounded to the code graph's terms.

## See also

- [Index the code graph](index-code-graph.md)
- [Semantic search](knowledge-layer.md)
- [Serve it to your editor](serve.md)
