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

## Aggregating documents (RAG)

Not everything lives in code. `contextlake ingest` pulls **external documents** into the same knowledge
layer, they become `kind="document"` graph nodes and, when embeddings are on, their bodies are embedded so
semantic search spans code *and* docs together:

```bash
contextlake ingest --path ./docs        # zero-config: ingest a folder of files
```

Sources follow a tiny seam, so common ones are **built-in and config-only** while anything heavier is a
**loosely-coupled plugin**: bake in the common, plugin the rest:

```toml
# kb.toml, built-in "files" source (no code, no extra install)
[[sources]]
type = "files"
name = "handbook"
path = "~/notes"
include = ["*.md", "*.txt"]
```

**Writing a plugin** is just a class with `iter_documents()` and one entry point, no fork, no core
dependency:

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
        yield Document(id="123", title="Runbook", text="...", uri="https://...")
```

`contextlake ingest` then discovers `type = "confluence"` automatically. Four sources ship built-in:
`files`, `web`, `api`, and `mcp`. **`web`** fetches URLs and ingests their readable text (stdlib-only):

```toml
[[sources]]
type = "web"
name = "changelog"
urls = ["https://example.com/changelog", "https://example.com/roadmap"]
```

An **`api`** source ships built-in too: GET a JSON endpoint and map its records to documents, with any
bearer token read from an env var (never the config file):

```toml
[[sources]]
type = "api"
name = "tickets"
url = "https://api.example.com/v1/articles"
items = "data.articles"        # dotted path to the record list
text_field = "body"            # which key holds the document text
token_env = "EXAMPLE_API_TOKEN"  # bearer token comes from this env var
```

An **`mcp`** source ships built-in as well: contextlake connects as an MCP *client* (stdio or
streamable-HTTP) to another MCP server, lists its resources, and ingests each:

```toml
[[sources]]
type = "mcp"
name = "team-kb"
command = "uvx"                 # stdio transport: a server to launch...
args = ["some-mcp-server"]
# ...or an HTTP endpoint instead:
# url = "https://mcp.example.com/sse"
```

So contextlake both *serves* a knowledge graph over MCP and *consumes* other MCP servers' resources into
it: the loop closes on the same seam.

An `mcp` source may also declare a search *tool* (not just read its resources) and template
codebase-derived terms into the tool's arguments. This is what powers query-driven enrichment in the
`enrich` stage (above). Declare the tool name and an argument template with substitution placeholders:

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

Both transports work with tool calling: `command` and `args` for stdio, or `url` for streamable-HTTP. The
tool is called with the templated arguments during enrichment, returning documents grounded to the
codebase's query context.

**Additional `[[sources]]` keys.** Beyond the per-type keys above, connector and ingest sources also
accept: `auth_dir`, an isolated OAuth-cache directory (set a distinct one per Atlassian org so their
`mcp-remote` caches never collide); `mcp_command`, a local stdio MCP command to launch instead of a remote
endpoint (e.g. `"figma-mcp --stdio"`); `group`, a GitLab group prefixed to each repo's path to form the
project id; and `per_page`, the API page size (default `50`).

## See also

- [Index the code graph](index-code-graph.md)
- [Semantic search](semantic-search.md)
- [Serve it to your editor](serve.md)
