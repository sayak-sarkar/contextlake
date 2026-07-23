# Generate the wiki

The wiki (optional, local-first) turns the graph into prose: a grounded, council-verified Markdown page
per repo, with a provenance footer citing the commit and sources it was built from.

## Running it

Enable `[llm]` in the config (generation runs on a local Ollama model by default, prompts never leave the
machine), or skip the toml entirely and pass `--llm <provider>` (`builtin` | `ollama` | `openai` |
`anthropic` | `cli`), for example `contextlake wiki acme/orders-api --llm builtin`, which enables the tier
inline and scopes generation to the named repo(s).

Run `contextlake wiki`: for each repo it synthesizes a Markdown page grounded strictly in graph facts (top
symbols, dependencies, files) with a provenance footer citing the commit and sources, then puts the draft
through a **verification council**, reviewers score it for accuracy, completeness, and clarity and a
chairman publishes only pages above a configurable threshold. Nothing that fails review is written.

For the LLM backends behind this (built-in CPU model, Ollama, OpenAI, Anthropic, or a local agent CLI),
see [Model providers](model-providers.md).

## Searchable prose

Accepted pages also become **searchable prose**: each page's sections are stored in an isolated
`@wiki:<repo>` partition and, when the semantic tier is enabled, embedded alongside the code vectors, so a
natural-language question can land on the wiki's explanation of a subsystem, cited back to the page file
and labeled advisory (kind `wiki`), never outranking extracted code facts. Pages written before this
existed are backfilled on the next `wiki` run without any LLM calls.

## Cluster (namespace) wiki

Beyond per-repo pages, `contextlake wiki --namespace acme/payments` writes one **cluster page** for a
whole group of repos (everything under that repo-id prefix), narrating how they fit together: which
services call which over HTTP, publish/consume which events, and share which packages, split into coupling
*within* the namespace and coupling to repos *outside* it. Use `--namespaces --depth N` to generate one
page per namespace at that prefix depth. It grounds strictly in the cross-repo edges the graph already
resolved (no new extraction) and reuses the same review council + provenance footer as the per-repo wiki,
so it stays advisory and cited; when the graph shows no coupling it says so rather than inventing a link.
Cluster pages are served over MCP by passing a namespace to `get_wiki`, and shown per group in the
dashboard's fleet overview.

## Incorporating connector enrichment

When `contextlake enrich` has populated a repo's `@enrich:<repo>` enrichment documents (via Atlassian or
MCP search sources), the wiki synthesizer draws on them and incorporates an "External context" section
into each repo's curated page. Each external fact is directly quoted from its source (Confluence page,
Jira issue, or MCP search result) and attributed by source URL or name, never presented as a free
assertion or as an undisclosed code fact. The council still gates the enriched page before it is written,
ensuring external context supplements rather than displaces code-backed facts and that attribution is
clear and verifiable.

The result, rendered in the dashboard's Wiki tab: prose grounded strictly in real symbols, with a
provenance footer citing the exact commit and source files it was built from:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/wiki-rendered.png" alt="The curated wiki for acme/orders-api rendered in the dashboard Wiki tab: an advisory banner, then Overview, Key components (OrderService, PaymentClient), How a request flows, and Notes, grounded in the repo's real symbols with a provenance footer." width="820">
</p>

## See also

- [Model providers](model-providers.md)
- [Connect and enrich](connect-enrich.md)
- [The dashboard](dashboard.md)
