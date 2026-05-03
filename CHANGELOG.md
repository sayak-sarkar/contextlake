# Changelog

All notable changes to contextlake will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **Genericity guard hardened — the leak-detector no longer leaks.** The org-token denylist used to be
  hardcoded in the test file (shipping real org identifiers in the published package). It now lives
  **outside the repo** — supplied via the `CONTEXTLAKE_GENERICITY_DENYLIST` env var or a git-ignored
  `.genericity-denylist` file (CI uses a secret) — so no real token is ever committed. The scan also
  now covers **every git-tracked file** (not a fixed list), and an always-on structural check rejects
  any non-allowlisted email address even when no denylist is configured.
- **Removed deployment-scale figures from docs.** Genericized specific fleet counts (the example
  `status` output, the overview-feature notes) to illustrative values, so nothing in the published repo
  is tied to any particular deployment's repository count.
- **Test-locked the offline boundary (INV-2).** A new test blocks all outbound sockets and asserts the
  core commands (`index`/`query`/`graph`/`lint`/`embed`) still run, while `connect` degrades rather than
  fails — proving contextlake is safe in air-gapped/egress-restricted environments, with enrichment the
  single opt-in online step. Documented in `docs/storage.md`.

### Added

- **`eval` now scores any retriever and reports a cost dimension.** Retrievers are built by factories
  (`make_fts_retriever` / `make_semantic_retriever` / `make_hybrid_retriever`) that close over their
  deps, so semantic and hybrid are scorable — not just FTS (the old fixed call site couldn't pass a
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

- **`[kb-full]` one-step install for local semantic search** — `pip install "contextlake[kb-full]"`
  pulls the knowledge layer + the built-in CPU embedder (`kb-local`) + the sqlite-vec ANN backend
  (`kb-vec`) together, so `index → embed → semantic search` just works with no Ollama and no API key.
- **Repo nodes show their primary language — the fleet's tech stack at a glance.** In the overview,
  each repo node now carries a lettermark (`PY`, `JS`, `TS`, `C#`, …) for its dominant language (a
  single GROUP-BY over data the parser already records), so an architecture map reads its stack
  without clicking in. Trademark-free white-on-navy lettermarks, inlined offline; unknown languages
  keep the generic repo glyph.
- **Architectural edges are now labelled — flows read like a C4 diagram.** Dependency / flow edges
  (`depends_on`, `calls_http`, `exposes`, `flow`, `publishes`, `publishes_event`, `consumes_event`)
  carry an autorotated label of the relation plus its context where meaningful (`depends_on · requests`,
  `calls_http · /v1/orders`, the event topic). Structural edges (`calls`/`contains`/`imports`) stay
  unlabelled so the hundreds of them don't bury the diagram in text.
- **Graph nodes now carry type glyphs — the first step toward architecture diagrams.** Every node is
  painted with a Lucide-style icon for its kind (file, class, function, package, repo, HTTP endpoint,
  event topic, …) so a graph reads by *type* at a glance instead of by colour alone. Glyphs are inlined
  as percent-encoded SVG `data:` URIs (no CDN, no sprite fetch — the page stays a single offline file),
  and each glyph's stroke colour is chosen per node fill at build time (white on the dark `repo` node,
  dark on the light `module` node) so it never washes out. Flow nodes (`endpoint`/`topic`) joined the
  palette + legend.
- **`--site` now renders the LLM-wiki as cross-linked pages.** Each repo with a generated wiki gets a
  `wiki-<slug>.html` (the index links it, the page links back to the graph), rendered by a tiny
  dependency-free Markdown→HTML converter (HTML-escaped — the wiki is untrusted LLM output), carrying
  the same fresh/stale badge as `get_wiki`. Stays fully offline, zero new deps.
- **MCP: `get_wiki(repo)` — serve the LLM-wiki to agents (with a staleness signal).** The generated
  wiki was written to `<store>/wiki/` but read by nothing; now an agent can fetch a repo's wiki prose
  (sanitised Markdown), explicitly labelled **advisory** (verify against cited sources; never outranks
  EXTRACTED facts) and carrying **`stale`** — true when the wiki's `head_commit` differs from the
  repo's current indexed head, so prose describing changed code is never cited as current.
- **MCP: `blast_radius(node_id, hops)` — "what could break if I change this".** Bounded transitive
  *reverse* reach over incoming `calls` + `depends_on` edges (configurable), breadth-first, capped by
  `hops` and `limit`. Each hit carries its hop distance, the relation, and confidence (EXTRACTED-first,
  `truncated` when capped) — an impact slice for agents, made correct by the AMBIGUOUS-edge change
  below so the hottest symbols aren't missed.

### Changed

- **`embed`'s "disabled" message is now actionable.** Instead of the dead-end "Embeddings are
  disabled", it names the exact next step — install `contextlake[kb-full]` (when the embedder is
  missing) and/or set `[embeddings] enabled = true` — and notes the one-time ~30 MB model download,
  so the post-`bootstrap` "Build semantic vectors" stage no longer silently goes nowhere.
- **Documented and test-locked the no-pollution invariant (INV-1).** `docs/storage.md` now states that
  every generated artifact lives under the store (`~/.contextlake/kb` by default) and never inside a
  synced repo working tree, and `tests/kb/test_no_repo_pollution.py` enforces it by driving the
  generating commands over a temp two-repo mirror and asserting each repo tree is byte-identical.
- **`doctor` now probes ANN (sqlite-vec) availability.** When embeddings are enabled it reports whether
  the native sqlite-vec KNN index actually loads in this environment, or whether semantic search will
  fall back to brute-force cosine — so the silent fallback (a known offline/corporate-env failure mode)
  is visible *before* you embed, not after.
- **Wiki generation is now incremental (skip-if-unchanged).** `contextlake wiki` skips the (expensive)
  LLM call for any repo whose existing page was already generated from its current head commit — so a
  no-op fleet re-run drops from O(repos × LLM calls) to ~0. `--force` regenerates regardless; the
  summary reports how many were skipped.
- **Ambiguous calls are no longer silently dropped.** When a call name resolves to 2–6 candidate
  definitions, indexing now emits an `AMBIGUOUS` `calls` edge to each candidate (de-duplicated,
  self-calls excluded) instead of discarding the call — so the hottest symbols aren't lost and
  blast-radius isn't undercounted. Names matching more than the cap are too generic to be signal and
  are still skipped. AMBIGUOUS edges render dotted in the visualizer.

### Added

- **`contextlake eval --golden FILE.json` — a retrieval-quality harness.** Score a labelled
  `query → expected-nodes` set against the index and get **precision@k / recall@k / MRR / hit-rate**
  (aggregate + per-query), over any retriever (FTS today; semantic/hybrid pluggable). Makes retrieval
  changes (embed-bodies, reranking, a future `ask` router) *falsifiable* instead of vibes. Stdlib-only;
  the golden set is plain JSON, `match` by node id or name.
- **Event/messaging flow extraction (Kafka/MSK, SNS, EventBridge).** Indexing now detects, per file,
  the message topics a repo **publishes** to and **consumes** from (literal topics in Kafka
  producer/`@KafkaListener`/`subscribe`, EventBridge `DetailType`, SNS), as `INFERRED` edges to a
  shared `topic` node. A two-hop join (`publishes_event ⨝ consumes_event`) yields directional
  `publisher --flow--> consumer` repo edges — the direction an event travels — shown in the fleet
  overview alongside HTTP `flow` and structural `depends_on`. High-precision (literal topics only);
  config-variable topics are an honest undercount, never a false link. Re-run `index` to populate.

## [2.4.0] - 2026-06-25

### Added

- **MCP: repo-level architecture tools `repo_dependencies` / `repo_flow`.** Surface the cross-repo
  wedge to AI agents: `repo_dependencies(repo, direction)` returns the package two-hop
  (`dependent → publisher`, weighted), `repo_flow(repo, direction)` returns the HTTP endpoint two-hop
  (`caller → exposer`, weighted) — both INFERRED, weight-ranked, with "undercount, verify" guidance.
  Previously these edges fed only the visualizer.
- **`contextlake graph --site DIR` — a cross-linked offline graph site.** Emits `index.html` +
  `overview.html` + one `repo-<slug>.html` per repo with a parsed graph, sharing a single
  `cytoscape.min.js` / `app.css` / `app.js` (referenced, not inlined, so the folder stays small).
  Overview repo nodes link to their repo page (and the inspector gains an "Open this repo's graph →"
  button); every page has an Index/Overview nav. Fully offline. Scope it with **`--repos PATTERN`**
  (comma-separated glob/substring) to build pages for only a subset of repos.
- **`contextlake graph --overview --serve` now serves the whole site live**, rendering each repo page
  **on demand** from the store instead of materialising the fleet up front — so online serving never
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
  unbounded list — so a hub node can't silently blow up an agent's context, and a clipped result
  announces itself.
- **Generated graphs now default to a dedicated `<store>/graphs/` directory** instead of the current
  working directory — `graph` HTML output and `--site` land next to the knowledge base, not wherever
  the command happened to run. Pass `--output` / `--site DIR` to override.

## [2.3.0] - 2026-06-24

### Added

- **Two interlocking overview views — a `Namespace` mindmap and a `Dependencies` graph.** The fleet
  overview now has a mode toggle over one graph. **Namespace** (default) collapses the whole repo fleet
  into its top-level GitLab namespaces (sized by repo count), with aggregated, weight-labelled
  namespace→namespace dependency edges; tapping a namespace expands its repos in place as a compact
  mindmap branch (the rest dims to spotlight it) and tapping again collapses — every repo stays placed
  and searchable. **Dependencies** lays the connected repos out as readable hub-and-spoke clusters.
  Both modes share selection, search, and the inspector.
- **Inspector lists a node's relationships**, each neighbour clickable to navigate to it (in-view
  hop-to-hop). Tapping a node/edge reframes the canvas onto the selection so it stays legible.

### Changed

- **Graph visualizer reworked into an enterprise app shell.** The floating translucent cards are
  replaced by a real layout — a top bar (brand, mode, search), a collapsible left sidebar (view
  controls + Nodes/Relationships legends with live counts), the graph filling the centre, a slide-in
  right inspector, and a status bar — on a CSS grid with a tokenised design system. Adds a **dark
  mode** (Deepwater theme; re-skins the canvas, not just the chrome), icon-button controls, empty/
  loading states, keyboard shortcuts (`/` search, `f` fit, `t` theme, `Esc` clear), and focus-visible
  rings. Still one self-contained offline HTML, zero new dependencies.
- **Fleet overview now shows real cross-repo dependencies.** Repointed from the raw cross-repo
  `imports` join (≈4,800 import-star artifacts from fleet-wide `module` nodes) to the **package
  two-hop** (`publishes ⨝ depends_on`) — 217 trustworthy, manifest-derived `depends_on` edges, marked
  `INFERRED` (a deliberate, honest undercount). Repos are labelled by short name (the full path moves
  to the inspector + search) so nodes are distinguishable.
- **Graph-visualizer CSS/JS extracted into `static/app.css` + `static/app.js`** (inlined at emit time
  like the vendored cytoscape), so the source is lint/`node --check`-able. Output is still one
  self-contained offline HTML.

### Fixed

- **Truncation is now visible in the UI.** A bounded subgraph that was clipped used to read as
  complete; a persistent status-bar banner now says "showing N of M — truncated" (honest counts only).
- **Overview readability.** Isolated/no-dependency repos — typically the bulk of a large fleet — no
  longer scatter the connected map into an unreadable speck — they're hidden by default behind a toggle
  (and revealed by search),
  and the layout frames the meaningful core. Expanding a namespace no longer triggers a disorienting
  global re-layout (scoped, position-stable).
- Canvas now reflows/reframes correctly when the inspector or sidebar opens (was leaving the old
  zoom/pan). Dark-mode faded opacity and `prefers-reduced-motion` gating for JS animations.

## [2.2.0] - 2026-06-23

### Added

- **Post-sync repo audit (`contextlake audit`, also auto-runs after `sync`/`bootstrap`).** Scans every
  local clone and reports which repos are effectively empty — **empty** (no commits / no files),
  **readme-only** (just a template README), or **boilerplate** (only meta files like LICENSE/.gitignore)
  — plus age/activity: each repo's **creation date** (GitLab `created_at`, captured during fetch; falls
  back to the first git commit) and **last commit date** (from the local clone). Prints an aggregate
  summary (counts, oldest/newest, how many stale >1y/>2y, repos with no commits) and writes a full
  per-repo report as JSON + CSV (`--report PATH`, default `<cache_dir>/repo_audit.json`). The scan is
  parallel, read-only, and works offline; `--no-audit` skips the automatic run. Zero new dependencies.

- **`contextlake graph` — visualize the knowledge graph.** Extracts a *bounded* subgraph (the full
  graph is far too large to draw) and renders it to an interactive, **offline-first** HTML page
  (vendored cytoscape.js, inlined — no network needed; `--cdn` for a small online file), or to
  `dot` / `mermaid` / `json`. Seed from a symbol (`--node`/`--name`+`--kind`/`--search`), a single
  repo (`--repo`), or the whole fleet (`--overview` = repos-as-nodes with aggregated cross-repo
  edges — the architecture map). Scoping knobs `--hops` / `--max-nodes` / `--max-fanout` /
  `--relation` / `--direction` keep hub nodes from exploding (truncation is always logged). The HTML
  is a full mini-explorer: nodes coloured by kind and sized by degree; edge labels hidden until a node
  is selected; **clickable edges with an inspector** (relation, a confidence trust indicator, the
  source `file:line` provenance with copy, context and weight) — edges are coloured by relation,
  styled by confidence, and sized by weight, with a **relationship legend that filters by relation**;
  a node **search** box, a **detail panel** (kind / repo / qualified-name / file:line),
  a clickable **legend that filters by kind**, hover tooltips, a **switchable layout**
  (`cose`/`concentric`/`breadthfirst`/`circle`/`grid`, default via `--layout`), and a toolbar
  (fit / reset / **save-PNG**) — all wrapped in the **contextlake brand** (inlined lake glyph,
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
  entire, ever-growing global FTS table — so persisting a repo cost O(repo_nodes × total_store_nodes)
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
  default) — derived noise that bloats the graph and slows legacy monorepos. Both are reported (no
  silent gaps) and tunable via `[kb] skip_generated` / `[kb] max_file_bytes`. The source the
  generated files derive from is still indexed, so there's no knowledge loss. On a real 3,230-file
  legacy repo this dropped ~26% of files / 4k generated nodes (22.5s → 16.6s).

## [2.1.5] - 2026-06-23

### Added

- **Built-in, zero-config CPU models for the knowledge base — no Ollama and no API key.**
  The embeddings and wiki tiers now accept `provider = "auto"` (the new default), which uses a
  reachable local Ollama, else an in-process **built-in** model, else skips. The built-in embedder
  ships two engines — **model2vec** (`potion-base-8M`, ~30MB, default; `pip install
  "contextlake[kb-local]"`) and **fastembed** (ONNX `bge-small`; `[kb-fastembed]`) — and the
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
  Python interpreter in use and flags the common cause — running the bare `./contextlake.py`
  (system Python) while the `[kb]` extra was installed into a virtualenv — with the precise install
  command for that interpreter and the venv alternative (`./.venv/bin/contextlake bootstrap`).

## [2.1.3] - 2026-06-22

### Changed

- **Sync is far more resilient to flaky networks and moved branches.** `update` and `branches`
  now **retry transient proxy/network drops** (e.g. `unexpected eof`, `connection reset`) with
  backoff instead of failing on the first hiccup. Pulls are **fast-forward only**: a branch that
  has *diverged* from origin is reported as a clean `Diverged … — skipped (manual reconcile)`
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
  classifier — silences the setuptools deprecation warnings emitted during the build. Building
  from source now needs `setuptools >= 77`.

## [2.1.1] - 2026-06-22

### Added

- **Maintainer release runbook** at `docs/releasing.md` (versioning → tag → build → publish to
  PyPI, with first-token and TLS-proxy troubleshooting) and a `release` extra
  (`pip install -e ".[release]"`) bundling `build` + `twine`.
- **Automated PyPI publishing** via `.github/workflows/release.yml`: pushing a `vX.Y.Z` tag
  verifies the tag matches the package version, runs lint + core tests, builds, and publishes
  using PyPI Trusted Publishing (OIDC) — no stored API token.

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
  working tree is now fetched and fast-forwarded on whatever branch it is checked out on —
  feature branches included. The only thing that blocks an `update` is a *dirty working tree*
  (uncommitted/unstaged/untracked changes), which is still skipped (or stashed with
  `--auto-stash`). `protect_working_branches` now applies **only** to the `branches` command,
  where it keeps a repo from being switched off a non-safe branch. Previously a clean repo on
  any branch outside `safe_branches` was skipped outright.

## [2.0.1] - 2026-06-22

### Changed

- **Clearer config-not-found warning.** When `gitlab_group` is still the placeholder, the
  warning now lists the exact files searched (absolute paths, with `[found]`/`[absent]`) and
  notes that local `.contextlake.ini` is read from the **current directory** — so a config
  placed next to the example in the repo but run from elsewhere is no longer a silent miss.


## [2.0.0] - 2026-06-22

### Changed

- **Renamed the project `gitlab-sync` → `contextlake`.** The tool grew from a GitLab
  mirror into a local *context layer* for AI tools, and the name now reflects that. This
  is a rename only — no behavior changes.
  - The command, Python package, and PyPI project are now `contextlake`
    (`contextlake <command>`, `python -m contextlake`, `python3 contextlake.py`).
  - **A deprecated `gitlab-sync` command alias is kept** so existing installs and scripts
    keep working; it will be removed in a future major release.
  - **Existing config keeps working.** The former `~/.gitlab_sync.ini` / `.gitlab_sync.ini`
    (and the `[gitlab_sync]` section) and the `~/.gitlab-sync/` knowledge store are still
    read; new installs use `~/.contextlake.ini` and `~/.contextlake/`. An already-built
    index at `~/.gitlab-sync/kb` is reused as-is — no re-index needed.
  - The MCP server is now named `contextlake-kb`, and `steer` writes `contextlake` into the
    files it generates (`.mcp.json`, `AGENTS.md`, …).

### Note

- The GitHub repository and CI-badge URLs point at `.../contextlake`; they resolve once the
  repository is renamed on GitHub (the old URL auto-redirects).

## [1.18.1] - 2026-06-22

### Changed

- Confirmed the mascot's name — **Pebble** the otter — in `BRANDING.md` and the mascot spec.

## [1.18.0] - 2026-06-22

### Added

- **Brand identity — `contextlake`.** A `BRANDING.md` guide establishes the project's
  name, voice, color palette (cool lake teals + a warm "spark" of fresh context),
  open-source typography, logo, and otter mascot. Hand-authored SVG assets live in
  `docs/branding/` (`glyph.svg`, `wordmark.svg`) alongside a mascot spec (`mascot.md`).
  The name says what the tool does — a local lake of real context for your AI — and stays
  source-agnostic so the brand survives growth beyond GitLab. This is the brand kit only;
  the package/command rename is a separate, later step.

## [1.17.1] - 2026-06-22

### Changed

- The **genericity guard** (no-org-data check) now scans the whole published surface
  — `docs/`, `examples/`, `.github/`, and every top-level doc — not just `src/` and a
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
  commands, and `docs/internals.md` — a deep-dive on the core-sync internals plus a
  new **knowledge-layer architecture** section.

### Changed

- **Slimmed the README** (~1,320 → ~860 lines): the deep Technical Documentation /
  architecture moved into `docs/internals.md`, and the inline version history now
  points to `CHANGELOG.md`. Fleshed out the `lint` and `doctor` docs. (Docs only —
  no code or layout changes, which already follow standard src-layout conventions.)

## [1.15.0] - 2026-06-22

### Added

- **QUICKSTART.md** — a short install → `bootstrap` → wire-your-editor guide.

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
  as a refresh job — documented cron + systemd-timer examples
  (`examples/gitlab-sync.service`, `examples/gitlab-sync.timer`) keep the mirror and
  knowledge layer always-fresh without disturbing in-progress work.

## [1.13.0] - 2026-06-22

### Added

- **Agent skills/workflows library**: `steer` now also installs a built-in, generic
  library of operating skills (investigate-root-cause, plan-before-coding,
  surgical-change, review-before-landing, ship-safely, use-knowledge-graph) into the
  workspace in the formats local tools read — Claude Code skills (`.claude/skills/`)
  and Windsurf workflows (`.windsurf/workflows/`) — so even a small-context model has
  a strong operating playbook. Managed/idempotent like the other steering files.

## [1.12.0] - 2026-06-22

### Added

- **`bootstrap` command** — one-command turnkey setup that chains mirror →
  index → connect → embed → wiki → steer, skipping unconfigured/disabled stages and
  never aborting on a single stage's failure. Takes `--kb-config` (separate from the
  sync INI) and `--no-sync`/`--no-embed`/`--no-wiki`/`--no-connect` toggles, so a
  teammate goes from nothing to a fully-wired workspace in one step.

## [1.11.0] - 2026-06-22

### Added

- **Steering-layer generation** (`steer` command): writes workspace-specific
  steering files so local AI tools pick up the knowledge graph natively —
  `AGENTS.md` (overview + knowledge tools + guardrails), a thin `CLAUDE.md` that
  imports it, `.windsurfrules`, `.kiro/steering/`, and a merged `.mcp.json` entry
  for the MCP server. Content is grounded in the indexed repos/languages/
  dependencies; it only overwrites files it manages (or with `--force`).

## [1.10.0] - 2026-06-21

### Added

- **OpenAI-compatible providers** for the embeddings and wiki tiers: set
  `provider = "openai"` to use any OpenAI-compatible API — a hosted key or a local
  server (LM Studio, Jan, llama.cpp, vLLM) — as an alternative to local Ollama. The
  API key is read from an env var named by `api_key_env` (never stored in config);
  servers that need no key work with it unset.
- **MCP integration docs**: a README section showing how to use `gitlab-sync serve`
  as an MCP server from Claude Code and Windsurf/Devin (the graph tools need no
  model; only semantic search needs embeddings).

## [1.9.1] - 2026-06-21

### Fixed

- `serve` over the stdio transport wrote human-facing log lines to stdout, which is
  the MCP JSON-RPC channel — corrupting the protocol stream (clients saw spurious
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
- **`lint` command** for the knowledge layer: reports graph-health issues — repos
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
  exact pure-Python cosine scan otherwise — same interface either way.

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
is entirely opt-in. Everything is generic and config-driven — no
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
  location, languages, knowledge sources, and association rules — all
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
