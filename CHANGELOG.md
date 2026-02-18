# Changelog

All notable changes to gitlab_sync will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
