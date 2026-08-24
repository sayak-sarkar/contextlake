# Roadmap

`contextlake` is three layers you adopt one at a time: mirror the repositories you can
reach across GitLab, GitHub, Bitbucket, or Gitea/Codeberg/Forgejo; build an optional,
local-first knowledge graph over them (code + dependency graph, semantic search, a
curated wiki, connectors); and serve the result to your AI tools over MCP. Everything
below the mirror is optional and stays off by default.

## Shipped

- **Core sync**: discover, clone, update, branch-select, verify, audit; resilient and
  concurrent across hundreds of repos, with branch-safety, `--dry-run`, logging, and a
  colorful CLI. Namespaced under `mirror` (`mirror fetch/clone/update/branches/verify/
  status/sync/audit`).
- **Knowledge graph**: tree-sitter parsing across **27 languages** (Python, JS/TS, C#,
  Go, Rust, Java, C/C++, and more) plus Terraform, SQL DDL, and manifest-derived
  cross-repo package dependencies. Incremental indexing with bi-temporal `--as-of
  <commit>` queries over per-commit shard snapshots. Namespaced under `kb`
  (`kb index`, `kb lint`, …).
- **Connectors**: Atlassian (Jira + Confluence), Figma, GitLab (MRs/issues), Slack and
  Zendesk, sharing one seam. Beyond repo-level links, a shared `link_to_code` matcher connects
  external content directly to the specific symbols it's actually about (a GitLab MR's
  file-touch, a Figma frame name, a Slack message mention), not just the repo.
- **Semantic search**: a pluggable embedder (built-in CPU model, Ollama, or an API) and
  vector store (optional `sqlite-vec` ANN backend), with `semantic_search` and
  `hybrid_search` (graph-propagated PPR) retrieval, plus a golden-query eval harness
  (`kb eval`) to score precision/recall/MRR against real questions.
- **Curated wiki**: LLM-synthesized, provenance-stamped pages gated by a verification
  council that scores each draft before publishing; coverage-ratio disclosure on every
  page; automatic per-subsystem pages for large, genuinely federated repos (5,000+ nodes,
  no single module dominating); a council review pass can use a stronger model than the
  one that generated the draft.
- **Diagrams**: an offline interactive HTML/DOT/Mermaid graph, plus class, sequence,
  state, ER, and Terraform-deployment diagrams, GraphML/Cypher export, and a composed
  namespace-level C4 diagram (with an optional C1 external-system layer). A `dagre`
  preview layout with HTML-card nodes and SVG export sits alongside the default.
- **Dashboard**: a local, offline-first UI: fleet overview, per-repo anatomy, the
  architecture graph, blast radius, health, search, a natural-language Chat tab, and a
  static `--site` export (used for the project's own public read-only trial demo).
- **Serve over MCP**: an `ask()` router plus 24 underlying tools, 22 of them always present
  (find_definition, find_callers, blast_radius, who_knows, get_wiki, …) and 2 semantic ones
  that register once embeddings exist, stdio/streamable-HTTP/SSE
  transports, and one-command editor steering (`kb steer`) for Claude Code, Windsurf,
  and Kiro.
- **CLI namespacing (breaking, v3.0.0)**: commands are grouped under `mirror` and `kb`
  by the layer they belong to, with grouped `--help`, a typo/abbreviation suggester, and
  shell tab-completion as a core dependency (no opt-in step).

## Future good-to-haves

- **Diagram-generation capability (item 7)**: a richer, eraser.io-style diagramming
  surface at the repo/group/fleet level. Most of the spike has since landed in the real
  product's graph page: dagre layout, DOM-card nodes and dual PNG/SVG export all ship.
  What remains is the group/fleet-level authoring surface itself, and the written spec
  for it.
- **Non-code/media ingestion.** *Shipped, and not as a plugin seam.* PDF, image (OCR)
  and video are all handled inside the built-in `files` source; PDF rides the `kb-pdf`
  extra. What remains open is broader format coverage, not the seam.
- ~~Per-repo wiki-steering file~~, **shipped.** A repo's own `.contextlake/wiki.toml`
  is read from its working tree and stamped on the page that used it.
- **Deeper diagram/wiki cross-linking.** Half done: wiki headings now carry stable,
  deduplicated `id` anchors, so the jump targets exist. The clickable diagram-node → 
  wiki-section jump itself is still to build.
- **Fleet-scale dashboard views.** The **fleet treemap shipped** as a fourth fleet mode
  alongside cards, list and table. A repo-coupling matrix and query-chip navigation are
  still open. (The separate repo-module treemap stays deliberately closed, it is not
  this item.)
- **Docs interactivity.** Live visualizations are already on the site; try-it editors are
  not. The remaining half still needs an explicit call to build or close.
- **Hosted provider breadth.** More connectors (Zendesk has since shipped) and more
  embedding/LLM provider options behind the existing pluggable `Embedder`/`LlmClient`
  interfaces.

Have an idea or a use case? Open an issue: the design goal is a generic, product-grade
tool, so concrete needs shape what lands next.

## See also

- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [contextlake, explained](docs/explained.md)
