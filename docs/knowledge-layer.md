# Knowledge layer

An optional subsystem (`contextlake.kb`) turns your mirrored repositories into a queryable **knowledge
graph** and serves it to AI agents over **MCP**, so an assistant can ask "where is `X` defined?", "who
calls `Y`?", or "which repos depend on package `Z`?" instead of grepping hundreds of repos. It's generic:
it indexes *any* repositories and connects to *any* configured knowledge sources; no organization-specific
data lives in the package (your sites, keys, and rules go in a private config file).

This page orients you; each stage below has its own focused page.

## Install the extra

The knowledge layer needs the `[kb]` extra (Python 3.10 or newer):

```bash
pip install "contextlake[kb]"        # knowledge layer (parse + graph + serve)
# ...or everything for local semantic search in one step (no Ollama or API key):
pip install "contextlake[kb-full]"   # = kb + built-in CPU embedder + sqlite-vec ANN
contextlake doctor                   # check the environment
```

`contextlake doctor` verifies the whole layer in one pass (FTS5, `git` / `glab` on PATH, the store's real
counts, the built-in CPU embedder, and the ANN index) and exits non-zero if anything is wrong, so it
doubles as a CI health gate:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-doctor.png" alt="contextlake doctor output: green ticks for SQLite FTS5, git and glab on PATH, config loads, a reachable store with 4 repos / 29 nodes / 28 edges, the built-in embedder, and the sqlite-vec ANN index, ending in OK." width="820">
</p>

The fastest way to build all of it is one command, `contextlake bootstrap` (see
[Bootstrap and keep fresh](bootstrap.md)). The rest of this section is the map of what that pipeline does,
stage by stage.

## Building it, stage by stage

- **[Index the code graph](index-code-graph.md)**: parse your repos into a typed graph of files, symbols,
  call/inheritance edges, infrastructure, SQL, and web topology.
- **[Connect and enrich](connect-enrich.md)**: link repos to their issues, docs, and designs, ingest
  external documents, and pull grounded external facts in.
- **[Semantic search](semantic-search.md)**: embed the graph for natural-language and hybrid retrieval,
  and measure retrieval quality with `eval`.
- **[Generate the wiki](generate-wiki.md)**: turn the graph into grounded, council-verified prose per repo
  (and per namespace).
- **[Model providers](model-providers.md)**: choose the embeddings and wiki backend (built-in CPU, Ollama,
  OpenAI, Anthropic, or an agent CLI).
- **[Bootstrap and keep fresh](bootstrap.md)**: run the whole pipeline in one command and keep it current.

## Using what you built

- **[Serve it to your editor](serve.md)**: expose the graph over MCP so agents query it directly.
- **[The dashboard](dashboard.md)**: a local, offline-first UI over the whole knowledge system.
- **[Visualize the graph](visualize.md)**: bounded interactive graph slices and the C4 diagram.
- **[Ownership and SMEs](ownership.md)**: who owns a repo or path, from git history.

For the command list see the [`contextlake` command reference](cli-reference.md); to decode a run see
[Reading the console output](console-output.md).
