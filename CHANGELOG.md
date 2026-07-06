# Changelog

All notable changes to contextlake will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **`ask`'s explain route degrades usefully.** When a question like "explain the
  orders-api" hits a repo with no generated wiki, `ask` now returns that repo's
  grounded anatomy (top symbols, packages, languages) from the graph instead of a
  blind semantic search — a structured `brief` beats fuzzy hits for "explain this."
  (Surfaced by a full end-to-end test sweep of the CLI + MCP server, which otherwise
  found no defects.)

## [2.27.0] - 2026-07-06

### Added

- **`ask` — one MCP tool, natural language, auto-routed.** A small-context IDE agent
  no longer has to pick among twenty graph tools: `ask("who calls charge_order")`,
  `ask("what breaks if I change OrderService")`, `ask("explain the orders-api")`. A
  deterministic, offline classifier maps the question to a substrate (definition /
  callers / dependents / impact / owners / explain / search), resolves the symbol or
  repo, and returns one labeled answer — graph facts cited and confidence-tagged, the
  `explain` route clearly marked advisory. The classifier is its own pure module
  (`kb/router.py`), unit- and eval-tested on a golden question set (23/23 route +
  target) so misroutes are falsifiable. It's a convenience front door over the
  specific tools, which remain first-class.

## [2.26.0] - 2026-07-06

### Added

- **`contextlake init` — guided first-run setup.** One command writes a valid mirror
  config (and, opt-in, the knowledge-layer config) instead of hand-authoring TOML/INI:
  it detects the platform, tells you which token env var it will use, and prints the
  next step. Interactive when stdin is a TTY, non-interactive with `--yes` (plus
  `--platform` / `--group` / `--work-dir` / `--no-kb` / `--embeddings`) for scripting.
  Never writes a token to disk; refuses to overwrite existing config without `--force`.

## [2.25.0] - 2026-07-02

### Added

- **The wiki is now searchable prose.** Accepted wiki pages are split into sections
  and stored in an isolated `@wiki:<repo>` partition (mirroring `@connect`/`@ingest`);
  with the semantic tier enabled they embed alongside the code vectors, so a
  natural-language query can land on the wiki's explanation of a subsystem — cited to
  the page file and labeled advisory (kind `wiki`), never outranking extracted code
  facts. Pages written before this existed are backfilled on the next `wiki` run with
  zero LLM calls (freshness-skipped pages included).

## [2.24.0] - 2026-07-02

### Added

- **Multi-platform mirroring: GitHub, Bitbucket, and Gitea (Codeberg / Forgejo)
  join GitLab.** Set `platform = github` (or `bitbucket` / `gitea` / `codeberg` /
  `forgejo`) and `group = your-org` in the config and the whole pipeline — fetch,
  clone, update, branches, verify, status, audit, bootstrap — runs against that
  platform: every enumerator normalizes to the same project shape, so everything
  downstream of the fetch cache is platform-agnostic. Auth is the platform's token
  env var (`GITHUB_TOKEN`, `BITBUCKET_TOKEN`, `GITEA_TOKEN`; public owners work
  tokenless, rate-limited), carried in headers and the git child environment with
  each platform's expected basic-auth username — never in URLs or argv. Self-hosted
  instances point `api_base` at their endpoint. GitLab behavior is unchanged,
  including the `glab` fallback.

## [2.23.0] - 2026-07-02

### Added

- **Semantic search now embeds real code content.** Each node's vector carries its
  captured signature and docstring alongside the name/path metadata, so
  natural-language queries land on the right symbol even when its name is terse.
  Eval-gated before shipping: on the golden-query harness's natural-language set,
  MRR doubled (0.50 → 1.00) and hit-rate went from 0.83 to 1.00 versus name-only
  vectors. Existing stores are detected by a new embedded-text version stamp and
  re-embedded once automatically (with a message saying why); incremental behavior
  then resumes.

### Changed

- **Built-in embedder guidance is now measured, not assumed.** A four-model
  bake-off on the enriched text (potion-8M/32M vs ONNX bge-small and quantized
  nomic-v1.5) showed the tiny static models winning on both quality and latency;
  the docs and config example now name `potion-base-32M` as the one-line quality
  upgrade and keep the 30MB `potion-base-8M` as the zero-config default.

## [2.22.0] - 2026-07-02

### Added

- **`glab` is now fully optional.** With a `GITLAB_TOKEN` (a `read_api` +
  `read_repository` PAT), `clone_method=auto` clones with plain `git`, passing the
  credential as an auth header through the child environment — never on the command
  line and never in the URL, so it cannot leak into `ps` output or `.git/config`.
  Enumeration already used the token-native HTTP client, so the whole mirror now runs
  with just `git` + a token; without a token the glab-then-git behavior is unchanged.

### Changed

- **The share card is built from the approved hero art** (Pebble in the wide misty
  lake) with real typography — Space Grotesk wordmark, Inter tagline, gold Get
  started button — instead of AI-generated text; the same card is the GitHub social
  preview.
- Docs polish: heading slugs now anchor correctly on both GitHub and the docs site,
  internals links to the branch-safety guide where it actually lives, and the
  command reference states the per-command `--help`, the `who-knows`/`blast-radius`
  aliases, and the dashboard `--sample` demo fleet.

## [2.21.0] - 2026-07-02

The product-review hardening release: an end-to-end review as a brand-new
`pip install` user surfaced the gaps between the advertised experience and the
real one; this release closes them.

### Fixed

- **`dashboard --sample` works from a pip install and under `--serve`.** The demo-fleet
  fixture used to live at the repo root (absent from every wheel, so `--sample` crashed
  with `FileNotFoundError`), and the `--serve` path ignored the flag entirely, serving an
  empty dashboard from the real store. The fixture now ships as package data and
  `--serve --sample` serves the fictional fleet from an ephemeral store — the advertised
  zero-setup preview actually is one.
- **A failed enumeration can no longer wipe the project cache.** `fetch` used to write
  the partial (often empty) result over a good cache on any mid-paging failure, print a
  green checkmark, and exit 0. It now raises, leaves both caches byte-identical, and
  `fetch`/`sync` exit non-zero; a genuinely empty enumeration warns instead of celebrating.
- **`bootstrap --workspace` is honored** (it was silently ignored in favor of the
  mirror's `work_dir`), and the steering files follow it. Indexing a workspace with zero
  git repositories now exits non-zero with guidance instead of reporting
  `✓ Bootstrap complete` over an empty knowledge base.
- **MCP `serverInfo` reports contextlake's version** instead of the MCP SDK's.

### Added

- **Per-command help.** Every verb is a real argparse subcommand: `contextlake sync --help`
  shows only sync's flags with worked examples, bare `contextlake` prints the front door
  (description, command list, getting-started) instead of an argparse error, and
  `contextlake index PATH` works as a positional. Flags may still appear before the
  command, so existing scripts keep working.
- **`who-knows` and `blast-radius`** as CLI aliases for `owners` / `impact`, matching the
  MCP tool vocabulary.
- **`serve` says when the semantic tools are gated.** When `semantic_search` /
  `hybrid_search` are not registered (no `[embeddings]` config, or no `contextlake embed`
  run yet) the server now states it and why, instead of the tools silently vanishing.
- **A Docker install block** for the published `ghcr.io/sayak-sarkar/contextlake` image,
  which now carries OCI source labels linking it back to the repository.

### Changed

- **The CLI introduces itself as what it is** — a local context layer that mirrors,
  indexes, and serves real source over MCP — rather than "GitLab Workspace
  Synchronization CLI Tool".
- **One coherent story across the docs**: the install leads with
  `pip install "contextlake[kb]"` (with the Python 3.10 floor stated at the point of
  use), one MCP server name (`contextlake-kb`), one bootstrap invocation, one canonical
  tagline tail everywhere, a complete MCP tool list in the serve guide, and a
  contributor setup (`[dev,kb]`) that can actually run the suite.
- **PyPI metadata points back at the product**: Homepage is the site, with
  Documentation/Issues links; the summary carries the anti-hallucination clause; the
  classifier and keyword sets state the supported Python range and positioning.

## [2.20.1] - 2026-07-01

### Fixed

- **README doc links now resolve on the PyPI project page.** They were relative
  (`docs/…​.md`), which 404s on PyPI (it renders the README but doesn't host the repo files);
  they're now absolute GitHub URLs. The docs-site build still rewrites them back to local pages.

### Added

- **CLI and rendered-wiki screenshots** in the docs. The knowledge-layer guide now shows real
  terminal output for `doctor`, `index`, `query`, `owners`, `impact`, and a single-repo graph,
  plus a curated wiki rendered in the dashboard — all captured from a generic demo fleet.

## [2.20.0] - 2026-06-30

### Added

- **Dashboard fleet layout switcher — Cards / List / Table.** The fleet overview now
  offers three densities (rich cards, dense rows, an aligned sortable-look table), each
  with an icon, persisted in localStorage.
- **"What am I looking at?" info popover** (ⓘ in the header) explaining nodes, edges, the
  three confidence levels (and that the chips filter by them), and the Live vs. Static data
  source — plus a visible "Show" label on the confidence filter.
- **Actionable empty states.** A repo with no wiki offers a **"Generate wiki"** button (copies
  `contextlake wiki <repo>`); blast-radius / out-of-snapshot views offer **"Run live server"**.
- **`--llm <provider>` and `--llm-model <model>` CLI flags** for `wiki` — enable the LLM tier
  inline (`builtin` | `ollama` | `openai`) without editing `kb.toml`, e.g.
  `contextlake wiki flx/app --llm builtin`.
- **A guided dashboard tour** ([docs/dashboard.md](docs/dashboard.md)) — a step-by-step
  walkthrough with screenshots (fleet layouts, repo anatomy, the architecture graph, blast
  radius, and generating a wiki), linked from the README and knowledge-layer docs.

### Fixed

- **`wiki` / `embed` / `connect <repo>` now scope to the named repo(s).** The positional repo
  id was ignored, so these silently ran across the entire indexed fleet; an unknown id now
  errors cleanly instead of processing everything.
- **Dashboard: repo names no longer truncate** — card names wrap to two lines (basename + a
  front-clipped namespace path), and the full id is on hover.
- **Dashboard: no more page-height jump on hover** — card metadata is always visible instead of
  expanding on hover.
- **Dashboard: architecture graph renders fully on first view** — the embedded cytoscape graph
  re-fits when its iframe gets real size, instead of leaving nodes painted off-screen until a
  manual zoom/click.
- **Dashboard: dead-end clicks are graceful** — repos beyond the static slice show a "run the
  live server" state, not a scary error.
- Replaced the crude inline otter illustration in empty states with the **Pebble** mascot art.

### Changed

- Dashboard stat / confidence numbers are **thousands-formatted** (`1,013,948`).
- Static-export per-repo relationships are built from a **single bucketed edge scan**
  (`repo_relationships_bulk`) instead of rescanning all edges per repo.
- The **`--sample` showcase is now a multi-repo demo fleet** (a fictional `acme` org) rather
  than a single repo, so the dashboard's sample mode reads like a real fleet.

## [2.19.2] - 2026-06-28

### Fixed

- **`impact <symbol>` no longer silently resolves an ambiguous name to the wrong repo.**
  A bare name (e.g. `Node`, `Order`) was resolved via a full-text search and the top hit
  taken blindly, so a common name could seed an unrelated repo's symbol and report a
  confidently-wrong (often empty) blast radius. Resolution is now exact-id → exact-name →
  fuzzy: when a name is defined in several repos the CLI lists the candidates and asks you
  to narrow with `--repo`, and `--repo` now actually scopes resolution. The dashboard's
  change-impact API returns `ambiguous` + `candidates` for the same case. Shared resolver
  (`impact.resolve_target`) drives both the CLI verb and the dashboard so they behave
  identically.

## [2.19.1] - 2026-06-28

### Fixed

- **Dashboard: the command palette (and the provenance drawer and pin chip) no longer render
  stuck-open.** Their `[hidden]` attribute was being overridden by a CSS `display:` value, so
  the "Jump to a repo, symbol, or action" palette stayed permanently open as a full-screen
  overlay that blocked the entire interface. Added `[hidden]` guard rules so each element is
  actually removed from layout when closed.

## [2.19.0] - 2026-06-28

### Added

- **`contextlake dashboard` — a local knowledge-system dashboard UI.** A self-contained,
  offline-first single-page app over your store: fleet overview (domain-grouped), per-repo
  anatomy / README / wiki / owners / connector links, repo→repo dependency / HTTP-flow /
  event-flow (each with confidence + provenance, never shown as ground truth), an embedded
  interactive architecture graph, a change-impact explorer, health, and search. `--serve`
  runs it live against your store; `--site DIR` exports a static `file://`-safe copy. Privacy:
  a real-store `--site` warns "review before publishing"; `--anonymize` hashes author
  identities and drops external URLs + README/wiki prose; `--sample` builds a guaranteed-generic
  showcase from the bundled fixture. Read-only in v1 (sync/MCP controls planned).

## [2.18.0] - 2026-06-28

### Added

- **Built-in `mcp` source for `ingest`.** contextlake now connects as an MCP *client*
  (stdio via `command`/`args`, or streamable-HTTP via `url`) to another MCP server, lists
  its resources, and ingests each into the graph + semantic store. So it both serves a
  knowledge graph over MCP and consumes other servers' resources — on the same source seam.

## [2.17.0] - 2026-06-28

### Added

- **Built-in `api` source for `ingest`.** GET a JSON endpoint and map its records to
  documents — `items` (dotted path to the record list), `id_field`/`title_field`/`text_field`,
  and an optional bearer token read from an env var named by `token_env` (the secret never
  lives in config). Standard library only.

## [2.16.0] - 2026-06-28

### Added

- **Built-in `web` source for `ingest`.** Fetch one or more URLs and ingest their readable
  text (`[[sources]] type="web"`, `urls = [...]`) into the graph + semantic store. Standard
  library only (`urllib` + `html.parser`) — no new dependency and no headless browser; the
  network is touched only when a `web` source is configured.

## [2.15.0] - 2026-06-28

### Added

- **`contextlake ingest` — aggregate external documents (RAG) into the knowledge layer.**
  Documents become `kind="document"` graph nodes and, when embeddings are on, their bodies
  are embedded so semantic search spans code *and* docs. Zero-config: `contextlake ingest
  --path ./docs`.
- **A source/plugin seam (`contextlake.kb.sources`).** Common sources are **built-in and
  config-only** (the `files` source ships now); anything heavier is a **loosely-coupled
  plugin** — a class with `iter_documents()` registered via a `contextlake.sources` entry
  point, discovered automatically (a broken plugin is skipped, never fatal). Bake in the
  common, plugin the rest.

## [2.14.0] - 2026-06-28

### Added

- **`contextlake impact <symbol>` — change-impact / blast radius from the shell.** Lists
  what calls or depends on a node (reverse-reachability over the graph, `--hops` deep,
  `--limit` capped), so "what could break if I change this" no longer needs an editor or
  MCP client. Resolves a node id or falls back to a name search. The walk is shared with
  the `blast_radius` MCP tool (one implementation in `kb/impact.py`).

## [2.13.0] - 2026-06-28

### Added

- **Ownership / SME lookup from commit history.** New `contextlake owners <repo>`
  (optionally `--path SUBDIR`) ranks likely owners / subject-matter experts straight
  from git history — zero-config, no index needed — using a recency-weighted blend of
  commit volume and lines changed, so recent active contributors outrank a long-departed
  prolific author. Exposed to agents over MCP as the `who_knows(repo, path?, limit?)` tool.

## [2.12.0] - 2026-06-28

### Added

- **`connect --watch` and `embed --watch`.** The live-refresh loop that `index` already
  had now covers the connector and embedding passes too — `connect --watch` re-links and
  `embed --watch` re-embeds on an interval (`--interval N`, default 60s; Ctrl-C to stop),
  each re-resolving its targets so newly indexed repos are picked up. `embed --watch`
  stays cheap by re-using the incremental HEAD gate.
- **Tunable sqlite-vec chunk size.** A new `[embeddings] vector_chunk_size` setting exposes
  the sqlite-vec `vec0` KNN chunk size (default 1024) for tuning large stores. Clamped to a
  multiple of 8; applied when the vector table is first created (re-embed to change it).

## [2.11.0] - 2026-06-28

### Changed

- **`contextlake index` with no arguments now indexes the current directory** instead of
  doing nothing, so `cd my-repo && contextlake index` just works. Pass `--source PATH` or
  `--workspace DIR` to index elsewhere.

## [2.10.0] - 2026-06-28

### Added

- **Incremental `embed`.** `embed` now re-embeds only repos whose indexed HEAD has moved
  since they were last embedded (tracked per-repo in the vector store), so a scheduled
  embed over a large fleet stays cheap, like `index` already is. `--force` re-embeds
  everything; a partial `--limit` run never updates the gate.
- **`.contextlakeignore`** — drop one at a repo's root to exclude your own paths from
  indexing (one glob per line; `*.lock` ignores by name anywhere, `vendor/` prunes a
  directory). A small, dependency-free subset of gitignore syntax; ignored files are
  counted and reported, never silently dropped.

### Changed

- **Colorful output now reaches `status` and `fetch`.** `status` prints a right-aligned,
  glyph-coded summary (`✓` synchronized, `⚠` missing/extra), and `fetch` styles its header and
  final count, matching the existing coloured per-repo output of `clone` / `update` / `branches`.
  Still plain and `NO_COLOR`-friendly when not a TTY.

## [2.9.1] - 2026-06-26

### Changed

- **README overhaul** (this also fixes the instruction shown on PyPI): corrected the primary install
  to `pip install contextlake` (the old `pip install .` only works from a clone), led with the value
  prop, a real graph screenshot, the Pebble mascot, and a branded "How it works" architecture diagram,
  and tightened the prose. Images are committed PNG/JPG with absolute URLs so the README renders
  identically on GitHub and PyPI (no SVG-only assets). Removed em-dashes across the prose docs.

## [2.9.0] - 2026-06-26

### Added

- **Graph readability overhaul, the dense-graph pain points are fixed.** Three long-standing
  complaints addressed in the shared visualizer (`graph --serve`, `--site`, and every embedded graph):
  - **Zoom floor**, "fit" no longer shrinks a big graph into unreadable specks. A clamp keeps any
    fit at or above a readable zoom (≥0.45); below that it snaps to the floor and re-centres, so you
    always land somewhere scannable instead of scrolling in 5–10 times.
  - **Level-of-detail labels**, dense graphs no longer pile their text into an illegible smear. Below
    a readable zoom only the higher-degree hubs keep their labels (degree-gated by zoom tier); hovering
    or selecting any node always reveals its label, and search/highlight are unaffected.
  - **Semantic cluster zoom** (namespace overview), zoom into a region and the on-screen namespace
    clusters expand into their repos; zoom back out and they collapse. A hysteresis gap prevents
    flapping, and the zoom path never re-frames, so it can't feed back on itself.
  - **Minimap**, a custom radar (bottom-right, no new dependency) showing every visible node; click or
    drag to recentre the main view. Tracks filters and cluster expand/collapse live.
  - **On-canvas legend key**, the node legend now shows each kind's actual glyph (the same icon the
    node paints), plus a collapsible key for edge-confidence line styles and per-language repo
    lettermarks, so the iconography is self-explanatory. All still offline/self-contained.

### Changed

- **Captured docstrings + signatures now feed the wiki and `get_repo_brief`.** `repo_brief`'s top
  symbols carry their `doc` + `signature`, so the LLM-wiki is synthesized from real docstrings (not
  just symbol names) and `get_repo_brief` returns them per symbol, closing the capture→consume loop
  for the doc/signature feature (richer, better-grounded wikis and repo anatomy).
- **`build_vector_store` and `SqliteStore.search` no longer fall back silently.** A sqlite-vec load
  failure now warns that search dropped to brute force; a search `OperationalError` is logged (DEBUG
  for an expected malformed-FTS query, WARNING for a real DB problem) instead of always returning `[]`.
- **Deduplicated HTTP/util helpers** (`_ollama_reachable`, `_post_json`, `_chunks`), previously copied
  across the llm/ and embeddings/ providers and the connector, into one stdlib-only `kb/_util`. No
  behaviour change.

### Fixed

- **Safety gate now fails *closed* on an indeterminate git state.** `has_uncommitted_changes` and the
  branch/HEAD reads in the sync core swallowed errors and returned a permissive default, so a failed,
  timed-out, or non-repo git call read as "clean / safe to modify" or "no change", silently
  mis-driving the destructive update/stash/merge they guard. They now check return codes + add
  timeouts and treat any unknown state as unsafe; `_rev_parse` and `_collect_branch_info` raise on a
  git failure instead of returning an empty string that misreads the update.
- **`bootstrap` and `embed`/`wiki`/`connect` now exit non-zero on failure.** `bootstrap` ignored every
  stage's result and always reported success; the three commands returned `0` even when every repo or
  source in a non-empty work set failed (embedder/LLM/connector unreachable → zero output, CI green on
  a broken knowledge layer). `bootstrap` now propagates stage failures (and hard-aborts if the
  foundational index stage fails); the commands return non-zero on total failure.

### Security

- **`.dockerignore` now excludes the gitignored local config/secret files** (`.gitlab_sync.ini`,
  `.contextlake.ini`, `.contextlake.kb.toml`, `.genericity-denylist`) so a local `docker build .`
  can't bake them into an image. The published image is unaffected (built from a clean checkout).

## [2.8.0] - 2026-06-26

### Added

- **Definitions now capture their docstring + signature** (on node `attrs`: `doc`, `signature`),
  **surfaced through the MCP `NodeOut`** (`get_node` / `find_definition` / neighbors etc. now return
  `doc` + `signature`), so an agent gets a function's purpose and parameters in one call. This is also
  the additive groundwork for body-aware embeddings, the `node_text()` change that would feed bodies
  to the embedder stays gated on the eval harness (quality measured, not assumed). Best-effort and
  multi-language: signatures across py/js/ts/c#, and docstrings from Python first-statement strings,
  **JSDoc** (`/** */`), and **C# XML** (`///`) leading doc-comments (plain comments are ignored).

## [2.7.0] - 2026-06-26

### Added

- **MCP: `repo_event_flow(repo, direction, limit)`**, repo→repo **event** flow (who publishes events
  that whom consumes), from the topic two-hop (`publishes_event ⨝ consumes_event`). Completes the
  cross-repo flow trio alongside `repo_dependencies` (package) and `repo_flow` (HTTP); the SQL already
  existed (used by the overview) but had no dedicated tool.
- **MCP: `get_readme(repo)`**, the repo's own README read straight from its local clone (offline).
  Ground truth (the maintainers' words), distinct from the advisory synthesized `get_wiki` prose.
- **MCP: `get_repo_brief(repo)`**, a repo's "anatomy" from its indexed graph: node/edge counts, kind +
  language breakdown, top symbols by connectivity, packages, and a file sample.
- **MCP: `list_repos(include_stats)`**, the repo fleet with per-repo branch, indexed head, last-index
  time, and node count, the dashboard's repository list.
- **MCP: `get_repo_links(repo)`**, a repo's cross-links to Jira / Confluence / Figma / GitLab (url,
  title, status), grouped by relation. Populated by `connect`; served offline afterward.
- **MCP: `graph_health()`**, knowledge-graph health as data (stale repos + dangling edges, with a
  sample) for the dashboard's health panel; `lint`'s logic is now a reusable `lint_result()`.

## [2.6.0] - 2026-06-26

### Security

- **Genericity guard hardened, the leak-detector no longer leaks.** The org-token denylist used to be
  hardcoded in the test file (shipping real org identifiers in the published package). It now lives
  **outside the repo**, supplied via the `CONTEXTLAKE_GENERICITY_DENYLIST` env var or a git-ignored
  `.genericity-denylist` file (CI uses a secret), so no real token is ever committed. The scan also
  now covers **every git-tracked file** (not a fixed list), and an always-on structural check rejects
  any non-allowlisted email address even when no denylist is configured.
- **Removed deployment-scale figures from docs.** Genericized specific fleet counts (the example
  `status` output, the overview-feature notes) to illustrative values, so nothing in the published repo
  is tied to any particular deployment's repository count.
- **Test-locked the offline boundary (INV-2).** A new test blocks all outbound sockets and asserts the
  core commands (`index`/`query`/`graph`/`lint`/`embed`) still run, while `connect` degrades rather than
  fails, proving contextlake is safe in air-gapped/egress-restricted environments, with enrichment the
  single opt-in online step. Documented in `docs/storage.md`.

### Added

- **`eval` now scores any retriever and reports a cost dimension.** Retrievers are built by factories
  (`make_fts_retriever` / `make_semantic_retriever` / `make_hybrid_retriever`) that close over their
  deps, so semantic and hybrid are scorable, not just FTS (the old fixed call site couldn't pass a
  vector store + embedder). The harness now also reports **estimated tokens per query** and
  **precision per 1k tokens**, making "route to the cheapest sufficient source" measurable, and
  `eval --retriever fts|semantic|hybrid` selects which to score. Ships a seed golden set at
  `examples/fixtures/golden-queries.json`.

## [2.5.1] - 2026-06-26

### Fixed

- **README logo now renders on PyPI.** The header glyph used a repo-relative `src`, which PyPI can't
  resolve (it doesn't host the repo files), so it showed as a broken image on the project page. Pointed
  it at the absolute `raw.githubusercontent.com` URL (correct `image/svg+xml` content-type, verified
  through PyPI's own `readme_renderer`). Badges were already absolute.

### Changed

- **Docs reconciled with the shipped MCP surface.** `docs/knowledge-layer.md` now lists the cross-repo
  tools (`repo_dependencies`, `repo_flow`, `blast_radius`, `get_wiki`) alongside the existing graph
  tools, and the README command table documents `eval` (the golden-query retrieval-quality harness).

## [2.5.0] - 2026-06-26

### Added

- **`[kb-full]` one-step install for local semantic search**, `pip install "contextlake[kb-full]"`
  pulls the knowledge layer + the built-in CPU embedder (`kb-local`) + the sqlite-vec ANN backend
  (`kb-vec`) together, so `index → embed → semantic search` just works with no Ollama and no API key.
- **Repo nodes show their primary language, the fleet's tech stack at a glance.** In the overview,
  each repo node now carries a lettermark (`PY`, `JS`, `TS`, `C#`, …) for its dominant language (a
  single GROUP-BY over data the parser already records), so an architecture map reads its stack
  without clicking in. Trademark-free white-on-navy lettermarks, inlined offline; unknown languages
  keep the generic repo glyph.
- **Architectural edges are now labelled, flows read like a C4 diagram.** Dependency / flow edges
  (`depends_on`, `calls_http`, `exposes`, `flow`, `publishes`, `publishes_event`, `consumes_event`)
  carry an autorotated label of the relation plus its context where meaningful (`depends_on · requests`,
  `calls_http · /v1/orders`, the event topic). Structural edges (`calls`/`contains`/`imports`) stay
  unlabelled so the hundreds of them don't bury the diagram in text.
- **Graph nodes now carry type glyphs, the first step toward architecture diagrams.** Every node is
  painted with a Lucide-style icon for its kind (file, class, function, package, repo, HTTP endpoint,
  event topic, …) so a graph reads by *type* at a glance instead of by colour alone. Glyphs are inlined
  as percent-encoded SVG `data:` URIs (no CDN, no sprite fetch, the page stays a single offline file),
  and each glyph's stroke colour is chosen per node fill at build time (white on the dark `repo` node,
  dark on the light `module` node) so it never washes out. Flow nodes (`endpoint`/`topic`) joined the
  palette + legend.
- **`--site` now renders the LLM-wiki as cross-linked pages.** Each repo with a generated wiki gets a
  `wiki-<slug>.html` (the index links it, the page links back to the graph), rendered by a tiny
  dependency-free Markdown→HTML converter (HTML-escaped, the wiki is untrusted LLM output), carrying
  the same fresh/stale badge as `get_wiki`. Stays fully offline, zero new deps.
- **MCP: `get_wiki(repo)`, serve the LLM-wiki to agents (with a staleness signal).** The generated
  wiki was written to `<store>/wiki/` but read by nothing; now an agent can fetch a repo's wiki prose
  (sanitised Markdown), explicitly labelled **advisory** (verify against cited sources; never outranks
  EXTRACTED facts) and carrying **`stale`**, true when the wiki's `head_commit` differs from the
  repo's current indexed head, so prose describing changed code is never cited as current.
- **MCP: `blast_radius(node_id, hops)`, "what could break if I change this".** Bounded transitive
  *reverse* reach over incoming `calls` + `depends_on` edges (configurable), breadth-first, capped by
  `hops` and `limit`. Each hit carries its hop distance, the relation, and confidence (EXTRACTED-first,
  `truncated` when capped), an impact slice for agents, made correct by the AMBIGUOUS-edge change
  below so the hottest symbols aren't missed.

### Changed

- **`embed`'s "disabled" message is now actionable.** Instead of the dead-end "Embeddings are
  disabled", it names the exact next step, install `contextlake[kb-full]` (when the embedder is
  missing) and/or set `[embeddings] enabled = true`, and notes the one-time ~30 MB model download,
  so the post-`bootstrap` "Build semantic vectors" stage no longer silently goes nowhere.
- **Documented and test-locked the no-pollution invariant (INV-1).** `docs/storage.md` now states that
  every generated artifact lives under the store (`~/.contextlake/kb` by default) and never inside a
  synced repo working tree, and `tests/kb/test_no_repo_pollution.py` enforces it by driving the
  generating commands over a temp two-repo mirror and asserting each repo tree is byte-identical.
- **`doctor` now probes ANN (sqlite-vec) availability.** When embeddings are enabled it reports whether
  the native sqlite-vec KNN index actually loads in this environment, or whether semantic search will
  fall back to brute-force cosine, so the silent fallback (a known offline/corporate-env failure mode)
  is visible *before* you embed, not after.
- **Wiki generation is now incremental (skip-if-unchanged).** `contextlake wiki` skips the (expensive)
  LLM call for any repo whose existing page was already generated from its current head commit, so a
  no-op fleet re-run drops from O(repos × LLM calls) to ~0. `--force` regenerates regardless; the
  summary reports how many were skipped.
- **Ambiguous calls are no longer silently dropped.** When a call name resolves to 2–6 candidate
  definitions, indexing now emits an `AMBIGUOUS` `calls` edge to each candidate (de-duplicated,
  self-calls excluded) instead of discarding the call, so the hottest symbols aren't lost and
  blast-radius isn't undercounted. Names matching more than the cap are too generic to be signal and
  are still skipped. AMBIGUOUS edges render dotted in the visualizer.

### Added

- **`contextlake eval --golden FILE.json`, a retrieval-quality harness.** Score a labelled
  `query → expected-nodes` set against the index and get **precision@k / recall@k / MRR / hit-rate**
  (aggregate + per-query), over any retriever (FTS today; semantic/hybrid pluggable). Makes retrieval
  changes (embed-bodies, reranking, a future `ask` router) *falsifiable* instead of vibes. Stdlib-only;
  the golden set is plain JSON, `match` by node id or name.
- **Event/messaging flow extraction (Kafka/MSK, SNS, EventBridge).** Indexing now detects, per file,
  the message topics a repo **publishes** to and **consumes** from (literal topics in Kafka
  producer/`@KafkaListener`/`subscribe`, EventBridge `DetailType`, SNS), as `INFERRED` edges to a
  shared `topic` node. A two-hop join (`publishes_event ⨝ consumes_event`) yields directional
  `publisher --flow--> consumer` repo edges, the direction an event travels, shown in the fleet
  overview alongside HTTP `flow` and structural `depends_on`. High-precision (literal topics only);
  config-variable topics are an honest undercount, never a false link. Re-run `index` to populate.

## [2.4.0] - 2026-06-25

### Added

- **MCP: repo-level architecture tools `repo_dependencies` / `repo_flow`.** Surface the cross-repo
  wedge to AI agents: `repo_dependencies(repo, direction)` returns the package two-hop
  (`dependent → publisher`, weighted), `repo_flow(repo, direction)` returns the HTTP endpoint two-hop
  (`caller → exposer`, weighted), both INFERRED, weight-ranked, with "undercount, verify" guidance.
  Previously these edges fed only the visualizer.
- **`contextlake graph --site DIR`, a cross-linked offline graph site.** Emits `index.html` +
  `overview.html` + one `repo-<slug>.html` per repo with a parsed graph, sharing a single
  `cytoscape.min.js` / `app.css` / `app.js` (referenced, not inlined, so the folder stays small).
  Overview repo nodes link to their repo page (and the inspector gains an "Open this repo's graph →"
  button); every page has an Index/Overview nav. Fully offline. Scope it with **`--repos PATTERN`**
  (comma-separated glob/substring) to build pages for only a subset of repos.
- **`contextlake graph --overview --serve` now serves the whole site live**, rendering each repo page
  **on demand** from the store instead of materialising the fleet up front, so online serving never
  inlines hundreds of MB. Shared assets are served once (browser-cached); `/neighbors` keeps
  click-to-expand inside a repo view.
- **HTTP/REST flow extraction (the first true cross-repo *flow* signal).** Indexing now detects, per
  file, the HTTP endpoints a repo **exposes** (ASP.NET / Express / FastAPI·Flask routes) and **calls**
  (HttpClient / axios·fetch / requests·httpx), as `INFERRED` edges to a shared `endpoint` node keyed by
  a normalised path. A two-hop join (`exposes ⨝ calls_http`) yields directional `caller --flow-->
  exposer` repo edges, which the fleet overview now renders alongside structural `depends_on` (distinct
  colour, aggregated per namespace). Path matching is deliberately conservative (host/query stripped,
  params → `{}`, trivially-generic paths dropped) so unrelated repos don't falsely link. Re-run
  `index` / `bootstrap` to populate. Event/messaging flow (SNS/SQS/EventBridge/Kafka) is the next slice.

### Changed

- **MCP: result budgeting on `get_neighbors` / `find_callers` / `find_dependents`.** They now take a
  `limit` (default 50), order EXTRACTED-first, and return `{..., total, truncated}` instead of an
  unbounded list, so a hub node can't silently blow up an agent's context, and a clipped result
  announces itself.
- **Generated graphs now default to a dedicated `<store>/graphs/` directory** instead of the current
  working directory, `graph` HTML output and `--site` land next to the knowledge base, not wherever
  the command happened to run. Pass `--output` / `--site DIR` to override.

## [2.3.0] - 2026-06-24

### Added

- **Two interlocking overview views, a `Namespace` mindmap and a `Dependencies` graph.** The fleet
  overview now has a mode toggle over one graph. **Namespace** (default) collapses the whole repo fleet
  into its top-level GitLab namespaces (sized by repo count), with aggregated, weight-labelled
  namespace→namespace dependency edges; tapping a namespace expands its repos in place as a compact
  mindmap branch (the rest dims to spotlight it) and tapping again collapses, every repo stays placed
  and searchable. **Dependencies** lays the connected repos out as readable hub-and-spoke clusters.
  Both modes share selection, search, and the inspector.
- **Inspector lists a node's relationships**, each neighbour clickable to navigate to it (in-view
  hop-to-hop). Tapping a node/edge reframes the canvas onto the selection so it stays legible.

### Changed

- **Graph visualizer reworked into an enterprise app shell.** The floating translucent cards are
  replaced by a real layout, a top bar (brand, mode, search), a collapsible left sidebar (view
  controls + Nodes/Relationships legends with live counts), the graph filling the centre, a slide-in
  right inspector, and a status bar, on a CSS grid with a tokenised design system. Adds a **dark
  mode** (Deepwater theme; re-skins the canvas, not just the chrome), icon-button controls, empty/
  loading states, keyboard shortcuts (`/` search, `f` fit, `t` theme, `Esc` clear), and focus-visible
  rings. Still one self-contained offline HTML, zero new dependencies.
- **Fleet overview now shows real cross-repo dependencies.** Repointed from the raw cross-repo
  `imports` join (≈4,800 import-star artifacts from fleet-wide `module` nodes) to the **package
  two-hop** (`publishes ⨝ depends_on`), 217 trustworthy, manifest-derived `depends_on` edges, marked
  `INFERRED` (a deliberate, honest undercount). Repos are labelled by short name (the full path moves
  to the inspector + search) so nodes are distinguishable.
- **Graph-visualizer CSS/JS extracted into `static/app.css` + `static/app.js`** (inlined at emit time
  like the vendored cytoscape), so the source is lint/`node --check`-able. Output is still one
  self-contained offline HTML.

### Fixed

- **Truncation is now visible in the UI.** A bounded subgraph that was clipped used to read as
  complete; a persistent status-bar banner now says "showing N of M, truncated" (honest counts only).
- **Overview readability.** Isolated/no-dependency repos, typically the bulk of a large fleet, no
  longer scatter the connected map into an unreadable speck, they're hidden by default behind a toggle
  (and revealed by search),
  and the layout frames the meaningful core. Expanding a namespace no longer triggers a disorienting
  global re-layout (scoped, position-stable).
- Canvas now reflows/reframes correctly when the inspector or sidebar opens (was leaving the old
  zoom/pan). Dark-mode faded opacity and `prefers-reduced-motion` gating for JS animations.

## [2.2.0] - 2026-06-23

### Added

- **Post-sync repo audit (`contextlake audit`, also auto-runs after `sync`/`bootstrap`).** Scans every
  local clone and reports which repos are effectively empty, **empty** (no commits / no files),
  **readme-only** (just a template README), or **boilerplate** (only meta files like LICENSE/.gitignore)
, plus age/activity: each repo's **creation date** (GitLab `created_at`, captured during fetch; falls
  back to the first git commit) and **last commit date** (from the local clone). Prints an aggregate
  summary (counts, oldest/newest, how many stale >1y/>2y, repos with no commits) and writes a full
  per-repo report as JSON + CSV (`--report PATH`, default `<cache_dir>/repo_audit.json`). The scan is
  parallel, read-only, and works offline; `--no-audit` skips the automatic run. Zero new dependencies.

- **`contextlake graph`, visualize the knowledge graph.** Extracts a *bounded* subgraph (the full
  graph is far too large to draw) and renders it to an interactive, **offline-first** HTML page
  (vendored cytoscape.js, inlined, no network needed; `--cdn` for a small online file), or to
  `dot` / `mermaid` / `json`. Seed from a symbol (`--node`/`--name`+`--kind`/`--search`), a single
  repo (`--repo`), or the whole fleet (`--overview` = repos-as-nodes with aggregated cross-repo
  edges, the architecture map). Scoping knobs `--hops` / `--max-nodes` / `--max-fanout` /
  `--relation` / `--direction` keep hub nodes from exploding (truncation is always logged). The HTML
  is a full mini-explorer: nodes coloured by kind and sized by degree; edge labels hidden until a node
  is selected; **clickable edges with an inspector** (relation, a confidence trust indicator, the
  source `file:line` provenance with copy, context and weight), edges are coloured by relation,
  styled by confidence, and sized by weight, with a **relationship legend that filters by relation**;
  a node **search** box, a **detail panel** (kind / repo / qualified-name / file:line),
  a clickable **legend that filters by kind**, hover tooltips, a **switchable layout**
  (`cose`/`concentric`/`breadthfirst`/`circle`/`grid`, default via `--layout`), and a toolbar
  (fit / reset / **save-PNG**), all wrapped in the **contextlake brand** (inlined lake glyph,
  wordmark, palette, frosted material cards). `--open` launches the browser; `--serve` runs a local
  UI with click-to-expand. Adds zero required Python dependencies.

- **Resilient project enumeration behind slow/corporate DNS (e.g. Zscaler).** When `GITLAB_TOKEN`
  (a `read_api` token) is set, `fetch`/`sync`/`bootstrap` enumerate a group's projects via
  contextlake's own GitLab REST client instead of the `glab` CLI. The `glab` CLI imposes a short Go
  dial timeout that a multi-second corporate DNS lookup trips on every call; the native client uses
  the system resolver's more generous budget, so enumeration completes where `glab` fails. Without a
  token it transparently falls back to `glab` (its own auth). Configurable via `gitlab_token_env`,
  `gitlab_host`, and `network_timeout`; the per-page fetch now retries with backoff on transient
  errors. Additionally, child `git` operations get a widened per-process DNS budget
  (`RES_OPTIONS=timeout:15 attempts:3`, root-free, tunable via `dns_timeout`/`dns_attempts`, and
  skipped if you already set `RES_OPTIONS`) so slow lookups don't surface as `i/o timeout`.

## [2.1.6] - 2026-06-23

### Fixed

- **Quadratic indexing slowdown at scale (the real fix for "indexing got slower the more repos I
  had").** Each node was refreshed in the full-text index with a per-row `DELETE FROM node_fts WHERE
  node_id = ?`; because the FTS5 table has no index on `node_id`, every one of those scanned the
  entire, ever-growing global FTS table, so persisting a repo cost O(repo_nodes × total_store_nodes)
  and the 600th repo took minutes. Now done with one set-based delete + batched `executemany` inserts.
  Re-indexing a repo into a 23k-node store dropped from **6.5s to 0.11s (≈59×)** and is now flat
  regardless of store size; the FTS contents are byte-for-byte identical.

### Added

- **Parallel repository indexing.** `contextlake index --workspace` (and `bootstrap`) now parse
  repositories across worker processes (CPU-bound work), persisting to SQLite serially from the
  parent. Defaults to `cpu_count - 1` (capped at 8); tune with `[kb] index_workers` (set `1` to force
  serial). Uses the `spawn` start method on every platform for identical behaviour on Linux, macOS and
  Windows, and falls back to serial automatically if a worker pool cannot start. With the quadratic
  fix above in place, a full warm re-index of a 33-repo subtree dropped from ~8.8s (serial) to ~3.1s
  (8 workers, ≈2.9×); the parse speedup grows with both repo count and core count.

### Changed

- **Indexing skips generated/derived files and oversized blobs (configurable, logged).** The code
  graph no longer indexes machine-generated files (`*.designer.cs`, `*.min.js`, `AssemblyInfo.cs`,
  `@generated`/`<auto-generated>` headers, …) or code files larger than `max_file_bytes` (5 MB
  default), derived noise that bloats the graph and slows legacy monorepos. Both are reported (no
  silent gaps) and tunable via `[kb] skip_generated` / `[kb] max_file_bytes`. The source the
  generated files derive from is still indexed, so there's no knowledge loss. On a real 3,230-file
  legacy repo this dropped ~26% of files / 4k generated nodes (22.5s → 16.6s).

## [2.1.5] - 2026-06-23

### Added

- **Built-in, zero-config CPU models for the knowledge base, no Ollama and no API key.**
  The embeddings and wiki tiers now accept `provider = "auto"` (the new default), which uses a
  reachable local Ollama, else an in-process **built-in** model, else skips. The built-in embedder
  ships two engines, **model2vec** (`potion-base-8M`, ~30MB, default; `pip install
  "contextlake[kb-local]"`) and **fastembed** (ONNX `bge-small`; `[kb-fastembed]`), and the
  built-in wiki LLM runs a small `Qwen2.5-0.5B-Instruct` GGUF via `llama-cpp-python`
  (`[llm-local]`). Models auto-download once to `~/.contextlake/models` on first use (honoring
  `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` behind a TLS proxy) and load lazily. `doctor` reports model
  presence. A new guard refuses to mix embedder models/dimensions in one vector store.
- **Container image on GitHub Container Registry** (`ghcr.io/sayak-sarkar/contextlake`), published
  by the release workflow. It bundles the `[kb]` + built-in model extras and **pre-downloaded
  models**, so `docker run … contextlake bootstrap` works with zero config / offline.

## [2.1.4] - 2026-06-22

### Changed

- **`bootstrap`'s "knowledge layer not installed" message is now actionable.** It prints the exact
  Python interpreter in use and flags the common cause, running the bare `./contextlake.py`
  (system Python) while the `[kb]` extra was installed into a virtualenv, with the precise install
  command for that interpreter and the venv alternative (`./.venv/bin/contextlake bootstrap`).

## [2.1.3] - 2026-06-22

### Changed

- **Sync is far more resilient to flaky networks and moved branches.** `update` and `branches`
  now **retry transient proxy/network drops** (e.g. `unexpected eof`, `connection reset`) with
  backoff instead of failing on the first hiccup. Pulls are **fast-forward only**: a branch that
  has *diverged* from origin is reported as a clean `Diverged …, skipped (manual reconcile)`
  (the tool never merges or rebases, and git's multi-line "divergent branches" hint no longer
  leaks into the output), and a **deleted upstream branch** is reported as `Upstream branch
  deleted` instead of a fatal error. Net effect: transient blips self-heal, and the remaining
  "errors" are real and few.

## [2.1.2] - 2026-06-22

### Added

- The release workflow now also **publishes a GitHub Release** on each `vX.Y.Z` tag, with notes
  pulled from this changelog and the built sdist + wheel attached.

### Changed

- Adopt the SPDX **`license = "MIT"`** form (PEP 639) and drop the deprecated `License ::`
  classifier, silences the setuptools deprecation warnings emitted during the build. Building
  from source now needs `setuptools >= 77`.

## [2.1.1] - 2026-06-22

### Added

- **Maintainer release runbook** at `docs/releasing.md` (versioning → tag → build → publish to
  PyPI, with first-token and TLS-proxy troubleshooting) and a `release` extra
  (`pip install -e ".[release]"`) bundling `build` + `twine`.
- **Automated PyPI publishing** via `.github/workflows/release.yml`: pushing a `vX.Y.Z` tag
  verifies the tag matches the package version, runs lint + core tests, builds, and publishes
  using PyPI Trusted Publishing (OIDC), no stored API token.

## [2.1.0] - 2026-06-22

### Added

- **Cleaner terminal output: the timestamp moves to the right edge.** On an interactive
  terminal each line now shows the message on the left with a dim `HH:MM:SS` clock flushed
  to the right edge, re-flowed to the live terminal width and dropped automatically when a
  line is too long to fit (never wraps or misaligns). Alignment is ANSI- and wide-character
  aware, so it lines up uniformly across terminals. Piped/redirected output and the rotating
  **log file keep the full `[YYYY-MM-DD HH:MM:SS]` prefix** unchanged, so the audit trail is
  untouched.

### Changed

- **Branch name alone no longer causes an `update` to be skipped.** A repo with a clean
  working tree is now fetched and fast-forwarded on whatever branch it is checked out on, 
  feature branches included. The only thing that blocks an `update` is a *dirty working tree*
  (uncommitted/unstaged/untracked changes), which is still skipped (or stashed with
  `--auto-stash`). `protect_working_branches` now applies **only** to the `branches` command,
  where it keeps a repo from being switched off a non-safe branch. Previously a clean repo on
  any branch outside `safe_branches` was skipped outright.

## [2.0.1] - 2026-06-22

### Changed

- **Clearer config-not-found warning.** When `gitlab_group` is still the placeholder, the
  warning now lists the exact files searched (absolute paths, with `[found]`/`[absent]`) and
  notes that local `.contextlake.ini` is read from the **current directory**, so a config
  placed next to the example in the repo but run from elsewhere is no longer a silent miss.


## [2.0.0] - 2026-06-22

### Changed

- **Renamed the project `gitlab-sync` → `contextlake`.** The tool grew from a GitLab
  mirror into a local *context layer* for AI tools, and the name now reflects that. This
  is a rename only, no behavior changes.
  - The command, Python package, and PyPI project are now `contextlake`
    (`contextlake <command>`, `python -m contextlake`, `python3 contextlake.py`).
  - **A deprecated `gitlab-sync` command alias is kept** so existing installs and scripts
    keep working; it will be removed in a future major release.
  - **Existing config keeps working.** The former `~/.gitlab_sync.ini` / `.gitlab_sync.ini`
    (and the `[gitlab_sync]` section) and the `~/.gitlab-sync/` knowledge store are still
    read; new installs use `~/.contextlake.ini` and `~/.contextlake/`. An already-built
    index at `~/.gitlab-sync/kb` is reused as-is, no re-index needed.
  - The MCP server is now named `contextlake-kb`, and `steer` writes `contextlake` into the
    files it generates (`.mcp.json`, `AGENTS.md`, …).

### Note

- The GitHub repository and CI-badge URLs point at `.../contextlake`; they resolve once the
  repository is renamed on GitHub (the old URL auto-redirects).

## [1.18.1] - 2026-06-22

### Changed

- Confirmed the mascot's name, **Pebble** the otter, in `BRANDING.md` and the mascot spec.

## [1.18.0] - 2026-06-22

### Added

- **Brand identity, `contextlake`.** A `BRANDING.md` guide establishes the project's
  name, voice, color palette (cool lake teals + a warm "spark" of fresh context),
  open-source typography, logo, and otter mascot. Hand-authored SVG assets live in
  `docs/branding/` (`glyph.svg`, `wordmark.svg`) alongside a mascot spec (`mascot.md`).
  The name says what the tool does, a local lake of real context for your AI, and stays
  source-agnostic so the brand survives growth beyond GitLab. This is the brand kit only;
  the package/command rename is a separate, later step.

## [1.17.1] - 2026-06-22

### Changed

- The **genericity guard** (no-org-data check) now scans the whole published surface
, `docs/`, `examples/`, `.github/`, and every top-level doc, not just `src/` and a
  handful of root files, with a regression test pinning `docs/` coverage. (`tests/`
  stays excluded: the guard itself contains the denylist tokens by design.)

## [1.17.0] - 2026-06-22

### Changed

- **Documentation refactored for readability.** The README is now a lean ~180-line
  landing page (down from ~1,300); detailed command, configuration, branch-safety,
  and scheduling docs live in `docs/usage.md`, and the knowledge layer in
  `docs/knowledge-layer.md`. Standardized examples on the `gitlab-sync` command,
  clarified the `status` output (what "Missing"/"Extra" mean), and removed the
  repetitive install/security prose.

## [1.16.0] - 2026-06-22

### Added

- A **"Commands at a glance"** reference table in the README covering all 17
  commands, and `docs/internals.md`, a deep-dive on the core-sync internals plus a
  new **knowledge-layer architecture** section.

### Changed

- **Slimmed the README** (~1,320 → ~860 lines): the deep Technical Documentation /
  architecture moved into `docs/internals.md`, and the inline version history now
  points to `CHANGELOG.md`. Fleshed out the `lint` and `doctor` docs. (Docs only, 
  no code or layout changes, which already follow standard src-layout conventions.)

## [1.15.0] - 2026-06-22

### Added

- **QUICKSTART.md**, a short install → `bootstrap` → wire-your-editor guide.

### Changed

- `steer` now **enhances existing files instead of skipping them**: an existing
  `AGENTS.md` / `CLAUDE.md` / `.windsurfrules` / `.kiro/steering` keeps the user's
  content and gets a clearly-delimited managed block appended (only that block is
  refreshed on re-runs); `.mcp.json` is merged; a same-named skill file is kept;
  custom layers like `.devin/` are never touched. Nothing the user wrote is deleted.

## [1.14.0] - 2026-06-22

### Added

- **GitLab knowledge connector**: links each repo to its open merge requests and
  issues (read through the authenticated `glab`), on the same connector seam as
  Atlassian/Figma. Configure with `[[sources]] type = "gitlab"` (optional `group`);
  it needs no association rules. The command runner is injectable, so the mapping is
  unit-tested without GitLab.
- **Scheduling recipe**: `bootstrap` is incremental and branch-safe, so it doubles
  as a refresh job, documented cron + systemd-timer examples
  (`examples/gitlab-sync.service`, `examples/gitlab-sync.timer`) keep the mirror and
  knowledge layer always-fresh without disturbing in-progress work.

## [1.13.0] - 2026-06-22

### Added

- **Agent skills/workflows library**: `steer` now also installs a built-in, generic
  library of operating skills (investigate-root-cause, plan-before-coding,
  surgical-change, review-before-landing, ship-safely, use-knowledge-graph) into the
  workspace in the formats local tools read, Claude Code skills (`.claude/skills/`)
  and Windsurf workflows (`.windsurf/workflows/`), so even a small-context model has
  a strong operating playbook. Managed/idempotent like the other steering files.

## [1.12.0] - 2026-06-22

### Added

- **`bootstrap` command**, one-command turnkey setup that chains mirror →
  index → connect → embed → wiki → steer, skipping unconfigured/disabled stages and
  never aborting on a single stage's failure. Takes `--kb-config` (separate from the
  sync INI) and `--no-sync`/`--no-embed`/`--no-wiki`/`--no-connect` toggles, so a
  teammate goes from nothing to a fully-wired workspace in one step.

## [1.11.0] - 2026-06-22

### Added

- **Steering-layer generation** (`steer` command): writes workspace-specific
  steering files so local AI tools pick up the knowledge graph natively, 
  `AGENTS.md` (overview + knowledge tools + guardrails), a thin `CLAUDE.md` that
  imports it, `.windsurfrules`, `.kiro/steering/`, and a merged `.mcp.json` entry
  for the MCP server. Content is grounded in the indexed repos/languages/
  dependencies; it only overwrites files it manages (or with `--force`).

## [1.10.0] - 2026-06-21

### Added

- **OpenAI-compatible providers** for the embeddings and wiki tiers: set
  `provider = "openai"` to use any OpenAI-compatible API, a hosted key or a local
  server (LM Studio, Jan, llama.cpp, vLLM), as an alternative to local Ollama. The
  API key is read from an env var named by `api_key_env` (never stored in config);
  servers that need no key work with it unset.
- **MCP integration docs**: a README section showing how to use `gitlab-sync serve`
  as an MCP server from Claude Code and Windsurf/Devin (the graph tools need no
  model; only semantic search needs embeddings).

## [1.9.1] - 2026-06-21

### Fixed

- `serve` over the stdio transport wrote human-facing log lines to stdout, which is
  the MCP JSON-RPC channel, corrupting the protocol stream (clients saw spurious
  parse errors). On stdio, logs now go to stderr.

### Changed

- `index --workspace` is quieter by default: the per-repo "parsed/resolved" detail
  is now debug-level (show it with `-v`), leaving the clean per-repo progress bar.
- Added a `ROADMAP.md` listing future good-to-haves.

## [1.9.0] - 2026-06-21

### Added

- **Curated wiki tier** (`wiki` command): a pluggable, local-first LLM client
  (Ollama) synthesizes a provenance-stamped Markdown page per repo, grounded
  strictly in graph facts, and an **LLM verification council** (accuracy /
  completeness / clarity reviewers + a chairman threshold) gates what gets written.
  Off unless `[llm] enabled = true`.
- **`index --watch`** (`--interval`): keep re-indexing the workspace incrementally
  on an interval (Ctrl-C to stop) for a long-running refresh.
- **Bi-temporal queries**: each indexed shard is snapshotted by commit, and
  `query --repo R --as-of <commit>` searches repo `R` as it was at a previously
  indexed commit (time-travel) without a schema overhaul.

## [1.8.0] - 2026-06-21

### Added

- **Incremental workspace indexing**: `index --workspace` now re-indexes only the
  repos whose git HEAD moved since their last index (skipping unchanged ones), with
  `--force` to rebuild everything. Paired with cron this gives scheduled
  incremental refresh.
- **`lint` command** for the knowledge layer: reports graph-health issues, repos
  gone stale (HEAD moved since index) and dangling edges (an endpoint node missing
  from the store).
- **Colorful CLI**: status glyphs, coloured per-repo lines, and a progress bar for
  the sync and knowledge-layer commands. Honors `NO_COLOR`/`FORCE_COLOR` and falls
  back to plain text off a TTY (pipes, cron, and logs stay clean). No new
  dependencies.

## [1.7.0] - 2026-06-21

### Added

- **Hybrid retrieval** (`hybrid_search` MCP tool): seeds Personalized PageRank with
  the embedding hits and propagates relevance across the graph (HippoRAG-style), so
  structurally-related nodes (callers, dependents) surface even when their text does
  not match the query. PPR runs over a BFS-bounded subgraph to stay tractable.
- **Optional sqlite-vec ANN backend** for the vector store, selectable via
  `[embeddings] vector_backend` (`auto` | `sqlite-vec` | `brute`). `auto` uses
  sqlite-vec when the `gitlab-sync[kb-vec]` extra is installed and falls back to the
  exact pure-Python cosine scan otherwise, same interface either way.

## [1.6.0] - 2026-06-21

### Added

- **Semantic-search tier (optional, local-first)**: a pluggable embeddings
  provider (`Embedder` interface + config-driven factory; a stdlib-only Ollama
  provider ships first), a local SQLite-backed vector store with cosine search, an
  `embed` command that vectorizes indexed nodes, and a `semantic_search` MCP tool
  exposed by `serve` when embeddings are enabled. Off by default; `doctor` reports
  embeddings status.

## [1.5.0] - 2026-06-21

### Added

- **Figma knowledge connector**: links repos to the design files they reference,
  classifying `figma.com` URLs (file/design/proto/board) to a stable file key and
  taking the human file name from the URL slug. When a Figma MCP is configured each
  design is additionally checked for reachability (best-effort, never required).
  Runs alongside Atlassian sources under `connect`. Connector-agnostic helpers were
  extracted to a shared module so new connectors stay small.

### Fixed

- `link_scrape` association rules expressed as a `patterns` list (as in the example
  config) were silently ignored; both a singular `pattern` and a `patterns` list
  are now honored.

## [1.4.0] - 2026-06-21

Adds an optional **knowledge layer** (`gitlab_sync.kb`, the `[kb]` extra,
Python ≥ 3.10) that turns the mirrored repositories into a queryable knowledge
graph served to AI agents over MCP. The core sync tool is unchanged and the extra
is entirely opt-in. Everything is generic and config-driven, no
organization-specific data lives in the package.

### Added

- **Knowledge-graph store and CLI**: `index`, `query`, `serve`, and `doctor`
  commands backed by a SQLite + FTS5 cross-repo index with per-repo JSON shards.
  Every node/edge is provenance-stamped (source file + verified date) and
  confidence-tagged (`EXTRACTED` / `INFERRED` / `AMBIGUOUS`).
- **Code graph** via tree-sitter for Python, JavaScript, TypeScript/TSX, and C#:
  files, classes, functions/methods, interfaces, imports, containment, and an
  intra-repo **call graph** (the parser registry is pluggable). `index --workspace`
  indexes every git repository under a directory.
- **Cross-repo dependency graph** from `pyproject.toml`, `package.json`, and
  `*.csproj` manifests through shared package nodes.
- **MCP server** (stdio or streamable-http) exposing `search_code`,
  `find_definition`, `find_callers`, `find_dependents`, `get_neighbors`,
  `shortest_path`, and `graph_stats`, plus a `kb://stats` resource. All output is
  sanitized before it reaches an agent.
- **Knowledge connectors** (`connect`): an **Atlassian** connector links each repo
  to the Jira issues and Confluence pages it references. Candidate issue keys
  (from branch/commit names) are confirmed and enriched against live sites with a
  single batched JQL call (unverified false-positives are dropped); Atlassian URLs
  in docs are classified into issue/page links. One or more sites are supported,
  each independently authenticated over MCP. Output is stored in an isolated graph
  partition so code re-indexing never disturbs external links.
- **Config** (`examples/kb.toml.example` → `~/.gitlab-sync/kb.toml`): store
  location, languages, knowledge sources, and association rules, all
  organization-specific facts live here, never in the package.
- CI now runs a separate knowledge-layer job (Python 3.10-3.13) alongside the
  core job, including a genericity guard that fails the build if organization data
  appears in the source.

## [1.3.0] - 2026-06-21

This release stabilizes the core and makes the tool installable. It repairs
several regressions introduced by the earlier modularization and fixes a
critical configuration bug.

### Fixed

- **Critical:** repositories were keyed by their full `<group>/...` path while
  local clones mirror the tree *below* the group, so every repo was misreported
  as missing-and-extra and a sync would clone duplicates into a bogus `<group>/`
  subtree. Paths are now mapped to their group-relative local form (the full
  path is retained for `glab` authentication).
- **Critical:** a `~` (or `$VAR`) in a config-file `work_dir`/`cache_dir` was
  treated literally, so the tool operated on a non-existent path and saw zero
  local repositories. Path values are now expanded.
- **Critical:** boolean config settings (`protect_working_branches`,
  `require_clean_workspace`, `clean_corrupted`, `adaptive_workers`,
  `auto_stash`) were silently overridden by CLI defaults on every run, which
  disabled branch protection and the clean-workspace requirement by default.
  Flags now default to "unset" so config-file values are honoured.
- `--config` was accepted but ignored; the explicit config path is now loaded.
- Config precedence corrected to: explicit `--config` > local > global > defaults
  (previously global silently overrode local).
- `AdaptiveWorkerPool` raised `AttributeError` / never actually resized the pool;
  it now initializes correctly and parallelism adapts to the live error rate.
- Retry/backoff existed but was never wired in; clone now retries transient
  failures (network/timeout) and fails fast on DNS/TLS.
- `update` reported failed `git pull` (conflicts, auth, network) as
  "Already up to date"; it now distinguishes updated / unchanged / error by
  comparing HEAD before and after.
- `load` silently discarded a list-shaped JSON cache; it is now normalized.
- `fetch` used a malformed `glab` invocation; it now calls the GitLab API with a
  URL-encoded group path and correct pagination, and restores the
  `path|ssh|http|default_branch|archived` text cache.
- `verify` recovers nested-repository (repo-inside-repo) detection.
- Corrupted (non-git) target directories are detected and re-cloned again
  (honouring `--clean-corrupted`); cloning prefers `glab` for authentication.

### Added

- Installable package with a `gitlab-sync` console entry point, `python -m
  gitlab_sync`, and the bare `python3 gitlab_sync.py` script (src layout).
- `--dry-run` to preview clone/update/branch actions without changing anything.
- Logging via the standard library with `-v/--verbose`, `-q/--quiet`, and
  `--log-file` (rotating audit log).
- `clone_method` (auto|glab|git) and `branch_strategy` (commits|recency|hybrid)
  configuration; the most-active-branch heuristic is now recency-aware.
- `--version` flag.
- A pytest test suite (68 tests) with fakes for `git`/`glab`, and GitHub Actions
  CI running ruff + pytest on Python 3.9-3.14.

### Changed

- Code modularized into a `gitlab_sync` package: `cli.py`, `core.py`,
  `config.py`, `safety.py`, `logging_setup.py`.

## [1.2.0] - 2026-06-16

### Added

- Branch safety checks to protect working branches from sync conflicts
- Workspace protection requiring clean workspace before operations
- Automatic stashing support for uncommitted changes
- Configurable safe branches list
- CLI arguments for branch safety control:
  - --protect-working-branches / --no-protect-working-branches
  - --safe-branches
  - --require-clean-workspace / --no-require-clean-workspace
  - --auto-stash / --no-auto-stash
- Enhanced error classification for better retry strategies
- Adaptive worker pool for dynamic parallelism
- Comprehensive branch safety documentation in README

### Changed

- Updated README with branch safety section including scenarios and examples

## [1.1.0] - 2026-05-24

### Added

- INI-based configuration file support
- Local and global config file support
- CLI arguments now override config file settings
- Improved security with externalized configuration
- Tilde expansion for home directory paths
- Configurable timeouts and worker counts
- Exponential backoff retry mechanism
- Adaptive worker pool for dynamic parallelism
- Enhanced error classification for better retry strategies

### Changed

- Removed all hardcoded company/personal identifiers
- Configuration files can be excluded from version control

## [1.0.0] - 2026-05-10

### Added

- Full synchronization pipeline
- Branch management with automatic active branch detection
- Structure verification
- Concurrent processing with ThreadPoolExecutor
- Error handling and timeout management
- Timestamped logging
