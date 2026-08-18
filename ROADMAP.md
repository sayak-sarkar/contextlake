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
- **Connectors**: Atlassian (Jira + Confluence), Figma, GitLab (MRs/issues), and Slack,
  sharing one seam. Beyond repo-level links, a shared `link_to_code` matcher connects
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
- **Serve over MCP**: an `ask()` router plus 23 underlying tools, 21 of them always present
  (find_definition, find_callers, blast_radius, who_knows, get_wiki, …) and 2 semantic ones
  that register once embeddings exist, stdio/streamable-HTTP/SSE
  transports, and one-command editor steering (`kb steer`) for Claude Code, Windsurf,
  and Kiro.
- **CLI namespacing (breaking, v3.0.0)**: commands are grouped under `mirror` and `kb`
  by the layer they belong to, with grouped `--help`, a typo/abbreviation suggester, and
  shell tab-completion as a core dependency (no opt-in step).

## Future good-to-haves

- **Diagram-generation capability (item 7)**: a richer, eraser.io-style diagramming
  surface at the repo/group/fleet level. Rendering research and a working prototype
  (dagre layout, DOM-card nodes, dual PNG/SVG export, module clustering for large repos)
  are done; the design and a written spec are still in progress before anything ships
  against the real product.
- **Non-code/media ingestion.** PDF, image, and video ingestion as a plugin-seam
  candidate, deliberately not core, no design work started.
- **Per-repo wiki-steering file** (`.contextlake/wiki.toml`): let a repo hint what its
  own wiki should emphasize, DeepWiki-style.
- **Deeper diagram/wiki cross-linking.** A clickable diagram-node → wiki-section jump
  between the two already-shipped surfaces.
- **Fleet-scale dashboard views.** A treemap, a repo-coupling matrix, and query-chip
  navigation for very large fleets, deferred to its own follow-up spec, not yet scoped.
- **Docs interactivity** (try-it editors, live visualizations on the docs site), which was
  quietly descoped earlier; still needs an explicit call to build it for real or close
  it out formally.
- **Hosted provider breadth.** More connectors (e.g. Zendesk, explicitly deferred,
  "not for now") and more embedding/LLM provider options behind the existing pluggable
  `Embedder`/`LlmClient` interfaces.

Have an idea or a use case? Open an issue: the design goal is a generic, product-grade
tool, so concrete needs shape what lands next.
