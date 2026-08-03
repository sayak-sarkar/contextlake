# Changelog

All notable changes to contextlake will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **A config file found by directory search can no longer make contextlake execute a program.**
  `.contextlake.kb.toml` is discovered by walking up from the current directory, so a repository you
  cloned could ship one setting `[llm] provider = "cli"` + `command`/`args` — handed straight to
  `subprocess.run` by the next `kb wiki`, `kb enrich`, or `dashboard --llm-chat`. The same hole existed
  in `[[sources]]`, whose `command`/`args`/`mcp_command` spawn an MCP server over stdio. Those keys are
  now honoured only from `~/.contextlake/kb.toml` or an explicit `--config` path; from a discovered file
  they are dropped with a warning naming the file and the key. Nothing else is distrusted — `store_dir`,
  `languages`, `max_file_bytes`, `[embeddings]`, `[[rules]]`, and non-`cli` LLM providers keep working
  from a project-local file exactly as before, so directory-scoped config is unaffected. Passing
  `--config` on that same file still honours it: naming the file is the explicit act the gate asks for.

### Added
- `CONTEXTLAKE_NO_LOCAL_CONFIG=1` skips ancestor config discovery entirely, for both
  `.contextlake.ini` and `.contextlake.kb.toml` — only the global file and an explicit `--config` are
  read. Intended for CI, containers, and anywhere untrusted checkouts are handled in bulk, where opting
  out of the whole tier is simpler to reason about than the per-key gate above.
- CI now enforces a coverage floor (`--cov-fail-under=88`) on the full-suite job, so a silent
  drop from the current 92% can no longer pass green. The floor is deliberately only on that one
  job: putting it in `pyproject`'s `addopts` would make every narrow `pytest -k ...` run fail on
  its own partial number, and the core job measures the whole package while skipping `tests/kb`,
  so its honest total is ~23% and no shared floor can fit both.
- Supply-chain scanning: a `security` workflow running `pip-audit` over the full dependency
  surface (dev plus every `kb` extra), a `ruff --select S` security-lint pass, and CodeQL for
  Python, on push to main, on pull requests, and weekly so newly-disclosed CVEs surface against an
  untouched tree. The `--select S` pass is non-blocking for now: it reports ~159 real findings plus
  ~3,336 `assert`-in-tests hits that are pytest idiom rather than defects, and triaging that
  backlog is its own piece of work. Deliberately kept out of `pyproject.toml`'s ruff `select` and
  out of `ci.yml` so it stays additive rather than a new gate on every commit.
- Dependabot for `pip` and `github-actions`, weekly, with minor/patch grouped per ecosystem so a
  routine tree-sitter point release does not open a dozen PRs. Major bumps stay ungrouped.
- Published container images now carry SLSA build provenance and an SPDX SBOM, and are signed
  keyless with cosign via the workflow's OIDC identity (no key material anywhere). Signing is by
  digest rather than tag, so the signature is pinned to exactly what was built.
- `--exit-zero-on-partial` on every mirror command (and `bootstrap`), for anyone whose scripts
  depend on the old always-zero exit status: the run still reports what failed, it just exits 0.

### Changed
- `index_repo_dir` is decomposed into a file walker, a parser registry and a ref collector; it was
  the most complex function in the codebase and sits on the critical path of every index run. Shard
  output is unchanged, proven by a new golden-shard test that also passes against the pre-change
  code, so it is a genuine regression check rather than a snapshot of the new behaviour.
- **Breaking:** a mirror run that had failures now exits 1. `mirror fetch`/`clone`/`update`/
  `branches`/`verify`/`sync` (and `bootstrap`'s mirror stage) exited 0 no matter how much of the
  fleet failed — `mirror sync` reported success with a ✓ even when every single clone failed. Nothing
  unattended could tell a healthy mirror from a dead one: the cron wrapper in `docs/usage.md` tests
  `$?` and so never fired, and the `Type=oneshot` systemd unit in `examples/` was always recorded as
  succeeding, leaving `systemctl is-failed` and any `OnFailure=` hook with nothing to fire on. Each
  stage now returns its own ok/failed/skipped counts, `sync` exits on the total across all stages,
  and the sync finale is a ⚠ rather than a ✓ when anything failed.

  What counts as a failure is exactly what each stage already logged as an error — no repo is
  reclassified. Skipped work (already up to date, protected branch, dry run) is never a failure;
  neither is a `verify` that reports repos missing or extra (only a cloned path with no `.git`,
  which is corruption). `fetch` fails on 0 projects only when no `--repos`/`repo_filter` is in
  play, since 0 matches for a narrow pattern is a legitimate answer.

  **Migrating:** if a script or CI job relies on a mirror command always exiting 0, add
  `--exit-zero-on-partial`. If it already checks the exit status, it starts working as intended —
  expect jobs to go red that were silently failing before.

## [4.0.0] - 2026-08-04

**Migrating from v3.0.0:** replace `contextlake init --yes` / `-y` with `contextlake init
--skip-interactive` -- it is a rename, not a new option; the old flags no longer parse.

### Changed
- **Breaking:** `contextlake init`'s non-interactive flag is now `--skip-interactive`; `--yes`/`-y`
  are gone, not aliased. Unlike apt/npm/gh's `--yes`, which only skips a single yes/no confirmation
  whose answer carries no new information, `init`'s flag drives a whole value-collecting wizard by
  substituting defaults for prompts that mostly aren't yes/no questions at all (platform, group,
  work_dir, store_dir all take a typed value) -- `--yes` misdescribed what it did. No deprecation
  window: same hard-cutover approach as the CLI namespacing change below, since there are no
  external users of the flag yet to accommodate.

### Fixed
- `contextlake init` no longer writes a config naming a group/org/workspace that doesn't exist.
  Previously, `init --yes` (now `--skip-interactive`) without `--group` silently wrote
  `gitlab_group = your-org`, and the stale-placeholder safety net never caught it because it
  checked for a different literal (`your-gitlab-group`). The same placeholder was also reachable
  interactively by accepting the suggested default. `group` now has no default at all: an empty
  value (either path) is refused with a clear message and exit code 2, before any file is written.
- The mirror `.contextlake.ini` side of `--config` now hard-fails when the given path doesn't
  exist, matching `kb.toml`'s existing behavior -- previously it silently fell through to the next
  config in the precedence chain (typically `~/.contextlake.ini`), which can point at a completely
  different workspace than the one you meant to use. `ConfigError` now lives in `config.py`
  (re-exported from `kb/config.py` for compatibility).

## [3.0.0] - 2026-08-03

### Added
- **Preview (opt-in, pending visual approval):** the graph page's layout dropdown gains a
  `dagre (preview)` option -- a layered/directed dagre layout that also renders nodes as real
  HTML cards (border-radius, shadow, real typography) instead of canvas circles, and marches
  ants along the selected node's edges. Selecting any other layout leaves the existing canvas
  rendering completely unchanged; this is a look to judge before it becomes anyone's default.
  Card rendering is skipped above 400 nodes (the status bar says so) and on the fleet overview.
- The graph page can now be saved as **SVG** as well as PNG -- a new `SVG` button beside the
  existing `PNG` one. The PNG button is unchanged (still cytoscape's own canvas render), and it
  keeps working while the `dagre (preview)` card rendering is on: the capture temporarily reverts
  the cards to canvas nodes and restores them afterwards. Expect that PNG to look sparse in card
  mode -- it is the classic circles-and-glyphs picture at the wider spacing dagre laid out for
  cards. The SVG is the format that keeps the card look: it embeds each card as real HTML in a
  `foreignObject`, which browsers render but Inkscape/Illustrator ignore. Hand-rolled, no new
  vendored library.
- Vendored `cytoscape-dagre` 4.0.0 and `cytoscape-dom-node` 2.1.0 (both MIT, ~46 KB + ~11 KB)
  alongside `cytoscape.min.js`, so the preview above works offline like the rest of the page.
  `cytoscape-dagre` bundles dagre itself, so there is no separate dagre file. `app.js`
  feature-detects both and drops the preview option if they did not load. They load on every
  graph page (a ~57 KB inline cost, or one shared sibling file each in a `--site` build);
  neither does anything until the preview layout is selected.
- `contextlake kb serve` now accepts `--transport sse`, the legacy HTTP+SSE transport, alongside
  the existing `stdio`/`http` (Streamable HTTP) options -- for MCP clients that only support SSE
  and haven't moved to Streamable HTTP yet. See [docs/serve.md](docs/serve.md#transports).
- Ingested documents now link to the code symbols they mention by name, via a new
  `kb ingest --for-repo <repo>` flag (per-source equivalent: `for_repo` on a `[[sources]]` entry)
  that says which indexed repo the documents are about. Without it, ingest behaves exactly as
  before and links nothing.
- Enrichment results (`kb enrich`) now link to the code symbols they mention by name, instead of
  being stored as isolated document nodes with no edges at all.
- Generated wiki pages (whole-repo and per-subsystem) now link each section to the code symbols it
  names, closing the last of the four zero-edge pipelines the audit found. Only the symbols are
  linked, not the repo as a whole: a repo's external-knowledge links (Jira / Confluence / Figma /
  GitLab) stay free of contextlake's own generated pages.
- GitLab merge requests now link directly to the code files their diff touches (not just their
  repo), via a new `fetch_changes`/`match_files_to_nodes` pair. The edge is `touched_by`, read
  code-first (`pay.py -> MR #42`) like the `designed_in` / `discussed_in` / `documented_by` edges
  beside it.
- Figma designs now link directly to code symbols whose name matches a frame or component name,
  when Figma metadata is available (MCP-configured).
- `connect` now discovers Figma and Slack links in docs by default (built-in URL patterns) -- no
  `[[rules]] type="link_scrape"` config required, matching how GitLab sources were already default-on.
- New shared text-mention matcher (`connectors/text_match.py`), reused by Slack and by
  ingested/enriched/wiki content to find which code symbols a piece of text is actually about.
- Slack connector can now fetch channel message text (previously it only ever parsed Slack links
  found in docs) -- lays the groundwork for linking discussions to the code they're about.
- Slack channels now link to specific code symbols mentioned in their message text, using the same
  text-mention matcher shared with ingest/enrich/wiki content.
- Connectors can now link external content directly to the code it's about via a new shared
  `link_to_code` primitive (the existing repo-level edge is kept alongside it, except for wiki pages).
- The Slack MCP tool used to read a channel's messages is configurable as `history_tool` on a
  `[[sources]]` entry (default `conversations_history`), alongside the existing `verify_tool`. Slack
  MCP servers don't agree on a tool name, so without this a non-default server silently produced no
  message links at all.
- **A public, read-only live demo of the dashboard is now linked from the project homepage and
  docs footer.** It's the existing `contextlake kb dashboard --site DIR --sample` static export
  (bundled fictional "acme" fleet, no real data) generated into `site/demo/` by `site/deploy.sh`
  on every deploy, no new tooling.
- **The wiki council can now review with a different (stronger) model than the one generating the
  pages**, via two new `[llm]` keys: `review_provider` and `review_model`. Until now a single client
  served both roles, so a local-only setup had the tiny built-in 0.5B grading its own drafts — a
  near-constant rubber-stamp. Setting `provider = "builtin"` + `review_provider = "anthropic"` keeps
  generation local and free while a real model decides what actually gets published; the inverse
  split (generate strong, review cheap) works too, since `review_provider` wins unconditionally. The
  reviewer's `model`, `api_key_env` and `base_url` are re-resolved for the review provider rather
  than inherited from the generator. Left unset — the default — the council reviews with the
  generating client exactly as before. Strictly opt-in and never inferred from an API key that
  happens to be in the environment: it costs **pages × `council_size`** extra calls against the
  review provider (drop `council_size` to 1 to cut that threefold). The run banner names both models
  when they differ, and `contextlake doctor` still checks the generation provider only.
- Connector-produced nodes (GitLab MRs/issues, Figma designs, Slack channels) are now embedded and
  semantically searchable, closing the third leg of the consolidation gap (unified in keyword
  search, now-linked in the graph, now embeddable). Each `connect` pass sweeps the repo's old
  connector vectors first, so an MR that closes or a design that's unlinked doesn't leave an
  orphaned embedding behind.
- `graph` exports (GraphML/Cypher/DOT/Mermaid) now include linked external nodes (GitLab MRs, Figma
  designs, Slack channels, wiki page sections) one hop out from the code they're linked to, not just
  code, so the edges the earlier consolidation work now creates actually show up in an export instead
  of being silently dropped.

### Changed
- Vendored cytoscape.js bumped 3.30.2 -> 3.34.0 (bugfix/feature releases within 3.x; the graph
  page's existing default rendering is unaffected). `--cdn` now pins the same version.
- **BREAKING: the commands are now namespaced under `mirror` and `kb`.** The CLI had grown to 29
  flat top-level commands doing two unrelated jobs — mirroring git repositories, and building and
  serving the knowledge layer over them — and `--help` had stopped being navigable. Each verb now
  lives under the noun it belongs to: `contextlake mirror fetch|clone|update|branches|verify|status|
  sync|audit`, and `contextlake kb index|source|connect|embed|ingest|enrich|wiki|lint|eval|query|
  graph|owners|impact|dashboard|serve|steer|hook`. `kb` is the word already user-visible in
  `kb.toml`, the `contextlake[kb]` install extra, and the knowledge-layer package. **Five commands
  did not move:** `init` and `bootstrap` span both tiers (bootstrap runs mirror + index + connect +
  embed + enrich + wiki + steer, and ships to users as a systemd `ExecStart`), `version` and
  `completion` belong to neither, and `doctor` is the diagnostic you reach for when nothing else
  works. The `who-knows` and `blast-radius` aliases survive under `kb`. Shell tab-completion needs
  no re-registration — it reads the live parser. **This is a hard cutover with no compatibility
  window:** the old flat spellings do not parse at all. `contextlake fetch` fails as an ordinary
  unknown command, and the existing suggester answers it with `Did you mean: mirror fetch?` — the
  same treatment any other unknown command gets, not a special case.
- **Two post-upgrade steps, both required.** contextlake wrote the old flat forms into files it does
  not revisit, and there is no grace period covering them: re-run **`contextlake kb hook install`**
  (or `--workspace DIR`) so already-installed post-commit hooks are rewritten — otherwise
  re-indexing stops with no visible error — and **`contextlake kb steer --force`** so `.mcp.json`
  and `AGENTS.md` point at `contextlake kb serve`. Both rewrite their managed block in place, and
  `hook install` detects and replaces a block still carrying the old syntax.
- Every command string contextlake generates now uses the namespaced form: the post-commit hook
  `kb hook install` writes (`contextlake kb index`), the `.mcp.json` / `.vscode/mcp.json` entry and
  the AGENTS.md / CLAUDE.md / windsurfrules / Kiro bodies `kb steer` writes (`contextlake kb serve`),
  the dashboard's own subprocess spawns, and the usage/next-step hints across the knowledge commands.
  The dashboard UI's own copy-paste commands moved too: every empty/unavailable state's suggested
  command, the Wiki tab's "Generate wiki" snippet, and the MCP card's `--transport http` example.
  So did `init`'s next-step lines, the mirror commands' "narrow to just the failures" retry hints,
  and the "port already in use" / "`[llm]` isn't enabled" notices `kb dashboard --serve` prints.

### Removed
- **The old flat command spellings (`contextlake fetch`, `contextlake index`, …).** They are gone
  outright, along with the deprecation notice and its `CONTEXTLAKE_NO_DEPRECATION` opt-out — the
  namespaced form is the only one that parses. See the two required post-upgrade steps above.

### Fixed
- **`contextlake kb graph --layout dagre` was unreachable from the CLI.** The renderer has
  supported `dagre` since the preview landed, but `cli.py` restates the layout names as two
  hard-coded `argparse` `choices` lists instead of importing the renderer's `LAYOUTS` tuple, and
  neither was updated -- so the preview was only reachable from the in-page dropdown. Both lists
  now include `dagre`.
- **Neither the dashboard's Links panel nor the `get_repo_links` MCP tool showed a repo's
  GitLab-diff or Slack cross-links.** Both listed only `tracked_by` (Jira), `documented_by`
  (Confluence), `designed_in` (Figma), `has_merge_request` and `has_issue`, so the newer
  `touched_by` (a merge request whose diff touches the repo's code), `discussed_in` (a Slack
  channel whose history mentions its symbols) and `referenced_in` (a Slack channel linked from
  its docs) edges reached the graph and were invisible on both surfaces. All eight relations now
  come from one shared list, so a connector adding a relation lights up both doors or neither.
- **A `repo=`-scoped semantic/hybrid search missed a repo's own linked connector/enrichment
  content.** `VectorStore.search`/`SqliteVecStore.search` filtered `repo_id` by exact match only, so
  a query scoped to `repo="team/api"` never matched rows written under the `@connect:team/api` /
  `@enrich:team/api` partitions that `connect`/`enrich` deliberately isolate on write (see
  `connect_partition`/`enrich_partition`) -- even though that content is directly linked to the
  repo's own code via real graph edges. Both `search()` implementations now widen a repo filter to
  match the literal repo id or either of its connector/enrichment partitions.
- **`contextlake kb serve --transport http` printed a bind URL that 404s.** It reported the bare
  `http://127.0.0.1:8765`, but Streamable HTTP is served at the SDK's `streamable_http_path`
  (`/mcp`), which contextlake does not override, so a client pointed at the printed URL got a 404
  and the root looked like a dead server. The line now prints `http://127.0.0.1:8765/mcp`, matching
  the `/sse` path already reported for `--transport sse`. `steer`-generated MCP config is
  unaffected: it wires the `stdio` transport by command, never by URL.
- **A bare `contextlake hook` exited 2 with `invalid choice: '==SUPPRESS=='` on Python 3.9–3.11**
  instead of defaulting to `install` as its own `--help` promises. The optional `action` positional
  paired `choices=` with contextlake's SUPPRESS-default convention — the same argparse trap already
  documented on the `completion` positional, where argparse validates the SUPPRESS sentinel itself
  against `choices` when the positional is omitted. `cmd_hook()` already rejects an unknown action
  with a clearer message, so no validation was lost.
- **`[llm] provider = "anthropic"` (or `"openai"`) with no explicit `base_url` sent its API calls to
  the local Ollama port instead of the real API endpoint.** `LlmCfg.base_url` was a declared field
  defaulting to `http://127.0.0.1:11434`, so that one literal won for every provider and the
  per-provider fallbacks in `build_llm` were dead code. `base_url` now defaults to `None` and is
  resolved per provider at read time (`llm.base.default_base_url`, mirroring `default_api_key_env`
  and its rationale): `anthropic` → `https://api.anthropic.com`, `openai` →
  `https://api.openai.com/v1`, `ollama`/`auto` → the local daemon. An explicitly configured
  `base_url` still wins, so proxies and local openai-compatible servers are unaffected.
- **Dashboard repo-detail requests on a large repo re-parsed and re-aggregated the entire shard from
  scratch on every single request**, with no caching of any kind. `read_shard` now keeps a small
  in-memory cache of the parsed shard (validated on every read against the file's own mtime/size, so
  a re-index — same process or a separate `contextlake kb index` run while `dashboard --serve` stays up
  — is still picked up correctly), and `repo_brief`'s degree/hubs/dispatchers/top-symbols aggregation
  over every node and edge is cached the same way. The shard cache is bounded by *estimated resident
  bytes*, not entry count: a parsed shard's pydantic objects measured at roughly 13x their on-disk
  JSON size, so an entry-count cap alone would have risked pinning dozens of large repos' shards in
  memory on the multi-hundred-repo fleets this targets. Measured against a synthetic
  54k-node/261k-edge shard (75 MB on disk), a warm repeat request against an unchanged repo dropped
  from ~2.5s to ~0.25s with no added memory growth on further repeats; the first, cold-cache request
  is unchanged. Both caches are correct under a rewrite this process makes itself: `write_shard`
  drops its entries in each, so a re-index at an unchanged commit that happens to emit a same-length
  shard within one filesystem mtime tick cannot serve a stale aggregation under a fresh `head`. And
  `repo_brief` observes the shard file's on-disk identity exactly once per call (previously twice,
  one independent `stat()` each for the shard-parse cache and the aggregation cache) so a rewrite
  landing between those two observations can't pair mismatched halves either.
- **A `languages` filter listing only `"c"` no longer silently drops all `.h` files.** `.h` files are
  parsed with the `cpp` grammar internally, so a `["c"]`-only filter previously excluded them entirely.
  `.h` inclusion is now decided independently of that internal parsing choice: it is indexed whenever
  either `"c"` or `"cpp"` is enabled, since C/C++ headers are shared infrastructure. The old workaround
  of listing both languages is no longer necessary (docs updated accordingly).
- **The graph visualizer's `repo_subgraph(path_prefix=...)` no longer matches a sibling directory
  that merely shares a string prefix** (e.g. `path_prefix="api"` incorrectly also matched `apiv2/`).
  It now requires a path-boundary match — the file equals `path_prefix` or starts with `path_prefix`
  plus `/` — the same fix already applied to the wiki's `repo_brief`.
- **An existing store now picks up the "overview names its subsystem pages" feature without a
  `--force` regeneration.** The freshness check asked one question — is the commit unchanged? — and
  skipped the page before the subsystem-naming field was ever consulted, so a repo already wiki'd at
  its current commit kept an overview page that said nothing about the subsystem pages sitting
  beside it, indefinitely. A wiki page's footer now records which subsystem pages it names, and the
  check asks the two questions separately: a page is skipped only when its commit is unchanged AND
  it already names the subsystems this run would name. A page that names none (every non-federated
  repo, and every page written before this existed) records none and is still skipped, so there is
  no fleet-wide regeneration.
- **The per-run cap on subsystem wiki pages no longer permanently strands the tail of a very large
  federated repo.** A repo with more qualifying modules than the cap (20) gave pages to its 20
  largest and never reached the rest: a later run with the same head commit re-picked the identical
  top 20 and freshness-skipped every one of them, and even a new commit re-picked a top 20 rather
  than the stranded tail. Module slots now go to never-yet-paged modules first (each group keeping
  the existing largest-first order), so repeated `wiki` runs walk the whole repo while any single
  run stays bounded by the cap. The whole-repo overview page names every module that already has a
  page or is getting one this run — so the named set accumulates run over run instead of tracking
  only the current slice — and the truncation log line now says how many modules were deferred to a
  later run instead of claiming they were skipped outright.
- **Subsystem wiki pages for modules that no longer qualify are now pruned instead of living
  forever.** A module that shrank below the module floor, or a repo whose tree was restructured (or
  that stopped qualifying as federated at all, orphaning every one of its module pages), left its
  page, its `@wiki:{repo}::{module}` partition, that partition's shard and its embeddings behind
  permanently — `--force` didn't remove them either, since it only regenerates what qualifies today.
  Every `wiki` run now removes all four for a module that is no longer in the qualifying set; it
  costs one indexed key-range lookup per repo and no LLM call, so it isn't gated behind `--force`.
  Pruning is skipped when the empty module list came from the index not answering (a large repo
  whose index rows are missing or mid-rebuild) rather than from the repo actually changing shape —
  the shard and the index are separate layers, and only the second reading is evidence.
- **A failed whole-repo wiki page no longer drags every one of that repo's subsystem pages through
  the same failure.** The whole-repo page and its module pages share one LLM and one council, so an
  unreachable backend cost up to 21 round trips per federated repo before anything was reported.
  The run now skips that repo's module pages and moves on as soon as its whole-repo page fails.
- **Each wiki page is now built from one `repo_brief`, not two.** `cmd_wiki` needs the brief itself
  for the council's review prompt, and `generate_page` then built a second, identical one
  internally. The parts of a brief that sit outside its cached shard aggregation are real I/O — the
  README read, the recursive legacy-build-tooling walk of the live checkout, the enrichment-shard
  read — so that was a duplicated filesystem pass per page, up to 21 of them for one federated repo
  in a single run. `generate_page` now accepts a caller-built `brief` and reuses it.
- **The wiki footer's grounding-coverage ratio is now comparable between a repo's overview page and
  its subsystem pages.** The whole-repo denominator counted every node — including file-less ones
  (import targets, packages, endpoints, topics) that a module-scoped page structurally cannot
  contain — so identical grounding depth read as systematically worse on the overview. Both halves
  of the ratio now count file-backed symbols only, and the footer names the unit ("Grounded in N/M
  **file-backed** symbols") so it can't be read against the prompt's own all-nodes symbol count.
- **A file-less `#include`/import-target pseudo-node is no longer guaranteed a slot in a whole-repo
  page's top-symbols/hubs/dispatchers lists.** The per-kind grounding floor exists so a real but
  structurally low-degree kind (a SQL table) isn't squeezed out by degree ranking; it was also
  treating `kind="module"` nodes with no file of their own — one per `#include`d name — as a kind
  deserving that guarantee, putting a row like "module widget.h (?)" in every C/C++ repo's lists.
  They remain eligible by ordinary degree ranking, so a heavily-included header still ranks in on
  merit. Other file-less kinds keep their floor slot.
- A council rejection now reports how many reviewers **abstained** (`N reviewer(s) returned nothing
  parseable`) alongside the score. A reviewer that returns nothing — a missing API key, a review CLI
  not on PATH (`CliLlm` returns `""` on non-zero exit rather than raising) — abstains on every lens
  and so rejects every page at score 0.0, which was previously indistinguishable from a strict but
  working council.
- `release.yml` and `binaries.yml` (both tag-triggered) no longer run independently of `ci.yml`'s
  full Python 3.9-3.14 test matrix. A new `verify-ci` job in each checks that `ci.yml` actually
  completed successfully for the tagged commit before building/publishing anything, and fails the
  release outright if it didn't. This closes the gap that let v2.62.0 ship with a red `ci.yml` run
  live for a full release cycle.

## [2.67.0] - 2026-07-31

### Added
- **Large, genuinely federated repos now get one wiki page per subsystem, in addition to the
  whole-repo page.** `repo_brief()` gained a `path_prefix` parameter that scopes its grounding
  (symbols, files, dependencies) to one module/subsystem instead of the whole repo, matched on a
  segment boundary — scoping to a module named `api` never also pulls in a sibling like `apiv2/`,
  since the match requires the prefix to be the whole path or followed by a `/`. `contextlake wiki`
  runs this automatically, no new flag: a repo qualifies once it has at least 5,000 graph nodes AND
  is genuinely federated (no single top-level module owns more than 60% of them) — a single large
  repo with one dominant source directory still gets just its one whole-repo page, same as before.
  Generation is capped at the 20 largest qualifying modules per run (deterministic largest-first,
  ties broken lexicographically) so one `wiki` invocation on a very large legacy repo stays bounded;
  each page's title, prompt framing, and provenance footer all say plainly that it covers only that
  module, never the repository as a whole. Module pages live under `wiki/_modules/` and embed into
  their own `@wiki:{repo}::{module}` partition (attributed to the real repo id) for `ask`/semantic
  search. **Known limitation:** the 20-page cap is not a rotating window — a repo with far more than
  20 qualifying modules will only ever get pages for its 20 largest; the rest stay unwritten across
  runs until a future improvement lets a run prefer never-yet-paged modules when filling slots.
- **The whole-repo overview page now names its subsystem pages instead of trying to summarize
  them.** When a repo has qualifying subsystems (above), the overview's Architecture section
  explicitly lists and briefly describes each one and points to its own dedicated page, rather than
  attempting to compress every subsystem's internals into a single section — which got thinner and
  less grounded the more subsystems a repo actually had. **This only takes effect the next time the
  overview page is actually regenerated:** a repo already wiki'd at its current commit has its
  overview skipped as unchanged (subsystem pages still generate fresh regardless), so upgrading to
  this release doesn't retroactively add naming to an existing overview page — that happens on the
  repo's next commit change, or a `--force` run (the dashboard Regenerate button's force option
  works too). **Known limitation:** the named-subsystems
  list is fixed before subsystem generation runs, so a subsystem that then fails council review or
  comes back empty (a shard/index mismatch) is still named in the overview as having its own page.
  A one-off failure self-heals the next time that subsystem's page is generated successfully, but a
  *persistent* failure does not: once the overview page's own indexed commit stops changing, its
  freshness check skips regenerating it, freezing the stale claim indefinitely while the named
  subsystem keeps being retried (and keeps failing) on every run.
- **Dashboard: the Wiki tab gained a subsystem picker.** A repo with generated subsystem pages now
  shows a "Subsystem:" dropdown above the wiki content, letting you switch between the whole-repo
  overview and any subsystem's own page without leaving the tab or re-fetching the rest of the
  repo's detail panel. The picker only ever lists subsystems that actually have a page written to
  disk (checked against the real file on disk, not just "this subsystem qualified for one"), so a
  subsystem beyond the 20-page cap, or one whose generation failed, is never offered as a dead
  option. New route: `GET /api/repo/<id>/wiki?module=<prefix>`.

## [2.66.0] - 2026-07-31

### Added
- **Wiki grounding sample size now scales with repo size instead of a flat cap of 15.** The
  number of symbols sampled into `top_symbols`/`hubs`/`dispatchers` is now
  `max(15, min(80, node_count // 1500))`. This only changes behavior once a repo passes about
  24,000 graph nodes (below that, the formula still floors to the same 15 as before); the sample
  size then grows with repo size up to a cap of 80, reached at around 120,000 nodes.
- **`top_symbols` now reserves at least one slot per distinct symbol kind.** Previously a pure
  degree-rank cutoff could squeeze out a structurally low-degree kind entirely (e.g. a SQL table
  node, which has no in/out call edges) once the sample cap filled up with higher-degree
  function/method nodes. The zero-degree backfill is applied only to `top_symbols`, which carries
  no numeric claim about a symbol; `hubs`/`dispatchers` reuse the same per-kind-floor helper but
  only ever reorder candidates that already have a real (nonzero) caller/callee count -- they never
  fabricate a "0 caller(s)" row for a kind with no signal.
- **Wiki pages' provenance footer now states a coverage-ratio fact.** It appends
  "Grounded in N/M symbols (X%)", where N is the number of distinct symbols appearing across
  `top_symbols`/`hubs`/`dispatchers` combined and M is the repo's total node count, letting a
  reader judge how much of the repo's surface the grounding sample actually covers.
- **`setup_signals` gained per-category counting for legacy C/C++ project/workspace files**
  (`.vcxproj`, `.vcproj`, `.dsp`/`.dsw`, `.pbxproj`, `.cdtproject`), so a large legacy repo is
  summarized as a count (e.g. "3 legacy MSVC6 project (.dsp) file(s) detected") rather than listed
  file-by-file. None of these extensions are part of the parsed/indexed language set, so they never
  become graph nodes -- the count instead comes from a recursive, bounded scan of the repo's live
  checkout (the same `store`-given, degrade-to-nothing path the existing config-file detection
  uses for `package.json`/`Dockerfile`), pruning the same vendored/build-output directories the
  indexer itself skips. The scan stops after 200,000 files visited, so a huge legacy monorepo can't
  turn an uncached, per-request `repo_brief` call into an unbounded walk. Any match already present
  in the graph's node set is merged in without double-counting.
- **`repo_brief` gained a `generated_paths_detected` flag** so the wiki prompt can warn the model
  off treating derived build output as hand-authored design. It fires for an indexed file living
  under a directory literally named `generated/` (e.g. `src/generated/widgets.py`); it also checks
  the parser's own generated-filename convention (e.g. `Form1.designer.cs`), but a file matching
  that convention is, by default, already excluded from indexing before it can reach the graph --
  so that half of the check only has an effect when a repo's `[kb] skip_generated = false`.

### Fixed
- **`contextlake graph --repo <repo>` (and the dashboard's repo diagram) now truncates to
  `--max-nodes` by degree rank, not by an arbitrary `node_id` order.** When a repo's node count
  exceeds the cap, the surviving nodes are now the highest-degree ones (ties broken by `node_id`)
  instead of whichever nodes happened to sort first by id, so the most connected/important part of
  the graph is what a truncated view keeps.
- **The wiki's Gotchas section now states only the caller-count fact, not a characterization of
  why.** The prompt still tells the model each symbol's caller count and that it's therefore worth
  extra care/tests when changed, but no longer lets it describe *why* a symbol has many callers --
  wording like "foundational", "core", or "critical infrastructure" is explicitly disallowed, since
  the caller count is the only fact actually given, not an explanation of the symbol's role.

## [2.65.0] - 2026-07-31

### Fixed
- **C++: out-of-line qualified method definitions (`Widget::Draw`, `App::Gadget::Spin`) are no
  longer lost or misfiled.** A method body defined outside its class, at any qualification depth
  (single- or multi-level `::` chains), is now captured with its fully-qualified name and resolved
  repo-wide back to the class it belongs to as a `method`, instead of either vanishing or being
  recorded as a bare, file-contained `function`. Along the way, a real forward-declaration
  ambiguity was fixed: a class/struct that's only forward-declared (no body) no longer produces a
  spurious node that a same-named real definition could be silently confused with.
- **C++/C: `.h` header files were mapped to the C language, not C++.** A class declared in a
  header and defined in a matching `.cpp` was invisible to the graph -- this affects any codebase
  with a conventional header/source split, which is most C++ code. Headers are now mapped to C++,
  matching or improving extraction on 196 of 200 sampled real-world headers; the remaining 4 trace
  to a separate, pre-existing gap in how template full-specializations are handled, not a
  regression from this change. **Note:** a `languages` config that lists `c` without also listing
  `cpp` will no longer index `.h` files at all, since they're now classified as `cpp` -- add `cpp`
  to your `languages` list if you rely on header-declared definitions. (Follow-up to admit `.h`
  under either language tracked in the project's backlog; not fixed in this release.)
- **C++: `#ifdef`/`#else` duplicate definitions no longer cause spurious ambiguous call
  resolution.** The same function or method defined once per preprocessor branch (a common
  portability pattern) is now de-duplicated only when the two definitions are genuinely the same
  symbol in different branches of the *same* `#ifdef`/`#else` (or `#ifndef`/`#else`) conditional --
  not merely behind *some* conditional anywhere in the file, and never for a bare `#ifndef` include
  guard with no `#else` (the single most common header pattern of all, deliberately excluded: a
  guard alone has only one branch, so there's nothing to collapse). A widened signature comparison
  (parameter types read from the AST, not just parameter names) keeps genuinely distinct
  overloads on either side of a branch as separate definitions.

### Added
- **Namespace blocks now participate in C++ containment.** A `namespace App { ... }` block is a
  real containing node in the graph, the same way a class or file already is, instead of its
  members appearing to float file-level.
- **`contextlake doctor` flags C/C++ shards indexed with an older parser version.** Each indexed
  shard now records the parser version it was built with; `doctor` compares that against the
  current version and calls out any C/C++ shard that predates the qualified-method, namespace, and
  `#ifdef`-dedup fixes above, so it's obvious which repos need a re-index to pick them up (an
  advisory check -- it doesn't fail the overall `doctor` exit code).

## [2.64.0] - 2026-07-30

### Added
- **Wiki: richer per-repo template.** Pages gain two new sections, each only written
  when the graph actually grounds it (never an empty heading): **Setup & Run** (from
  a README excerpt read off the repo's live checkout, plus which conventional
  entry-point/config files are present -- `package.json`, `Dockerfile`,
  `pyproject.toml`, `manage.py`, etc.) and **Gotchas** (the most-depended-on symbols,
  reframed as a "treat changes here with extra care" signal -- reuses the hubs data
  already computed, no new extraction). Section order is now fixed:
  Overview, Setup & Run, Architecture, Dependencies, Gotchas, Decisions, External
  context. `repo_brief()` gained an optional `store` param (only used for the README
  read; omit it and the field is simply `None` -- degrades the same way
  `dashboard.data._readme_html` already does for a missing/moved checkout).
  Anonymized `--site` exports drop the README excerpt, same rule as the existing
  README/wiki-body exclusion.
- **Wiki: same richer template for cluster/namespace pages.** `wiki/cluster.py`'s
  cluster prompt gets the same fixed-order, nothing-invented treatment, including a
  "Gotchas" section grounded in a real coupling-risk signal: the highest-weight
  internal edges (busiest cross-repo coupling) and the member repos with the most
  boundary edges (widest external blast radius) -- both read directly off data the
  cluster brief already computes.
- **Dashboard: the Wiki tab no longer gates content behind a "Reveal wiki" click.**
  The page renders directly; the one thing the old gate carried that mattered (the
  `stale` flag) is now a persistent badge next to the heading instead of being hidden
  behind the same click.
- **Dashboard: live "Regenerate wiki" action, single-repo and fleet-wide.** With
  `--allow-mutations`, a repo's Wiki tab gets a scoped regenerate button and the
  Settings tab gets a fleet-wide one. Both show a real pre-flight estimate ("N of M
  repos will regenerate, the rest are already up to date") before confirming, with a
  Force option to bypass the freshness check (the estimate updates to make that cost
  explicit first). Modeled on the existing MCP-server start/stop lifecycle
  (non-blocking subprocess + pidfile), not the blocking sync/add-repo pattern -- an
  LLM-backed run has no safe fixed timeout. Spawns the real `contextlake wiki` CLI
  unmodified, so there's no duplicated generation logic; the dashboard just tails its
  log and polls whether it's still running.

## [2.63.0] - 2026-07-30

### Added
- **Dashboard: recursive module drill-down for oversized repos.** A repo whose diagrams
  tab used to dead-end at one still-too-large module (auto-scope to the largest
  top-level directory, still truncated, no way further down) now keeps narrowing into
  that module's own largest child, one level at a time, until the view fits or there's
  genuinely nowhere further to go. A breadcrumb trail shows the path taken; any earlier
  crumb widens back out, and a "narrow further" control lets you explore a sibling.
  `kb/visualize/payload.py`'s `repo_modules()` gained a `within` param to enumerate one
  level below an already-scoped prefix (the underlying `path_prefix` matching already
  supported arbitrary depth; only the enumerator was ever depth-1-only) -- additive,
  no change to existing single-level callers.
- **Dashboard: Hotspots section on the Anatomy tab.** The existing combined-degree "Top
  symbols" ranking is now also split into **hubs** (most depended-on -- worth
  protecting with tests) and **dispatchers** (widest fan-out -- where behavior
  branches), each its own ranked table. No new extraction: `repo_brief()` already
  computed this centrality data at index time; it's now split by direction instead of
  only combined.
- **Dashboard: Path tab.** "How does A reach B" as a single numbered route, not a
  diagram -- the existing `shortest_path` MCP tool's BFS finally has a dashboard UI.
  Accepts a bare symbol name (same id/name/fuzzy resolution and ambiguous-across-repos
  handling the Blast radius view already has) or a node id.

## [2.62.1] - 2026-07-30

### Fixed
- **`contextlake completion` crashed on Python 3.9-3.11** with `argparse.ArgumentError: argument
  shell: invalid choice: '==SUPPRESS=='` when run without an explicit shell argument. Root cause:
  the `shell` positional combined `nargs="?"` + `choices=[...]` + a `SUPPRESS` default (this
  project's standard subparser-default convention) -- older argparse (fixed by 3.12) validates the
  `SUPPRESS` sentinel itself against `choices` when the positional is omitted. Fixed by validating
  the shell value in `cmd_completion()` instead (matching `init`'s existing `--platform` pattern),
  not via argparse `choices=`. Missed in v2.62.0's own release gate (`release.yml`/`binaries.yml`,
  both green) because that gate doesn't run the full Python version matrix -- only `ci.yml` does,
  and it wasn't checked after tagging. Reproduced and confirmed fixed against real Python 3.9 and
  3.10 (not just the maintainer's own, newer, unaffected interpreter).

## [2.62.0] - 2026-07-30

### Added
- **Shell tab-completion now registers itself automatically**, the first time any command runs in a
  real interactive terminal -- no `init` run required first. A `pip install`/`uv tool install`/
  `pipx install` has no post-install hook to do this at install time (a deliberate Python packaging
  limitation, not a gap here), so this is the closest achievable equivalent: a one-time,
  TTY-gated check, logged plainly before it writes anything, that never re-fires and never
  overrides an explicit `contextlake init --no-completion` decline (tracked by a new
  `~/.contextlake/.completion_setup_done` marker). Opt out of the check entirely with
  `CONTEXTLAKE_NO_AUTO_COMPLETION=1`.
- **`contextlake completion [bash|zsh|fish]`**: register tab-completion on demand, for the current
  shell or an explicit override, without waiting for the automatic first-run check or a full
  `init`. Works without the `[kb]` extra (shell completion isn't a knowledge-layer concern).

## [2.61.0] - 2026-07-30

### Added
- **`contextlake --help` now groups all 29 commands by task** (Get started / Mirror a fleet / Build
  the knowledge graph / Explore & search / Serve to editors) instead of one flat, un-grouped list --
  built from an advisor-reviewed CLI-wide audit after direct feedback that the CLI "is not very easy
  or intuitive to guess without reading the manual." Descriptions are pulled live from each
  subcommand's own help text (no separate copy to drift out of sync) and wrapped to the terminal
  width, correctly re-indented under the description column.
- **`contextlake <mirror-command> --help-advanced`** (on `fetch`/`clone`/`update`/`branches`/
  `verify`/`status`/`sync`/`audit`) reveals the ~14 resilience/tuning flags (`--max-retries`,
  `--backoff-initial`/`--backoff-max`, `--adaptive-workers`, `--protect-working-branches`,
  `--safe-branches`, `--require-clean-workspace`, `--auto-stash`, and their `--no-` counterparts) that
  default `--help` now keeps out of the listing -- every one already has a `.contextlake.ini`
  equivalent, so hiding them by default removes ~60% of the visible flag surface on 8 commands for
  zero functional cost. The flags themselves are unchanged and still fully documented in
  [Mirror repositories](docs/usage.md).
- `Examples:` epilogs added to `fetch`/`clone`/`update`/`branches`/`verify`/`status`/`audit`'s own
  `--help`, matching the worked examples every other command already carried.

This is an additive-only pass: no command, flag, or alias was renamed, removed, or re-nested --
everything documented in `docs/usage.md`, the README, and the site keeps working exactly as before.
Full proposal, rationale, and what was deliberately deferred (not done here): `planning/specs/
spec-cli-simplification.md`.

## [2.60.8] - 2026-07-30

### Fixed
- **`provider = "auto"` (embeddings and wiki-LLM tiers) no longer commits to a local Ollama that
  doesn't have the target model pulled.** Root-caused the long-standing "Embedder unavailable —
  HTTP Error 404: Not Found" by reproducing it live: Ollama running for one model (e.g. a chat
  model) with the embedding/LLM default (`nomic-embed-text` / `llama3.1`) never pulled used to
  still get picked by "auto" (it only checked the daemon answered `/api/tags`, not whether *this*
  model was in the response), so every real call failed on Ollama's own genuine 404. `auto` now
  checks model availability (new `ollama_has_model()`) before picking Ollama, falling through to
  the builtin CPU model instead. An explicit `provider = "ollama"` config that hits a real 404 now
  gets Ollama's actual reason and a `run 'ollama pull <model>'` hint, not a bare
  `HTTP Error 404: Not Found`.
- Fixed a raw traceback fragment on `contextlake serve` (stdio transport): a second/third Ctrl-C
  landing in the brief window while Python joins the `mcp` SDK's background stdio-reader thread
  during interpreter shutdown printed a harmless but alarming "Exception ignored while joining a
  thread in `_thread._shutdown()`". Reproduced directly (3 rapid SIGINTs); fixed with a hard
  process exit immediately after `cmd_serve`'s own cleanup runs, skipping the rest of Python's
  shutdown sequence where the noise originated.
- Reworded the "corrupted .git" skip message (`index`, `repo_migrate`) after empirically verifying
  real submodules/worktrees are NOT false-flagged (already correct) — the actual gap was that the
  wording conflated two distinct situations. It now distinguishes "git can't find a repository here
  at all" (a dangling submodule/worktree link) from "git resolves it to a DIFFERENT, ancestor
  directory" (naming that directory) — the latter being the actual silent-misattribution case this
  check exists to catch.
- Dashboard Diagrams tab: the default (no module selected) view on a truncated repo now auto-scopes
  to the repo's largest module instead of an arbitrary alphabetical node slice — a repo with an
  `ExternalProjects/`-style vendored top-level directory no longer shows vendored code ahead of
  real source by default. An explicit pick from the module dropdown (including "Whole repo")
  still overrides this and sticks across format-tab switches.

### Added
- `graph --format graphml`: export the bounded subgraph slice as GraphML for Gephi/yEd import, with
  real typed node/edge attributes (kind, name, repo, file, line, lang / relation, confidence,
  weight) as GraphML `<data>` keys.
- `graph --format cypher`: export as Cypher `CREATE` statements for Neo4j/FalkorDB import. Node
  labels come from `kind`, relationship types from `relation`, both backtick-quoted since
  contextlake's kind/relation vocabularies are open text, not a fixed enum.
- CLI: unknown-flag errors now suggest a fix instead of a bare argparse dump. A flag valid on a
  *different* subcommand names where it belongs (`bootstrap --local` → "isn't a flag on
  'bootstrap' — it's used by: init, source"); a genuine same-command typo suggests the real flag
  (`--worksapce` → "Did you mean: --workspace?"); a value-taking flag immediately followed by
  another recognized flag names the real problem instead of argparse's generic "expected one
  argument" (`--workspace --open` → "needs a value, but the next token ('--open') is itself a
  recognized flag").

## [2.60.7] - 2026-07-30

### Fixed
- Dashboard's Mermaid-rendered diagram formats (`mermaid`/`classdiagram`/
  `statediagram`/`erdiagram`/`deploymentdiagram`) now cap a repo's internal
  subgraph by **edge** count, not just node count. A dense repo (heavy
  `contains`/`calls` fan-out -- found on a large real-world C/C++ codebase)
  could pack well over 500 edges into a 500-node slice, which exceeded
  Mermaid's own hard `maxEdges` default and made the Relations diagram fail to
  render outright. `repo_subgraph()`'s new `max_edges` parameter defaults to
  400 (safely under Mermaid's 500) for those formats only -- `--format html`/
  `dot`/`json` render via cytoscape/DOT, have no such limit, and are
  deliberately left uncapped by default (still overridable via the new CLI
  flag, `graph --max-edges N`). The dashboard's own `mermaid.initialize()`
  also raises `maxEdges` to 2000 as a belt-and-braces margin.
- Dashboard: fixed a real, reproducible DOM/stylesheet leak. On every FAILED
  Mermaid render, the library itself leaves a temporary `<div id="d<renderId>">`
  (holding a full injected stylesheet) sitting directly in `document.body` --
  it only cleans this up on success. Left alone, every failed render (e.g. the
  edge-limit error above, before this fix) permanently adds one more global
  stylesheet the page's CSS engine has to consider on every recalc, so it's a
  real, unbounded-growth correctness bug worth fixing regardless of how much
  it costs in practice. Now defensively removed after every failed render.

### Added
- Dashboard Diagrams tab: when a repo's diagram comes back truncated, a "scope
  to one module" dropdown appears, populated from the repo's top-level path
  segments (largest first, segments under 5 nodes dropped). Lets a huge repo be
  explored one directory at a time instead of only ever seeing an arbitrary
  (alphabetically-first) slice of 500 nodes -- which, for a repo with a
  `ExternalProjects/`-style vendored directory, meant vendored code crowded out
  real source in every default view. New `kb/visualize/payload.py::repo_modules()`
  + `/api/repo/<id>/modules` endpoint.

## [2.60.6] - 2026-07-29

### Fixed

- **`contextlake dashboard --serve` on a port already in use dumped a raw traceback**
  instead of a clean, actionable message. Found live: a second `dashboard --serve`
  invocation while one was already running. `serve_dashboard` now catches the
  `OSError` from the socket bind, logs what happened and how to fix it (`--port`, or
  stop the existing process), and returns a proper non-zero exit code -- `cmd_dashboard`
  now propagates that instead of always returning `0` regardless of what happened.
- **`contextlake index` (no `--source`/`--workspace`) now warns before silently
  bundling nested repos into one.** Indexing a workspace root that isn't itself a git
  repo but *contains* one (the common "cd into your mirrored fleet and just run index"
  mistake) folded every nested repo's files into a single made-up repo id -- then
  running the correct `index --workspace .` afterward duplicated that data under the
  nested repo's real identity. The zero-config single-repo behavior is unchanged (it's
  a legitimate, narrower use case); it now just says so and points at `--workspace .`
  when cwd clearly isn't that case.

## [2.60.5] - 2026-07-29

### Fixed

- **A def nested directly under an unnamed struct/union/enum could silently drop an entire
  C/C++ file from the graph.** Found indexing a real legacy C++ codebase: the containment-edge
  fallback used `def_node_to_id.get(parent.id)` without a default, so a structural parent that
  matched a def type but was never captured (anonymous structs/unions/enums have no `name:` field
  for the query to capture) produced `Edge(src=None)` -- a pydantic validation error that aborted
  parsing the *whole file*, not just that one edge, silently losing every node and edge it would
  have contributed. Now falls back to the file node, same as every other uncontained definition.
- **The embed step's error message now includes the exception's class name**, not just `str(e)`.
  Chasing an "Embedder unavailable — HTTP Error 404: Not Found" report turned into an extended
  investigation because several unrelated failure modes render similar-looking messages; the
  class name alone would have settled it immediately. No behavior change, purely diagnostic.
- **Test-isolation bug** (dev-only, not user-facing): two dashboard tests asserted
  `sources == []` while reading the real config precedence chain, so a machine with a populated
  `~/.contextlake/kb.toml` (e.g. from manually testing `init`) made them fail locally even though
  CI was always green. Both now isolate `HOME`.

## [2.60.4] - 2026-07-29

### Fixed

- **`init --local` now actually scopes its own defaults to the workspace it's writing into.**
  Found via live dogfooding: `contextlake init --local --yes` wrote a project-scoped
  `.contextlake.ini` correctly, but the `work_dir` *value* inside it still defaulted to a
  hardcoded `~/work` regardless of where `init` was run or that `--local` was passed at all --
  so `contextlake bootstrap` would mirror into a generic `~/work` directory instead of the
  project you were sitting in. The interactive prompt suggested the same wrong default. Both now
  default to the current directory (override with `--work-dir`, or by typing a different answer
  at the prompt). Confirmed this default was a generic, hardcoded literal, not anything read from
  your real environment -- it happened to resemble a real workspace name purely by coincidence of
  casing.
- **`init` now also scopes the knowledge-layer store under `--local`.** The generated
  `.contextlake.kb.toml`'s `store_dir` unconditionally pointed at the global
  `~/.contextlake/kb`, so two separate `--local` projects on the same machine silently shared one
  store. `--local` now defaults `store_dir` to a `.contextlake/kb` directory next to the
  workspace; a new `--store-dir` flag (and an interactive prompt) overrides it either way.

### Added

- **`--store-dir`** on `contextlake init`, alongside `--work-dir`: sets the knowledge-layer store
  location explicitly instead of taking the (now workspace-scoped, with `--local`) default.

## [2.60.3] - 2026-07-29

### Added

- **Shell tab-completion, on by default.** `argcomplete` is now a core dependency (pure Python,
  ~40KB, zero required dependencies of its own), so completion is available the moment
  `contextlake` itself is installed -- `pip install contextlake` alone. `contextlake init` then
  offers (on by default; `--no-completion` to skip) to register it with your shell: a one-line
  `eval "$(register-python-argcomplete contextlake)"` appended to `~/.bashrc`/`~/.zshrc` (zsh gets
  a `bashcompinit` line first), or a dedicated completions file for fish -- idempotent, and never
  touching anything else already in your rc file. `contextlake <TAB>` then completes every command
  and every one of its flags, generated live from the same parser that runs the command. See
  `docs/usage.md#shell-completion` for the manual one-liner per shell if you skipped it at `init`
  time (or use a shell other than bash/zsh/fish).
- **`contextlake version`** as a subcommand alias for `--version` (docker/npm/kubectl all support
  both spellings; `version` previously errored as an unknown command, suggesting `verify`).

## [2.60.2] - 2026-07-29

### Fixed

- **Ctrl-C during `init` (or any knowledge-layer command) no longer dumps a raw traceback.**
  Only the mirror-pipeline commands (`fetch`/`clone`/`sync`/...) had a `KeyboardInterrupt` catch in
  `cli.py`'s `main()`; `init`'s interactive prompts and the entire knowledge-layer dispatch
  (`index`, `wiki`, `dashboard`, `serve`, `source add`'s guided prompt, everything routed through
  `_KB_COMMANDS`) fell straight through to an unhandled `KeyboardInterrupt` and a stack trace.
  Both paths now exit clean with `Operation cancelled by user` (exit 130), matching the mirror
  commands' existing behavior -- including the lazy `from .kb import commands` import itself
  (tree-sitter/numpy/mcp, the slowest part of a cold start and a very reachable place for a real
  interrupt to land), not just the dispatch call after it.
- `contextlake serve` now reports `Stopping MCP server` on Ctrl-C instead of falling through to the
  generic top-level message, matching `graph --serve`/`--site` and `dashboard --serve`'s existing
  per-command stop messages.

## [2.60.1] - 2026-07-29

### Fixed

- **Chat: a failed question now offers Retry.** If a chat request fails for any reason (a network
  blip, the server restarting mid-request, and so on), the error now carries a **Retry** button that
  resends the same question in place, instead of leaving you to retype it.

### Documentation

- README now mentions the dashboard's Chat tab (shipped in 2.60.0 but not yet called out there).
- `docs/dashboard.md` §11 documents the new Retry button.
- `site/.gitignore` was missing `comparison.html` from its generated-page list -- the only
  `build_docs.py` output not covered, so it kept showing as untracked instead of ignored.

## [2.60.0] - 2026-07-29

### Added

- **Chat tab in the dashboard.** Ask a question about the fleet in plain language and get an answer,
  right in the browser. Two layers, always shown together:
  - **Free graph router (always on, no flag needed).** Reuses `contextlake serve`'s own `ask` MCP tool
    unchanged, in-process -- no logic duplicated. Classifies the question, dispatches to the matching
    graph tool (`find_definition`/`find_callers`/`blast_radius`/`who_knows`/`get_wiki`/semantic search),
    returns a structured, cited result. Zero LLM cost.
  - **LLM-synthesized prose (`--llm-chat`, opt-in at server start).** Turns that structured result into
    a short written answer using whatever `[llm]` provider `kb.toml` already has configured (the same
    setting `contextlake wiki` uses). The citations the prose was grounded in are always shown alongside
    it, expandable, so it's checkable rather than just trusted. An LLM failure degrades to the free
    result rather than erroring out.
  - `--llm-chat` mints the same per-launch token `--allow-mutations` uses, and every chat request while
    it's active must carry it -- a page other than this dashboard can't silently trigger a paid call. The
    free layer needs no token, same risk level as any other read-only `/api/*` route.
  - See [`docs/dashboard.md` §11](https://github.com/sayak-sarkar/contextlake/blob/main/docs/dashboard.md#11-chat).

## [2.59.1] - 2026-07-29

### Changed

- **Upgraded the `mcp` SDK dependency to 2.0.0**, lifting the `<2` stopgap pin from v2.58.3. Patched
  every breaking rename (`FastMCP` -> `MCPServer`, `streamablehttp_client` -> `streamable_http_client` in
  two files, `CallToolResult.structuredContent`/`isError` -> snake_case, the removed
  `mcp.shared.memory` in-memory test helper -> the new `mcp.Client`).

### Fixed

- **`contextlake serve` was completely broken for any tool touching the SQLite-backed store**, on
  either transport. This mcp SDK version dispatches every synchronous tool call through
  `anyio.to_thread.run_sync` unconditionally, so a server-lifetime store sharing one `sqlite3`
  connection across calls now crashed with "SQLite objects created in a thread can only be used in
  that same thread" the moment any tool ran (confirmed live against a real HTTP-served server: works
  on mcp 1.28.1, broken on 2.0.0). `SqliteStore`, `VectorStore`, and `SqliteVecStore` now hand out one
  connection per thread instead of one connection total (WAL mode, already in place, is exactly the
  mode SQLite recommends for this).

## [2.59.0] - 2026-07-29

### Added

- **Directory-scoped config with inheritance.** `contextlake init --local` (and `contextlake source add
  --local`) writes `.contextlake.ini` / `.contextlake.kb.toml` into the current directory instead of
  `~/`. The "local" tier of both config systems now walks up from the current directory to the
  filesystem root looking for these files -- the same discovery model `git` uses for `.git` -- so a
  config at a project's root is inherited by every subdirectory underneath it, not just the exact
  directory that holds the file. Previously the local tier only ever checked cwd literally, which meant
  running any command from a subdirectory silently fell straight through to the global config.
  `contextlake source add`/`remove`/`enable`/`disable` now default to the nearest ancestor's local config
  once one exists, instead of always writing to global.

## [2.58.3] - 2026-07-29

### Fixed

- **Pinned `mcp` to `<2` in the `kb` extra.** The v2.58.2 release gate failed in CI (nothing published)
  because `mcp>=1.28` had no upper bound and CI resolved the just-released `mcp` 2.0.0, which renamed
  `streamablehttp_client` to `streamable_http_client` in `mcp.client.streamable_http` -- breaking every
  connector/MCP import at collection time. Unrelated to any code in this release; a fresh install
  yesterday would have hit the same break. Pinned below the major bump until `contextlake.kb.mcp_client`
  is deliberately audited and updated for the 2.x API, rather than chasing it mid-release.

## [2.58.2] - 2026-07-29

### Added

- **`contextlake init`'s data-source prompt now loops** ("Connect a data source now?" then "Connect
  another data source?") instead of collecting exactly one source per run -- found via dogfooding: adding
  a second source meant re-running `contextlake source add` by hand afterward.
- **`init` and `source add` explain what "Source name" actually means** -- it's a local nickname you pick
  to reference the connection later (`contextlake source test <name>`), not your Atlassian site, Figma
  team, or any other provider-side identifier. Also found via dogfooding: a real user typed their org name
  as the "source name," reasonably expecting it to be some kind of account identifier.
- **`init`/`source add` suggest each provider's official hosted MCP URL as the default** for `atlassian`
  (`https://mcp.atlassian.com/v1/mcp/authv2`) and `figma` (`https://mcp.figma.com/mcp`), verified against
  each provider's own docs, so the prompt no longer forces you to already know the endpoint yourself.

### Fixed

- **`[llm] provider = "cli"` with `command = "gemini"` was broken since it shipped.** `gemini`'s
  `-p`/`--prompt` flag is a required string *value*, not a boolean "read the rest from stdin" switch --
  the preset sent `["-p"]` with the prompt only on stdin, which produced a yargs usage dump instead of a
  completion (found live once a real subscription login was available to test against). `CliLlm` now
  substitutes a `{PROMPT}` placeholder in `gemini`'s preset args with the actual prompt at call time and
  skips stdin for that call -- every other command (`claude`, `codex`, a user's own CLI) is unaffected
  and keeps feeding the prompt on stdin. This trades away two properties for `gemini` specifically: no
  more `ARG_MAX` headroom for very large prompts, and the prompt is now visible to other local processes
  via `ps`/`/proc` for the duration of the call (still never a secret, but no longer stdin-only).
- Confirmed live (not just from docs) that `codex`'s ChatGPT-subscription login is **not** hijacked by a
  stray `OPENAI_API_KEY` in the environment -- ran a real `cli`-provider wiki generation with the key set
  and it used the subscription login without complaint, unlike `claude`/`gemini`. The v2.58.1 entry below
  called this "not confirmed"; it now is. `OPENAI_API_KEY` is still stripped defensively for `codex`, since
  a future `codex` version could change that behavior.

## [2.58.1] - 2026-07-28

### Fixed

- **`[llm] provider = "cli"` no longer leaks the CLI's own API-key env var into the child process.**
  The whole point of this provider is reusing a subscription (`claude -p` / `gemini` / `codex`)
  instead of an API key contextlake would have to hold -- but `claude` and `gemini` both treat their
  own key (`ANTHROPIC_API_KEY`; `GEMINI_API_KEY`/`GOOGLE_API_KEY`) as an auth override that takes
  precedence over the subscription login and must be unset to fall back to it. A key set anywhere in
  the shell for an unrelated reason (testing the `anthropic`/`openai` provider directly, another tool)
  silently flipped `cli` onto pay-per-token auth instead -- found live while testing (`claude -p`
  failed with "Credit balance is too low" the moment `ANTHROPIC_API_KEY` was exported, worked once it
  was unset; the `gemini` behavior is confirmed from its own auth docs, not live-tested here).
  `CliLlm.generate()` now strips the matching var(s) from the subprocess environment only, per
  recognised command (matched by basename, so a path-qualified `command` still strips) -- an
  unrecognised `command` (a user's own CLI) strips nothing, since its auth model isn't known.
  `OPENAI_API_KEY` is stripped for `codex` too as a defensive precaution, though its docs describe
  API-key auth as a separate explicitly-opted-into login mode rather than an env-var override.

## [2.58.0] - 2026-07-27

### Changed

- **Empty-repo classification: new `note` state, distinct from `skip`.** A repo with zero commits
  (`update`/`branches` can't resolve `HEAD` -- there's no history to read yet) previously reported as
  `skip` (`⊘`), which reads as "something that would normally happen didn't." It isn't that: it
  describes what the repo *is*, not something withheld. `update_repository()`/
  `switch_repository_branch()` now return `"note"` instead of `"skip"` for this case, with a
  friendlier message (`"New repo -- no commits yet"`, was `"No commits yet (empty repository)"`).
  New neutral glyph `•` (`style.note()`), a new `update`/`branches` summary bucket (`empty`, was
  folded into `skipped`). **Behavior change for anything parsing CLI output**: the returned status
  string changed from `"skip"` to `"note"` for this specific condition. `docs/console-output.md`
  documents the new glyph and the skip-vs-note distinction.

## [2.57.0] - 2026-07-27

### Added

- **Dashboard mutating routes (`--allow-mutations`).** The dashboard was
  strictly read-only (v1). `contextlake dashboard --serve --allow-mutations`
  now additionally exposes three write actions, each behind an explicit
  confirm in the browser: **Sync now** on a repo page (`git pull --ff-only` +
  reindex), **Add repo** on the fleet overview (clone a URL + index it), and
  **Start/Stop/Restart** for a separate `contextlake serve --transport http`
  process from the MCP console tab. Security-reviewed before shipping (a
  `BaseHTTPRequestHandler` answering POST on localhost is a classic
  CSRF-to-RCE shape): refused outright with `--sample` or a non-loopback
  `--host`; a random per-launch token (custom `X-Contextlake-Token` header,
  so a cross-origin POST can't complete the preflight it would trigger) plus
  a `Host` header check (blocks DNS rebinding around the loopback bind) gate
  every mutating request; `git clone`'s URL is scheme-allowlisted
  (`https://`/`ssh://`/`user@host:path`, rejecting flag-injection and the
  `ext::` arbitrary-command transport) and always passed after a literal
  `--`; each mutation takes the store's single-writer lock for its own
  duration only, so a concurrent CLI command sees a clean `409` instead of an
  interleaved write. New `kb/dashboard/mutations.py`. Verified against a real
  git repo (not mocks) end-to-end, including a live curl pass against the
  playground store. See [dashboard.md §10](docs/dashboard.md#10-mutating-routes---allow-mutations).

- **Per-symbol ticket attribution.** `tracked_by` edges could previously only
  originate from a repo node (a branch name or a doc link, with no way to say
  *which* symbol an issue relates to). Two new candidate sources — a symbol's
  own docstring, and the git-blame commit message on its defining line (one
  batched `git blame` per file, not per symbol) — each a bare-key regex
  match, so both are AMBIGUOUS candidates fed through the exact same live-JQL
  `verify_issues`/`reconcile` pipeline that already promotes branch-derived
  keys to INFERRED. New `connectors/symbol_refs.py` (pure logic) +
  `AtlassianConnector.associate_symbols()`. Closes the approved-spec
  divergence flagged when dashboard cross-linking shipped (breadcrumb ends
  in repo-level "Links" instead of a per-symbol "Ticket"): the symbol/blast-
  radius page's breadcrumb now gets a real **Ticket** crumb when a symbol
  has one, opening the tracker URL directly. Built and tested against a
  spawned mock/real git repo (no live Jira credentials this session); real-
  workspace verification is still needed before this is production-proven.
- **Slack connector.** A new `slack.py` connector (mirroring the Figma/Atlassian
  shape) classifies `slack.com` permalinks in a repo's docs (`/archives/<channel>`
  and `/archives/<channel>/p<ts>`) into channel/message links, wired into
  `connect`/`contextlake source` alongside the existing three connectors.
  Reachability is checked best-effort over a configured Slack MCP; there's no
  single spec-mandated tool name across Slack MCP servers, so the verification
  tool is configurable (`verify_tool`, default `conversations_info`) rather
  than assumed. Built without live Slack credentials this session (tested
  against a spawned mock MCP server); real-workspace verification is still
  needed before this is considered production-proven.
- **Deeper Figma enrichment.** `FigmaConnector.fetch_metadata()` (used by
  `connect`) now merges a design's *real* metadata (a name and/or top
  structural frame/page names, parsed from either a simplified dict or
  Figma's own XML `get_metadata` response) into the design node, on top of
  the URL-slug title that was previously the only source of a name.
- **Single-binary releases via [PyApp](https://ofek.dev/pyapp/).** A new
  tag-triggered `binaries.yml` workflow builds a self-contained launcher per
  platform (`contextlake-linux-x86_64`, `contextlake-macos-arm64`,
  `contextlake-windows-x86_64.exe`) and attaches them to the GitHub Release.
  Each binary embeds `contextlake[kb-full]`'s project metadata and bootstraps
  a private Python + the package into its own cache on first run (network
  needed once; every run after is instant) — for the audience that has
  nothing preinstalled, not even Python. Deliberately a separate workflow
  from `release.yml`, so a binary-build failure can never block the PyPI
  publish. `uvx` / `uv tool install` remain the recommended path for anyone
  who already has `uv`. Does not bundle the optional `llm-local` wiki
  backend (needs a C++ toolchain to build); `ollama`/`openai`/`anthropic`/
  `cli` remain available as wiki LLM providers.

## [2.56.0] - 2026-07-27

### Added

- **Dashboard: a "Call sequence" card on the symbol/blast-radius page.** The one
  `graph --format` Mermaid diagram the repo-level Diagrams tab (v2.55.0)
  couldn't offer — `sequencediagram` needs a single symbol seed, not a
  repo-wide view — is now reachable from where a seed already exists: the
  symbol page. A new `/api/impact/diagram?node=<id>` endpoint reuses the same
  `extract_subgraph -> to_payload -> to_sequence_diagram` pipeline `graph
  --node <id> --format sequencediagram` already runs.
- **`query --retriever {fts,semantic,hybrid}`.** Semantic/hybrid search was
  previously only reachable via `contextlake eval`; `query` now accepts the
  same flag and reuses `eval`'s exact retriever factories, degrading to an
  honest fts fallback (never a crash, never a silent network call) when
  embeddings aren't configured. Fixed in the same pass: `--kind` was silently
  ignored under `--retriever semantic|hybrid` (plain fts already filtered by
  it); it now filters there too.
- **Dashboard: a "Data flow" tab.** Intra-repo `reads`/`writes` edges
  (extracted since v2.48.0 but never surfaced anywhere — no CLI, dashboard,
  or `visualize/` consumer read them before now) are now visible per repo:
  which file reads or writes which SQL table/view, each with its
  file:line and a citation. Deliberately not folded into the existing
  `dependencies`/`http_flow`/`event_flow` relationship tables — those are
  repo→repo aggregates on a node shared across repos by construction; a
  table/view definition is only ever known within the repo that defines it,
  so this is a different, honest row shape (file→table, always single-repo).
- **Docs: typed callouts.** Python-Markdown's built-in `admonition` extension
  (`note`/`warning`/`important`, no new dependency) replaces the handful of
  existing bold-lead blockquotes that were really a distinct interruption —
  a risk before you act, an honest limitation, a must-not-skip guarantee —
  while generic asides stay plain blockquotes.
- **Docs: per-page-type hero accent.** A doc page's hero eyebrow now
  recolors by its nav group (Get started/Build your knowledge base/Use
  it/Understand it), from the existing brand palette — a "where am I" signal
  at a glance, not new illustration work.
- **Landing page: the "Get started" terminal card** now matches the depth of
  the rest of the fog→clarity system — a teal border/glow and a blinking
  cursor after the last command (respects `prefers-reduced-motion`).

### Fixed

- **Dashboard server: a client disconnecting mid-response no longer logs a
  traceback.** `_send()`'s `wfile.write` is now guarded against
  `BrokenPipeError`/`ConnectionResetError` — a browser tab closed mid-load or
  `curl` killed early is normal, not an error.
- **Empty-repo classification consistency.** `core.py`'s
  `update_repository()` classified a no-HEAD repo as `error`; the
  branch-switch path (same file, identical condition) already classified it
  as `skip`. Both now agree on `skip` — nothing failed, there's just nothing
  to sync yet, and error tallies stay meaningful.
- **`docs/img/architecture.png`** regenerated as transparent RGBA via the
  existing `gen_diagrams.py` + cairosvg pipeline — was the one hand-made
  diagram on the site that didn't adapt to dark mode.

## [2.55.0] - 2026-07-27

### Added

- **Dashboard: a repo page's `Diagrams` tab.** Five of the six `graph --format`
  Mermaid diagrams (`mermaid`/`classdiagram`/`statediagram`/`erdiagram`/
  `deploymentdiagram`, `sequencediagram` excluded — it needs a single symbol
  seed, not a repo-wide view) are now reachable from the dashboard, not just
  the CLI, rendered inline as SVG with a raw-source copy card. A new
  `/api/repo/<id>/diagram?format=<fmt>` endpoint reuses the exact
  `repo_subgraph -> to_payload -> renderer` pipeline the CLI already runs, no
  new extraction or rendering logic. The format switcher only enables formats
  the repo actually has data for (classes for `classdiagram`, tables for
  `erdiagram`, etc.), read from the same anatomy census the repo page's Kinds
  card already fetches. Mermaid.js (vendored offline, MIT, ~3.5MB) is
  lazy-injected into the page only the first time the tab is opened, at
  `securityLevel: "strict"` (mermaid's own DOMPurify-sanitized mode — diagram
  text embeds repo-derived symbol/table/resource names, untrusted input).
  Live-only, same as MCP console/Settings.

### Fixed

- **`deploymentdiagram`: a repo's own Python/JS/etc. module nodes no longer
  leak into the Terraform diagram.** `kind="module"` isn't exclusive to HCL —
  `kb/parse.py` emits `kind="module"` package nodes for every code language —
  so a repo with both Terraform *and* regular source files was incorrectly
  drawing unrelated source-module nodes as deployment "module" entries. Now
  gated on `lang="hcl"` too. This bug shipped in v2.54.0's `deploymentdiagram`
  release; caught while writing this release's dashboard Diagrams tests
  against a fixture with an ordinary Python module node alongside a Terraform
  resource, and fixed here using the same revert-the-fix, watch-it-fail
  discipline v2.54.0 used for its own data-block categorization bug.

## [2.54.0] - 2026-07-27

### Added

- **`graph --format deploymentdiagram`: a Mermaid flowchart of Terraform/HCL
  `resource`/`data`/`module` definitions grouped by an inferred category**
  (network/compute/storage/database/security/module/other), over data
  `kb/hcl.py`'s existing extractor already collects (no new extraction pass,
  same spirit as `erdiagram`). Category is a keyword heuristic over the
  resource type prefix (`aws_security_group.web` -> security); more-specific
  categories are checked before generic ones so e.g. `aws_db_instance` lands
  in database, not compute, on the "instance" substring — caught live before
  shipping via a real Mermaid render, not just code review. A `data` block's
  address (`data.<type>.<name>`) is unwrapped before categorizing, so a data
  source is grouped by its underlying resource type, not left in "other" on
  the literal `data.` prefix. A single-category view renders flat (no
  subgraph wrapper); `depends_on` edges (reconstructed by `parse.py` from
  `var.`/`module.`/type-name interpolation references) draw the connections.
  `--c4` continues to reject text-diagram formats (`deploymentdiagram` added
  to that list). A repo with no `.tf` files renders an honest empty diagram
  with guidance, not a bug.

## [2.53.0] - 2026-07-26

### Added

- **`graph --c4 --c1`: C1 external-system layer.** One dashed box per distinct
  host an indexed repo calls over HTTP that never resolves to any indexed
  repo's exposed route (`kb/arch/resolve.py:repo_external_system_edges`),
  drawn outside every namespace boundary, connected by a `calls_external`
  edge. Deliberately unclassified: a genuine third party and an unindexed
  internal service look identical here (no `internal_domains` allowlist in
  V1 — see `contextlake-planning/specs/spec-D-c1-system-context.md`). No new
  extraction pass: `flow/http.py`'s `calls_http` edges now carry the raw
  call-target host (`Edge.attrs["raw_host"]`, new `Edge.attrs` field,
  `edges.attrs` store column, `SCHEMA_VERSION` 1→2 with an automatic
  `ALTER TABLE` migration for existing stores — verified against a
  pre-v2.52 store shape, no manual step needed). `--c1` requires `--c4`.
  Verified live end-to-end (host captured → persisted → joined against
  `exposes` → rendered, confirmed both the resolved and unresolved paths on
  a real two-repo fleet).

## [2.52.0] - 2026-07-26

### Added

- **ADR/decision-record surfacing.** A repo's own decision docs under common
  conventions (`docs/adr/`, `docs/decisions/`, `decisions/`, `adr/`) become
  first-class `adr` nodes in that repo's shard during `index` — no separate
  command, no `@enrich:`/`@ingest:` side-channel. Title comes from the file's
  first `# ` heading, or the filename otherwise. Semantically searchable
  (`adr` added to `EMBEDDABLE_KINDS`) and cited in `wiki` generation as a
  grounded "Recorded decisions" section, distinct from connector-sourced
  "External context" — an ADR is authored, checked into the repo's own git
  history, so it's presented as a fact, not something to attribute or hedge
  on. No column data, no edges to other nodes: a decision doc mentioning a
  class by name isn't a verified reference the way an import is.
- **`graph --format erdiagram`: a Mermaid ER diagram of `table`/`view` definitions
  and their foreign-key `references` edges**, over data the SQL DDL extractor
  already collects (no new extraction pass). Entities render as bare boxes
  (the extractor has no column data); a `REFERENCES` clause always points
  child-row to parent-row, so cardinality (`||--o{`) is asserted from FK
  semantics, not guessed. An ORM-only schema (SQLAlchemy/Entity Framework/
  TypeORM, no literal `CREATE TABLE` text) renders an honest empty diagram
  with guidance instead of looking broken.
- **`wiki` now hints once per run when the builtin model is doing the council
  review.** The builtin 0.5B is a weak reviewer (near-constant high accept
  scores, mostly rubber-stamping): still functional, but a real backend
  (`--llm anthropic|openai|ollama|cli`) gates meaningfully. The note prints
  once, before generation starts, not per repo.

### Fixed

- **Docs house-style: em-dashes stripped from the remaining `docs/*.md`
  pages** (`serve.md`, `cli-reference.md`, `index-code-graph.md`,
  `visualize.md`, `dashboard.md`, `connect-enrich.md`) that had drifted since
  the original 2026-07-24 pass, replaced context-aware with colons,
  semicolons, commas, or parens per the surrounding sentence.

## [2.51.1] - 2026-07-26

### Fixed

- **A top-level unrecognized flag followed by a path-looking token no longer
  reports a confusing "Unknown command".** `contextlake --work-d /tmp doctor`
  (`--work-d` isn't a real flag) used to say `Unknown command: '/tmp'` — `/tmp`
  fell into the `<command>` positional slot since argparse never learned
  `--work-d` expected a value. Now correctly reports `unrecognized arguments:
  --work-d`, the same message the no-trailing-token and subcommand-scope forms
  of this mistake already got right.
- **Dashboard/graph architecture view: repo labels no longer overlap when a
  namespace cluster expands.** The mindmap drill-in's grid layout only spaced
  node *shapes* apart (`avoidOverlap`), not their label text, so adjacent
  repos with longer names (`catalog-api`, `auth-service`, `shared-lib`) ran
  into each other. Now accounts for label width
  (`nodeDimensionsIncludeLabels`) and uses more generous cell spacing.

## [2.51.0] - 2026-07-26

### Added

- **Dashboard: MCP console + Settings surfaces.** Two read-only, live-only panels
  (not part of a `--site` export). **MCP** shows the live tool catalog for
  `contextlake serve` against this store — introspected from a real
  `server.build_server()` instance so it can never drift from what's actually
  exposed — plus copyable `.mcp.json` / `.vscode/mcp.json` snippets (reusing
  `steer.generate.mcp_server_entry`, the same entry `contextlake steer` writes).
  **Settings** summarizes the active `kb.toml`: store path/size/schema version,
  the mirror root (derived from indexed repo paths), configured connectors, and
  the embedder/LLM tiers — no in-browser editing, edit `kb.toml` directly.
  Connector rows show configured status only, never a live connectivity probe
  (`contextlake source test <name>` already does that on demand; auto-probing
  every connector on every dashboard page load would be a surprising network
  side effect from a read-only view). Under `--sample`, both panels use bare
  config defaults instead of the real precedence chain, so a real
  `~/.contextlake/kb.toml` (languages, connectors, embedder/LLM settings) never
  leaks into the fleet billed as "fictional data, nothing local is read".

### Fixed

- **`index --workspace` no longer silently misattributes a corrupted nested
  `.git` to an unrelated ancestor repo.** `git -C <path>` walks *up* the
  filesystem tree past an incomplete/corrupted `.git` to find the nearest real
  one, so a broken checkout could previously be indexed under a completely
  different repo's remote and commit history, with `0 failed` reported —
  found incidentally by the v2.50.0 post-release testing pass. Now verified
  via `git rev-parse --show-toplevel` before any identity lookup is trusted;
  a broken `.git` is skipped with a warning instead.
- **`query`/`owners`/`impact --json` on an empty argument now emit a
  structured JSON error** (`{"error": "missing_argument", "usage": "..."}`)
  instead of a plain-text `usage: ...` line with a log timestamp — the
  previous behavior broke `--json`'s "always valid JSON" contract on exactly
  the case a script piping to `jq` most needs it to hold.
- **MCP `semantic_search`/`hybrid_search`/`ask` no longer crash on an empty
  query.** An empty string embeds to a zero vector, which crashed downstream
  in the vector store's similarity scoring with a raw, uncaught
  `TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'`
  instead of degrading gracefully the way `search_code` already does. All
  three now return an empty result for an empty/whitespace-only query.
- **`contextlake init --config <path>` no longer silently ignores `--config`.**
  `init` is the one command that writes both generated files (mirror INI +
  kb.toml) and previously always targeted the fixed defaults
  (`~/.contextlake.ini` / `~/.contextlake/kb.toml`) regardless of `--config` —
  every other command honors it correctly. `--config` now redirects the mirror
  INI, with kb.toml written alongside it as a sibling.
- **`update`/`branches` no longer keep a green checkmark on their final
  summary line when a repo failed.** `index`/`embed`/`wiki` already swap to
  the warning glyph on partial failure; `update`/`branches` called
  `style.ok()` unconditionally, so a failed run's summary still visually read
  as success (the failure detail was only apparent further down).
- **A malformed `source add --set KEY` (no `=`) now fails cleanly** instead of
  dumping an uncaught Python traceback — the error message itself was already
  correct, it just wasn't caught.
- **`--plain`'s `--help` text no longer overclaims.** It strips ANSI color
  (same as `NO_COLOR=1`); the unicode status glyphs (✓⚠✗...) are hardcoded
  literals with no ASCII fallback and always render, which the previous text
  ("no colour or glyphs") didn't reflect.

## [2.50.0] - 2026-07-26

### Added

- **A mistyped subcommand now suggests the closest real one** instead of
  dumping argparse's raw 30-item choice list (`contextlake fetc` →
  `Did you mean: fetch?`). Reuses the same `difflib`-based fuzzy match already
  powering unknown-repo-id suggestions. Matches against every command
  including aliases, then always displays the canonical verb — a typo of
  `blast-radius` suggests `impact`, matching what `--help` teaches (`cli.py`).
- **`--help` now links to the docs site and the issue tracker**
  (`https://sayak.in/contextlake`, GitHub issues) so a stuck user isn't a
  search engine query away from either.
- **`-n`** as a short form of `--dry-run`, matching the near-universal
  `rm`/`cp`/`make` convention.
- **`--plain`** as a friendlier, discoverable spelling of `NO_COLOR=1` — same
  code path, just a flag instead of an environment variable.
- **`contextlake source add --from-stdin KEY`** reads a connector option's
  value off a pipe instead of the command line, so a secret never lands in
  shell history (`--set KEY=VALUE` already never echoes it to logs; this
  closes the other exposure vector). Errors clearly instead of hanging if
  stdin isn't actually piped.
- **`--json` on `query`, `owners`, `impact`, and `lint`** — the four commands
  whose entire job is answering a question had no machine-readable output,
  despite contextlake's whole pitch being "agents answer from real source
  instead of guessing." Reuses `graph`'s already-proven pattern: logs move to
  stderr via `use_stderr()`, the payload is the only thing on stdout. Error
  cases (`unknown_repo`, `not_found`, `ambiguous`) are structured JSON too,
  not just the success path. Exit-code contract: `--json` mirrors the human
  path exactly, including `lint --json` returning 1 on an unclean graph (not
  just on a malformed request) — a CI script piping to `jq` still gets valid
  JSON on a non-zero exit, so check `$?` deliberately rather than assuming
  0 means "ran" instead of "clean".

### Fixed

- **`embed` and `wiki` (both per-repo and `--namespaces`) silently reported a
  partial failure as a clean success.** If even one repo/namespace out of a
  batch failed, the summary line kept its `✓` glyph and simply omitted the
  failure from the count — indistinguishable from a fully successful run
  unless you scrolled back through the whole log. Both now show `⚠` and the
  failed count whenever `failed > 0`.

### Changed

- **`update`, `branches`, `index --workspace`, `embed`, and `wiki` now name a
  concrete next step when something failed**, instead of ending on a bare
  summary line. `update`/`branches` list the failed repos and the exact retry
  command (`contextlake update --repos <name>`); `index` points at the log
  and notes re-runs are incremental; `embed`/`wiki` point at the log and
  suggest re-running. `update` also notes how many repos were auto-switched
  to a new branch. Mirrors `_bootstrap`'s existing ending, which already did
  this well.
- **Flags can no longer be silently abbreviated** (`--work-d` used to resolve
  to `--work-dir` via argparse's default `allow_abbrev=True`). Disabled on the
  root parser and every subcommand parser — an unrecognized long flag is now
  a clear error instead of an undocumented shortcut that breaks the moment a
  new flag creates an ambiguity.

- **`contextlake update` no longer just reports a deleted upstream branch and
  waits for the user to run `branches` by hand.** A tracked branch missing on
  origin almost always means it was renamed, merged, or superseded by another
  default — not something that needs manual triage. `update` now auto-fetches
  the full branch list and switches to the most-active remaining branch (the
  same selection `branches` makes), reporting `switched` with both the old and
  new branch name. Falls back to the previous clean-skip-with-hint behavior
  only if the broader reselect fetch itself fails too (e.g. a real network
  outage), or if no other branch exists (`core.py`).

## [2.49.0] - 2026-07-26

### Removed

- **Dropped the `gitlab-sync` backward-compat layer** (the pre-rename project name).
  This is a breaking change for anyone still relying on it — nothing else changes.
  Specifically removed: the `gitlab-sync` console-script alias; reading a legacy
  `~/.gitlab_sync.ini` / `.gitlab_sync.ini` file or its `[gitlab_sync]` INI
  section; reading a legacy `~/.gitlab-sync/kb.toml` / `.gitlab-sync.kb.toml`
  knowledge-layer config, and falling back to a pre-existing `~/.gitlab-sync/kb`
  store when `~/.contextlake/kb` doesn't exist; recognizing the old
  `<!-- BEGIN gitlab-sync ... -->` managed-block marker in `contextlake steer`
  output. Use `contextlake` (not `gitlab-sync`), `.contextlake.ini` /
  `~/.contextlake.ini`, and `~/.contextlake/kb.toml` / `~/.contextlake/kb`
  going forward — migrate any existing legacy config/store to the current
  names before upgrading past this release.

## [2.48.2] - 2026-07-26

### Fixed

- **The C4 diagram's namespace boundary tagging could mark a real edge as
  internal when it wasn't, or as crossing a boundary when it wasn't.** Two bugs
  in how repos were bucketed into namespaces for boundary purposes (shared with
  the dashboard's display-grouping heuristic, `derive_groups`, but wrong for
  boundary tagging specifically): (1) a repo whose id IS exactly a namespace
  prefix (e.g. a repo literally named `acme` alongside `acme/pay/api`) fell into
  the meaningless catch-all `"(ungrouped)"` bucket instead of the `acme`
  boundary its own child repo joined, so a real edge between them wrongly
  rendered as crossing a boundary; (2) `"(ungrouped)"` was treated as one shared
  namespace, so two entirely unrelated single-segment repos that only
  coincidentally had no deeper namespace got a real edge between them rendered
  as internal, identical to a genuinely related same-namespace edge. C4
  boundary tagging now uses its own bucketing rule, independent from
  `derive_groups`: each repo's own path prefix (or its full id, if shorter than
  `group_depth`) becomes its namespace, so a repo with no real namespace always
  gets a namespace of exactly itself (`kb/c4.py`).

- **Wiki council review parsing could drop a review's real issues or fabricate a
  wrong score, on the malformed-JSON recovery path a small local model's output
  regularly takes.** Three related bugs in `_parse_review`/`_extract_score`: (1)
  the naive `text[first "{" : last "}"]` slice broke as soon as any trailing
  prose contained its own brace, discarding a validly-parsed `issues` list along
  with the JSON — now uses `json.JSONDecoder.raw_decode`, which stops at the
  first complete object regardless of what follows; (2) when JSON parsing failed
  outright, the fallback score-recovery regex scanned the *entire* raw text with
  `re.search` (first match wins), so an unrelated "...rating is 1 star..." aside
  earlier in a model's own issue text could win over the real, later `"score"`
  field — now the literal JSON-quoted `"score": N` form is tried first, since it
  can't be confused with ordinary prose; (3) the "N out of 10" fallback matched
  anywhere in prose with no context check, so "3 out of 10 endpoints... are
  undocumented" (a coverage-gap description) was misread as a 0.3 review score —
  now rejects a match immediately followed by a noun, the count-phrase shape
  (`kb/wiki/council.py`).

- **`contextlake hook install` could silently wire a hook to the wrong `repo_id`
  and never say so.** `_canonical_repo_id` swallowed every exception from opening
  the store (a bad `--config`, a corrupt/too-new store) with a bare `except:
  pass` and fell back to the bare directory name — even for an already-indexed
  repo, and even though `SqliteStore` itself never raises for a fresh/not-yet-
  indexed store (it creates one on open), meaning anything landing in that catch
  was a real problem, not the benign case the fallback was written for. One blast
  radius: a bad `--config` produced a hook install reported as a clean success
  but permanently inert (its own re-index invocation embeds the same bad config
  and silently never runs). Another: a transient store error left a duplicate
  `repo_id` row after the hook did fire. Now logs a warning explaining the
  fallback instead of swallowing it silently (`kb/commands.py`).

- **`contextlake steer` could silently corrupt a user's AGENTS.md/CLAUDE.md/etc. on a
  later refresh, or crash mid-run leaving the workspace half-steered.** A repo id or
  package name (the latter reachable verbatim via `manifest.py`'s unvalidated
  package.json dependency-key parsing) carrying a backtick or the literal
  `<!-- END contextlake -->` marker text broke out of its markdown code span or
  smuggled a duplicate marker into the generated body; the next refresh's naive
  first-occurrence `_upsert_block` splice then truncated/duplicated real content.
  Now: names are sanitized before interpolation, and `_upsert_block` refuses to
  splice (warns instead) if a marker pair doesn't cleanly bound a single block.
  Separately, an existing `.mcp.json`/`.vscode/mcp.json` with a non-dict
  `mcpServers`/`servers` value (e.g. `null`) crashed `_merge_mcp_entry` uncaught
  after markdown/skill files were already written — now self-heals instead. Also: a
  relative `--config` value is now resolved to absolute before being embedded in the
  generated MCP entry, since it may be launched from `--out`, not the invocation
  directory (`kb/steer/generate.py`, `kb/commands.py`).

- **Mermaid diagrams (`to_mermaid`/`to_class_diagram`/`to_sequence_diagram`) could emit
  invalid or directive-injecting output from ordinary node/edge text.** `_mermaid_escape`
  only escaped `"`, `[`, `]`. Confirmed against a real Mermaid parser: a `|` in an edge's
  `relation` broke the `-->|label|` delimiter (invalid diagram); a `}` in a class member's
  signature closed the `class X { ... }` body early, letting text after it emit as new
  top-level statements; a newline in a node's name became a genuine new line, rendering as
  a real `Note over ...` annotation box rather than inert label text. Now also escapes `{`,
  `}`, `|`, and newlines (`kb/visualize.py`).

- **The dashboard's "Generate wiki" action copied a command that silently
  no-ops.** docs/dashboard.md documents `contextlake wiki <repo-id> --llm
  builtin`; the actual generated/copied command
  (`kb/dashboard/static/dashboard.js`, shared by both `--serve` and `--site`)
  was `contextlake wiki <repo-id>` with no `--llm` flag at all — and without
  `--llm` (or `[llm]` enabled in `kb.toml`), the wiki stage does nothing.
  Not just a docs mismatch: the copied command didn't work. Now appends
  `--llm builtin`, matching the docs.

- **`ask`'s `explain`/`owners` routes didn't resolve a repo by its short name,
  contradicting the docs' own headline example.** `ask("explain the
  catalog-api")` (verbatim from docs/serve.md) silently fell through to
  `search` instead of returning wiki prose or a repo brief: repo ids are
  always host-qualified (`gitlab.example.com/acme/catalog-api`), but the
  router only extracts the trailing "catalog-api" out of the question, which
  never matched the full stored id via `get_wiki`/`get_repo_brief`/
  `who_knows`'s exact lookup. A person naturally refers to a repo by its
  short name, so this needed to be resolved, not documented around. Added
  `_resolve_repo`, mirroring the existing symbol-resolving `_resolve_id`:
  falls back to matching the repo's last path segment when an exact id
  lookup misses (`kb/server.py`).

- **`contextlake audit --repos PATTERN` accepted the flag but silently ignored
  it, scanning every repo regardless.** docs/usage.md promises `--repos`
  works for "every mirror command" (audit included, per
  docs/cli-reference.md's own mirror-tier list) — but `scan_repo_metrics`
  called `get_local_repos` directly and never consulted `repo_filter`, unlike
  fetch/clone/update/branches/verify/status, which already route through the
  same `match_repo_filter` check. Now filters the same way (`metrics.py`).
  `repo_filter_patterns` (`core.py`) is promoted from `_repo_filter_patterns`
  to a public name, reflecting that it was already a cross-module helper
  (`kb/commands.py` also calls it).

- **Wiki review parsing: a JSON `"score": true`/`false` was silently accepted
  as a perfect/zero score.** `bool` is an `int` subclass in Python, so
  `float(True) == 1.0` raises nothing — the primary score-parsing path
  lacked the same `not isinstance(val, bool)` guard its own sibling fallback
  ladder (`_extract_score`) already applies. Now abstains (as any other
  wrong-shaped score does) instead of fabricating a score from a bool
  (`kb/wiki/council.py`).
- **The most-active-branch scan could silently drop a real branch whose name
  merely contained the substring "HEAD"** (e.g. `release/HEAD-fix`), not just
  the `origin/HEAD` symbolic ref it was meant to filter — a substring check
  (`"HEAD" in line`) over the whole `git for-each-ref` line, rather than an
  exact match on the ref name. `select_most_active_branch`/`branches` could
  then pick a less-active branch, or (if the affected branch was the only
  one) report "No branches found" (`core.py`).
- **`contextlake branches`' own fetch could report a deleted/access-revoked
  upstream project as a generic error, inconsistent with `update`.** A prior
  fix classified this condition as a clean skip in `update_repository`'s
  fetch path only; `switch_repository_branch`'s `git fetch --all` (the
  `branches` command's own fetch, hitting the same origin) still reported it
  as an undifferentiated `("error", ...)`. Now applies the same
  `classify_error(...) == "project-deleted"` check (`core.py`).

## [2.48.1] - 2026-07-26

### Fixed

- **`statediagram` extraction (v2.48.0) could emit a false transition, not just an
  undercount.** An independent review of the guard→assignment regex found five
  reproducible cases where the lazy gap between a guard and its assignment could
  cross into an `else`/`elif` sibling branch, a different method, or past a second,
  unrelated guard on the same field — asserting a transition the code doesn't
  actually establish, contradicting this module's own "never a false transition"
  contract. Also fixed: `self.status = other.status` (a field copy) synthesizing a
  bogus state literally named `status`. The guard→assignment span is now rejected
  if it contains a boundary keyword (`else`/`elif`/`def`/`class` for Python, a `}` /
  `function`/`class` for JS/C#) or a second mention of the same receiver+field, and
  a transition value matching the field name itself is dropped (`kb/flow/state.py`).
- **Dataflow extraction (v2.48.0) read commented-out and docstring-quoted SQL as a
  live `reads`/`writes` edge.** A `# DELETE FROM orders` left in a data-access file,
  or a docstring like `"""...INSERT INTO audit_log..."""`, asserted a data
  dependency that doesn't exist — the same "never a false edge" contract this
  extractor documents. Line comments (`#`, `//`), block comments (`/* */`), and
  triple-quoted strings are now blanked out (newlines preserved, so line numbers on
  real matches don't shift) before scanning (`kb/flow/data.py`). Trade-off, by design
  (honest undercount over false positive): a real query written as a triple-quoted
  string (`query = """SELECT * FROM orders"""`) is now missed too, same as the
  existing undercount for ORM/string-concatenation queries.
- **The fleet architecture map and generated site could render a shared-node
  sentinel (`"(shared)"`, `"(packages)"`) as though it were a repo.** Since Finding
  #10 (v2.48.0) gave shared nodes their own stable `repo_id`, that id showed up in
  the raw `GROUP BY repo_id` node-count query the fleet overview and site builder
  use to enumerate repos — and `"(shared)"` (every module imported fleet-wide) is
  now the single largest bucket, ranking first and potentially displacing a real
  repo when the fleet exceeds `max_nodes`. A new `repo_node_sizes()` helper filters
  sentinel ids out at the one query all three call sites (`overview_subgraph`,
  `build_site`, `build_site_server`) and the dashboard's embedded graph pages share
  (`kb/visualize.py`, `kb/dashboard/server.py`). `"(packages)"`/`"(external)"` are
  now named constants (`PACKAGES_REPO`/`EXTERNAL_REPO` in `kb/model.py`) alongside
  `SHARED_REPO`, replacing the bare string literals at their four call sites.
- **Graph views: a harmless but noisy console warning on every node, on first render.** Cytoscape's
  style sheet maps node width/height from a `deg` (degree) data field, but `deg` was only set
  client-side in a `.forEach()` that ran *after* the graph's first style pass — so every node
  logged "no mapping for property... try a `[deg]` selector" before silently correcting itself.
  `deg` is now computed once server-side (`kb/visualize.py:_cytoscape_elements`, mirroring the
  existing `weight`-always-present pattern for edges), present from the very first render.
- **The release gate never actually ran the knowledge-layer test suite.** `release.yml` installed
  only the core package and ran `pytest --ignore=tests/kb`, mirroring CI's "core (no knowledge
  layer)" job rather than its "knowledge-layer" job — so a broken `tests/kb/*` test could tag and
  ship a release. Found the hard way cutting v2.48.0: a `tests/kb` test had been misplaced one
  directory too high (outside `tests/kb/`), so it ran under the core-only gate and failed there
  instead — catching the mistake by coincidence, not because the gate covered `tests/kb`. The
  actual `tests/kb/*` suite still never ran in `release.yml`. Now installs the `[kb]` extra and
  runs the full suite, matching CI's knowledge-layer job.

## [2.48.0] - 2026-07-26

### Added

- **`statediagram` graph format.** `contextlake graph --repo <repo> --format statediagram` renders a
  Mermaid entity state machine from guarded assignments to a status/state/stage field — `if
  order.status == Created: order.status = Paid` becomes a labeled transition. Only *guarded*
  transitions are emitted (the source state must be established by a preceding comparison on the
  same field), so a diagram never claims a transition the code doesn't actually establish — an
  honest undercount, never a guess. Regex-based, Python/JS·TS/C# (`kb/flow/state.py`), every edge
  `INFERRED`, same stance as the existing HTTP/event flow extractors.
- **Intra-repo dataflow: `reads`/`writes` edges from code to the tables/views it queries.** A literal
  `SELECT ... FROM` / `INSERT INTO` / `UPDATE ... SET` / `DELETE FROM` in any file becomes a `reads`
  or `writes` edge to the matching `table`/`view` node the SQL DDL extractor already found —
  resolved by name repo-wide, the same mechanism an FK `references` edge uses, so a query against a
  table this repo never defines is an honest miss, not a guessed link (`kb/flow/data.py`).
  `reads`/`writes` are now in `impact`'s default relation set, so `contextlake impact <table>`
  answers "what code touches this table" without needing `--relation reads,writes` spelled out.
- **Dashboard: the symbol breadcrumb continues to Diagram / Wiki / Links.** Viewing a symbol's blast
  radius now shows `repo → symbol → Diagram → Wiki → Links`, one click each to that symbol's
  repo-scoped architecture graph, curated wiki, and connector links — Wiki/Links only appear when the
  repo actually has one, never as a dead crumb for data that doesn't exist.

### Fixed

- **A deleted (or access-revoked) upstream GitLab project is a clean `update` skip, not an
  error.** Previously bucketed into the generic error count alongside real failures; now
  classified like the existing deleted-branch case, so a project marked for deletion upstream
  no longer inflates `N errors` in the run summary.
- **Dashboard: the symbol view's "Cross-repo only" toggle was silently dead in live mode.** It
  worked only against the static demo snapshot, because `/api/impact` never told the client which
  repo the seed symbol lived in — the client tried to look it up in a symbol index that only exists
  in static mode. `impact()` now returns the seed's own `repo` (`kb/dashboard/data.py`), fixing the
  toggle in live mode and, incidentally, a symbol-view provenance chip that was citing the wrong
  "repo" (actually the raw node id, via a `.split(":")` on an id that never had a colon in it).
- **Finding #10: shared nodes (`module`/`endpoint`/`topic`, and — found while fixing this —
  `package` and connector nodes) had their `repo_id` silently overwritten by whichever repo's index
  run touched them last, and could be deleted out from under another repo entirely.** Root cause:
  `SqliteStore.upsert_nodes` stamped every node in a shard's batch with that shard's own `repo_id`,
  ignoring each `Node`'s own `.repo` field — so a "requests" module imported by two repos flipped
  owner on every reindex, and `clear_repo` on whichever repo it currently pointed at deleted the
  node (and any other repo's still-live edges into it) as collateral damage. `upsert_nodes` now
  writes each node's own `.repo`; extractors for `module`/`endpoint`/`topic` nodes now set it to a
  new `"(shared)"` sentinel (`kb/model.py`), matching the existing `"(packages)"` / `"(external)"`
  pattern for nodes no single repo owns — per-repo attribution for these already lives correctly on
  their *edges* (`arch/resolve.py` has always read it from there, never from the shared node itself).
  **No schema migration needed, but self-correction requires an actual re-parse, not just re-running
  `index`**: `index`'s incremental skip (a repo whose HEAD hasn't moved since its last index is left
  untouched) means a shared node keeps a stale owning repo from before this fix until the repo that
  currently owns it is actually re-parsed — a new commit landing, or `index --force`. Run `--force`
  once after upgrading if you want existing shared nodes corrected immediately rather than opportunistically.
- **Dashboard: a shared node's breadcrumb no longer links to a repo that doesn't exist.** Search
  for a symbol like an imported module, an HTTP endpoint, or an event topic and trace its blast
  radius (reachable from any search result's "Blast" button) and its owning "repo" is a pseudo-repo
  like `"(shared)"` (see the Finding #10 fix above) — the breadcrumb previously still tried to link
  `Diagram`/`Wiki`/`Links` to `#/repo/(shared)`, which resolves to nothing. Now treated the same as
  "repo unknown": those crumbs are omitted, matching the existing rule that an absent wiki/link is
  never shown as a dead crumb (`kb/dashboard/static/dashboard.js`).
- **An explicit `--config`/`--kb-config` path that doesn't exist is now a hard error, not a silent
  fall-through.** `load_kb_config` previously treated a missing config path exactly like an absent
  auto-discovered file (empty, keep going down the precedence chain) — so a typo'd or not-yet-created
  `--config` path silently landed on the next file in the chain, typically the real
  `~/.contextlake/kb.toml`, pointing at a completely different (possibly production) store than the
  one intended. A missing explicit path now raises `ConfigError` with a clear message instead; the
  auto-discovered files in the chain are unaffected (still silently optional, by design).

## [2.47.0] - 2026-07-25

### BREAKING

- **`repo_id` is now canonical (derived from the repo's git remote), not the path
  relative to `--workspace`.** The old scheme meant the same physical repo got a
  different id from a different index root — duplicate ids, broken path-based
  owners/graph/impact views. The canonical id survives being re-cloned elsewhere or
  indexed from a different workspace root. Two local checkouts of the same remote
  (e.g. a stale pre-reorg clone left alongside its replacement — a real pattern
  found in this project's own fleet) collapse to one repo, keeping whichever is
  more recently committed; the dropped checkout is logged, never silently skipped.
  A repo with no remote falls back to a stable `dirname@root-commit-hash` id.
  **Migration is automatic**: the next `index`/`bootstrap` run on an existing store
  detects any repo still under the old id, clears its old row/shard/vectors, and
  re-derives it fresh under the canonical id — same as a first index, no manual
  step. **This re-parses every existing repo once** (their content doesn't change,
  only their id), and **clears embeddings for migrated repos** — re-run `embed`
  afterward if semantic search is enabled. Verified against this project's real
  678-repo store (detection pass, full scale) and a real multi-repo slice
  (full detect-clear-reindex pipeline).

## [2.46.0] - 2026-07-25

### Added

- **`graphql` source type for `ingest`.** POSTs a query (+ optional variables) to one
  endpoint and maps records in the response to documents, the same shape as the
  existing `api` source's REST mapping. Auth is a bearer token read from an env var
  named in config, never stored.
- **`.vscode/mcp.json` steering output.** `contextlake steer` now writes VS Code's own
  MCP config file (top-level `servers` key, a different schema from `.mcp.json`'s
  `mcpServers`) alongside the existing steering files, merging in the `contextlake-kb`
  server entry without disturbing any other servers already configured there.
- **`sequencediagram` graph format.** `contextlake graph --node/--name/--search ID
  --format sequencediagram` renders a Mermaid call-order trace from one seed function,
  walking `calls` edges depth-first and ordering each caller's callees by call-site
  line. No new extraction was needed — every `calls` edge already carried its source
  line — so this is a renderer over data already collected, not a new parser pass.

### Fixed

- **Docs corrected a false claim that Devin reads the same repo-committed MCP config
  file as Windsurf.** Devin's MCP connections are account/org-level (`mcp.devin.ai`,
  API key + org header); contextlake cannot self-register there the way it can for
  file-based clients. `docs/serve.md` now says so plainly instead of grouping Devin
  with Windsurf's wiring instructions.
- **Deterministic lowest-line dedup for repeated `calls`/`inherits` references.** A
  call site hit twice from the same caller could surface an arbitrary (not
  necessarily first) line, since tree-sitter's capture order isn't guaranteed to
  match source order. References are now sorted by line before resolution.

## [2.45.1] - 2026-07-25

### Fixed

- **A local config file's `[llm]`/`[kb]`/`[embeddings]` table no longer wipes out
  sibling fields set globally.** Those three tables are now deep-merged key-by-key
  across the precedence chain; a `.contextlake.kb.toml` setting only `[llm] model`
  used to silently disable a globally-enabled LLM tier (`enabled`/`provider` reverted
  to their defaults) because the table was replaced wholesale. `sources`/`rules` keep
  their documented wholesale-replace behavior (they are list tables, not scalar ones).
- **Readable terminal output while a long run is in flight.** The live progress bar
  (stderr) and per-item status lines (stdout) share one terminal cursor, so every
  frame was left on screen with the next status line welded to its right edge. The
  bar is now erased before each log line and repainted after, and it erases to
  end-of-line instead of padding out to the terminal width.
- Per-item status lines are clamped to one row on a terminal (long ids elide from
  the middle, the reason stays whole) so they cannot wrap through the bar. Piped and
  redirected output is left unclamped, where full ids matter and there is no bar.
- Git's three-line "ambiguous argument 'HEAD'" usage hint is reported as
  `No commits yet (empty repository)` instead of being dumped into the status line.
- No ETA is shown until there is enough signal, and it is derived from the cumulative
  rate: a single early completion used to produce confidently wrong estimates that
  swung between seconds and tens of minutes.
- `Update complete:` / `Clone complete:` / `Branch switch complete:` no longer lose
  the space before their counts.

## [2.45.0] - 2026-07-25

### Fixed

- **`doctor` flags a missing wiki-LLM runtime.** When the built-in wiki LLM is
  configured but `llama-cpp-python` isn't installed, `doctor` now reports
  `⚠ … runtime not installed` with the install hint, instead of a green `✓` that only
  checked for the model file — so the report matches what `wiki` will actually do.
- **Dashboard favicon.** The dashboard shell ships an inline SVG favicon, so browsers
  no longer log a `/favicon.ico` 404 (and the tab gets an icon).

### Changed

- **Moonlit-navy dark theme** for the dashboard and the graph visualizer.
- Renamed the demo running example to a generic "Catalog" service across the sample
  fixture, examples, and docs.
- Stripped em-dashes from user-facing copy (house style).

## [2.44.0] - 2026-07-23

### Added

- **Composed namespace C4 diagram.** `contextlake graph --c4 [--group-depth N]`
  renders a C4-Context/Container view over already-extracted graph data: namespaces
  as boundaries, repos as containers, and aggregated `depends_on`/HTTP/event `flow`
  edges as the labeled inter-service connections (e.g. `http x3`). Fully offline, no
  new extraction; output as `html` (default, interactive, `<store>/graphs/c4.html`),
  `dot` (Graphviz clustered), or `json`. Mermaid/classdiagram output and `--serve`
  are not supported for `--c4`.
- **Consistent CLI progress line.** `wiki`, `index`, `embed`, and the mirror-tier
  `clone`/`update`/`branches` now share one progress renderer: a live bar (done/total,
  percent, elapsed, ETA, rate) on stderr, degrading to periodic summaries when not a
  TTY, so stdout redirects (e.g. `>> run.log`) stay clean of bar/`\r` artifacts.

### Changed

- **Consistent CLI presentation across every command.** One status vocabulary
  (`✓` ok, `⚠` warn, `✗` fail, `⊘` skip, `=` unchanged, `↝` switched, `~` dry-run) now
  covers the mirror tier and every other command; `bootstrap` and `sync` both show
  `▶ <Phase>` section headers; every long-running command ends with a glyph-prefixed
  summary line. `contextlake serve --transport http` now logs its bind URL, and
  `graph --overview` on an empty store warns with a "run `contextlake index` first"
  hint instead of silently reporting a written artifact. Per-item detail lines across
  every long-running command (mirror-tier `clone`/`update`/`branches`, `index`, `embed`,
  `wiki`, `connect`, `ingest`, `enrich`) no longer flicker a right-aligned clock.

## [2.43.0] - 2026-07-22

### Added

- **Fleet / namespace-level wiki.** `contextlake wiki --namespace <prefix>` (or
  `--namespaces --depth N`) generates a cluster wiki page for a whole group of repos,
  narrating how they fit together: which services call which over HTTP, publish/consume
  which events, and share which packages, split into coupling within the namespace and
  coupling to repos outside it. It grounds strictly in the cross-repo edges the graph
  already resolved (no new extraction), reuses the per-repo wiki's review council +
  provenance footer (advisory and cited), and says so rather than inventing a link when
  the graph shows no coupling. Cluster pages are served over MCP by passing a namespace
  to `get_wiki`, and shown per group in the dashboard's fleet overview.

### Changed

- **`query` now points at semantic search when a natural-language phrase finds no
  keyword matches.** A multi-word query with no FTS hit gets a one-line hint (run
  `contextlake embed`, then use serve's `semantic_search` / `ask` tools) instead of
  a bare "No matches"; a single-token symbol lookup stays quiet.

## [2.42.0] - 2026-07-21

### Fixed

- **Indexing config keys are now honored.** `[kb] skip_generated`, `max_file_bytes`,
  and `index_workers` were documented but silently ignored (the loader read only
  `store_dir` and `languages`), so they always used defaults. They are now loaded from
  `kb.toml`.
- **Vendored nested repos are skipped in discovery.** An upstream clone carried inside
  the mirror with its own `.git` under a `module-federation` path segment was indexed as
  a full repo, flooding the global graph with upstream-demo nodes. Such repos are now
  skipped, and each skip is logged.

### Added

- **Unknown config keys are warned, not silently ignored.** An unrecognized `[kb]` key
  or config table (e.g. `store` for `store_dir`) now logs a warning instead of being
  dropped without a trace.
- **`owners` and `graph` suggest close repo ids** when given an id that is not in the
  store, including the workspace-relative-prefix case (a sub-workspace-indexed
  `team/billing/api` points at the stored `acme/team/billing/api`), instead of
  a bare error or a silently empty view.

## [2.41.0] - 2026-07-21

### Added

- **React Router data-router (object form) extraction.** `createBrowserRouter`,
  `createHashRouter`, and `createMemoryRouter` route arrays now surface as `route`
  nodes, joining the flat JSX `<Route>` form from 2.39.0. It reuses the tree-sitter
  AST walk built for Angular, anchored on the `create*Router` call's array argument
  so bare `{path:...}` objects are never mis-read as routes. Nested `children`
  compose into full paths, `index: true` resolves to the parent path, and a
  `Component`/`element` is captured when it names a plain component. Deferred:
  `loader`/`lazy` and `createRoutesFromElements`.

## [2.40.0] - 2026-07-20

### Added

- **Angular route extraction (tree-sitter AST).** Angular `Routes` tables now
  surface as `route` nodes, joining the Next.js and React Router extraction from
  2.39.0. It walks the TypeScript AST (not regex) anchored on the route-table
  container (a `Routes`-typed declaration, or an inline `forRoot`/`forChild`/
  `provideRouter` array), so nested `children` compose into full paths and a bare
  `{path:...}` config object is never mis-read as a route. `path: ''` index routes
  fold into the parent, `redirectTo` routes are skipped, `**` maps to the catch-all
  token, and lazy `loadChildren` captures the mount path (the child module is a
  future release). Only TypeScript files that mention Angular routing are re-parsed.

## [2.39.0] - 2026-07-20

### Added

- **Web-topology: frontend route extraction.** Indexing now surfaces frontend
  **routes** as embeddable, repo-scoped `route` nodes from **Next.js App Router**
  page files (the `app/**/page.*` path convention, with route groups `(name)`
  dropped and dynamic `[id]`/`[...slug]` collapsed) and **React Router v6** flat
  JSX `<Route path=...>`, so "what routes does this app define" and "where is
  `/dashboard`" are queryable. Angular route tables, the `createBrowserRouter`
  object form, and Luigi navigation configs need AST parsing and are skipped for
  now rather than mis-captured (a later release adds them).
- **Next.js API route handlers as endpoints.** `app/**/route.ts` files that export
  `GET`/`POST`/etc. are now recognized as HTTP `endpoint` nodes (path from the file
  convention, verbs from the exports) and join the existing cross-repo HTTP flow;
  previously the HTTP extractor only knew Express/FastAPI/ASP.NET and missed them.

## [2.38.0] - 2026-07-20

### Added

- **`contextlake source` command family for managing connectors.** `source add|list|remove|test|enable|disable`
  let you manage knowledge-source connectors (Atlassian, Figma, GitLab) without hand-editing `kb.toml`.
  The CLI is guided by default (interactive prompts) and fully flagged for scripting. `list` and `test`
  show the effective merged config and per-source reachability; `add`/`remove`/`enable`/`disable` mutate
  the config while preserving comments via tomlkit, a new `[kb]` extra dependency. `init` can prompt to
  connect a source during first-run setup, and `doctor` reports per-source reachability as part of its
  environment check. Hand-editing `kb.toml` still works for power users.
- **MCP tool-calling connector for external search.** An `mcp` source can now declare a search *tool*
  (not just read resources) and template codebase-derived terms (repo name, key symbols) into the tool's
  arguments via `tool` and `arg_template` keys. Supports both stdio (`command`/`args`) and streamable-HTTP
  (`url`) transports. Groundwork for query-driven wiki enrichment in the upcoming `enrich` stage.
- **`contextlake enrich`: query connected sources with codebase-derived terms.** Derives search terms
  from each repo's code graph (repo name and top symbols) and queries connected sources (Atlassian Rovo
  search, or any `mcp` source with a `tool` and `arg_template`), storing the results in a searchable,
  embedded `@enrich:<repo>` partition. Idempotent and re-runnable across the whole fleet. Results are
  embedded and surface in semantic search (as `document` nodes tagged with their source), groundwork for connector-
  enriched wiki pages in the next stage.
- **The curated wiki now incorporates connector enrichment.** After `contextlake enrich` completes,
  each repo's wiki page gains an "External context" section drawn from its `@enrich:<repo>` enrichment
  documents (Confluence pages, Jira issues, MCP search results). Each external claim is directly quoted
  and attributed to its source, never presented as a free assertion or undisclosed code fact; the enriched
  page still passes through the verification council before being written.
- **`contextlake bootstrap` now runs the `enrich` stage**, so `init` plus `bootstrap` takes a blank
  workspace to a mirrored, indexed, embedded, connector-enriched, wiki'd, editor-wired workspace in one
  command (skip enrichment with `--no-enrich`). A documented command-composition matrix shows every
  supported flow (blank-to-enriched, single-repo, add-a-connector-refresh, etc.), so users build exactly
  what they need by chaining the right stages.

## [2.37.0] - 2026-07-08

### Added

- **`pom.xml` is now indexed** into the cross-repo dependency graph (Maven ecosystem):
  the project's `groupId:artifactId` becomes a `publishes` edge and each `<dependency>`
  a `depends_on` edge, linking Java/Maven repos through shared package nodes, the same
  way `pyproject.toml`/`package.json`/`.csproj` already do.
- **Terraform/HCL is now indexed** into an infrastructure dependency graph: `.tf` files
  index `resource`/`data`/`variable`/`output`/`module`/`local` definitions and resolve
  `var.`/`module.`/`data.`/resource references into `depends_on` edges (cross-file
  within a repo). `resource` nodes are semantically searchable. The grammar
  (`tree-sitter-hcl`) ships in the `[kb]` extra.
- **SQL DDL is now indexed** into a referential graph: `.sql` files index `CREATE TABLE`/
  `VIEW`/`PROCEDURE` as `table`/`view`/`procedure` nodes and resolve foreign-key
  `REFERENCES` clauses into `references` edges (cross-file within a repo). `table` and
  `view` nodes are semantically searchable, and FK dependents surface in `blast_radius`.
  It is regex-based (the fleet's T-SQL/PL-SQL defeats a tree-sitter AST), so no new
  dependency is needed.
- **Kotlin is now indexed** as a tree-sitter code language (`.kt` and `.kts` files):
  classes, objects, interfaces, enums, functions, methods, imports, and the inferred
  call graph are extracted; inheritance edges are captured via `delegation_specifier`
  (extending and implementing base classes). The grammar (`tree-sitter-kotlin`) ships
  in the `[kb]` extra.

### Fixed

- **Tolerant wiki review-score parsing.** The council reviewer now recovers a numeric
  score from prose or alternate-JSON review responses before abstaining, so capable
  models whose review text is not strict JSON no longer trigger spurious "unparseable
  review" rejections. The fallback is scoped to unparseable-JSON responses only;
  genuinely score-less prose still abstains.

## [2.36.0] - 2026-07-08

### Added

- **Selectable LLM backends for the wiki and council tier.** `provider = "anthropic"`
  (native Messages API, stdlib-only) and `provider = "cli"` (shell out to a local
  `claude`/`gemini`/`codex` you already pay for, no API key held by contextlake). Gemini
  works today via the existing OpenAI-compatible client (`provider = "openai"`,
  `base_url = ".../v1beta/openai/"`). `doctor` reports each backend's key/PATH readiness.

### Documentation

- **Install-flag guidance + scenario cheatsheet.** QUICKSTART now documents `-U`,
  `--only-binary :all:` (wheels-only, for compiler-less / brand-new-Python machines), and
  `--extra-index-url`, with a "your situation → exact command" table (mirror-only, zero-config
  kb-full, upgrade, no-compiler Python 3.14, Docker-no-toolchain). The built-in-LLM wheel
  section gains the `--only-binary :all:` guard alongside the existing CPU-wheel index.
- **Config & flag reference completeness.** Documented previously example-only keys in the
  narrative docs (so they reach the website): `[embeddings] vector_backend` + `batch_size`,
  `[[sources]] auth_dir`/`mcp_command`/`group`/`per_page`, the `dashboard --group-depth` flag,
  and `clone_method`/`branch_strategy` rows in the usage settings table.
- **"Reading the console output" guide.** A knowledge-layer section decoding the runtime
  lines users puzzle over: the `▶` phase headers, `0 nodes, 0 edges` (config/doc-only repos),
  the incremental `already up to date` embed counts, the `Fetching 10 files … 0.00B` cached
  model-load bar, and the `✓ written` / `⚠ rejected by council` / `unparseable review` wiki lines.

## [2.35.0] - 2026-07-08

### Added

- **Rust, Ruby, PHP, and Scala are now indexed** too. Rust (functions, structs, enums,
  traits, `use` imports, calls); Ruby (classes, modules, methods, calls, `<` inheritance);
  PHP (classes, interfaces, traits, enums, functions, methods, `use` imports, calls,
  `extends`/`implements`); Scala (classes, objects, traits, methods, calls, `extends`).
  The parser now covers **13 languages**. (`.rs .rb .php .scala/.sc`.) Kotlin was
  evaluated but deferred — the available tree-sitter grammar is too inconsistent to index
  reliably (superseded, see the Unreleased section).
- **Go, Java, C, and C++ are now indexed.** Four more tree-sitter grammars: Go
  (functions, methods, struct/interface types, imports, calls); Java (classes,
  interfaces, enums, records, methods, constructors, imports, calls, full inheritance);
  C (functions, structs, enums, unions, `#include`s, calls); C++ (classes, structs,
  enums, functions, in-class methods, `#include`s, calls, `: public Base` inheritance).
  Brings the parser to Python, JS/TS(X), C#, Go, Java, C, C++ — covering .NET (C#),
  Node/React/Next/Angular (JS/TS), and native code. (`.go .java .c .h .cpp/.cc/.cxx
  .hpp/.hh/.hxx`.) A shared `_def_node` normalization keeps call-attribution and
  containment correct where a language nests the name under a declarator (C/C++).
- **`contextlake hook install` — continuous intelligence.** A git `post-commit` hook
  that re-indexes a repo into the store after each commit, so the graph never drifts
  from HEAD without a manual `index`/`bootstrap`. `install` (single repo or
  `--workspace` across a whole mirror) / `uninstall` (restores any pre-existing hook) /
  `status`. Re-uses the repo's stored id so it updates the same node, never a duplicate;
  runs detached so commits don't block.
- **Store single-writer lock.** Two contextlake writers on one store race on SQLite and
  can interleave shard writes. `index` / `embed` / `wiki` now take an advisory lock
  (`<store>/.contextlake.lock`) and refuse to run when a *live* peer holds it, with a
  clear message naming the holder — while transparently reclaiming a lock left by a
  crashed process. Override (rarely correct) with `CONTEXTLAKE_ALLOW_CONCURRENT=1`.
- **Configurable wiki-LLM `timeout`.** `[llm] timeout` (seconds, default 300) is now
  honored by the `ollama` and `openai` providers, so a slow CPU box can raise it instead
  of every page failing silently at the hardcoded 5-minute per-call limit. Surfaced
  while measuring wiki quality: a 1.5B–3B Ollama model on a **CPU-only** host (~0.85–1.7
  tok/s, no GPU) exceeds 300s per page.

### Changed

- **Quieter, less alarming model downloads.** Downloading the built-in model (LLM or
  embedder) used to print two Hugging Face notices — a `local_dir_use_symlinks`
  deprecation and "You are sending unauthenticated requests to the HF Hub…" — that can
  read, on a local-first tool, like outbound data transfer. They are not: the model is
  downloaded *to* your cache, nothing is uploaded. Both are now silenced (the real
  download progress still shows).
- **More patient, resumable mirror on a network drop.** The GitLab enumeration now
  retries up to 6 times (≈1+2+4+8+16s of backoff) so it rides out a brief VPN/proxy
  reconnect. If it still can't reach GitLab, `bootstrap` prints a clear
  network-drop notice, builds the knowledge layer from the repos already on disk, and
  tells you the exact idempotent command to re-run once the connection is back — nothing
  is lost, the mirror just resumes.

### Fixed

- **Wiki reviews without a usable score now abstain instead of scoring zero.** Small
  local models (e.g. the built-in 0.5B) sometimes return a review the council can't score
  — either malformed JSON *or* valid JSON in the wrong shape (no `score` field). That
  lens was counted as 0, dragging an otherwise-good page below the accept threshold and
  rejecting it (`rejected by council (score 0.657)` — "unparseable review"). Any review
  we can't extract a numeric score from is now excluded from the mean; a page is rejected
  only if *no* review scored. Far fewer good pages lost to a flaky reviewer.
- **`[llm] council_size` is now applied.** It shipped in the example config and was
  documented as tunable, but `council_gate` always ran all three review lenses. It now
  trims to `council_size` lenses (1–3), so fewer reviews = fewer model calls per page.

### Documentation

- **Detailed wiki + LLM-provider docs.** knowledge-layer.md now covers: per-provider
  `[llm]` config with a model-id table; **why the built-in LLM needs a prebuilt wheel or
  a compiler** (native `llama.cpp` bindings; PEP 508 can't pin an index; PyPI lags new
  Pythons); **using Ollama for the wiki**, including the WSL↔Windows-host networking
  gotcha (mirrored networking or `OLLAMA_HOST=0.0.0.0` + the default-route gateway IP);
  and a measured **model-vs-hardware** quality note (built-in 0.5B vs Ollama on CPU vs
  GPU vs API).

## [2.34.0] - 2026-07-07

### Added

- **`bootstrap --llm PROVIDER` (and `--llm-model`).** `bootstrap` already ran the
  wiki stage, but it had no way to turn on the LLM tier, so on a fresh setup the wiki
  step silently no-op'd unless you had pre-enabled `[llm]` in `kb.toml`. Now
  `contextlake bootstrap --llm builtin` builds the whole knowledge layer — graph,
  vectors, **and** wiki — in one command (`builtin` = local CPU model;
  `ollama` | `openai` | `auto` also accepted). Point `store_dir` at a workspace folder
  and everything lands in one place. The pre-command form (`--llm builtin bootstrap`)
  kept working throughout; this adds the natural post-command form.

### Changed

- **Clearer built-in-LLM install error.** When the `llm-local` extra is missing, the
  error now also gives the prebuilt-CPU-wheel fallback (`pip install llama-cpp-python
  --extra-index-url .../whl/cpu`) for Pythons without a wheel or a compiler (e.g. 3.14),
  instead of only re-suggesting the `pip install 'contextlake[llm-local]'` that just
  failed. Same fallback documented in QUICKSTART + the knowledge-layer model-providers
  section.

### Fixed

- **`find_callers` and `blast_radius` accept a bare symbol name.** Agents call these
  MCP tools with a name (e.g. `CatalogService`), but they only accepted an internal
  node id, so a name silently returned nothing even when the graph had the answer
  (only the `ask` router resolved names). Both now resolve a name to its first
  matching definition. Surfaced while benchmarking MCP token cost on a 1M-node fleet.

### Documentation

- **Benchmarks page.** An honest, measured look at what connecting the contextlake
  MCP saves (new-code grounding, search, maintenance) with methodology and caveats.
- **Benchmarks: generation-token nuance.** Refined the "does not reduce generation
  tokens" claim — a single *correct* generation is irreducible, but across a whole task
  contextlake cuts *total* generation by avoiding failed regenerations and reinvented
  code. Added a ranked "Does it cut generation tokens?" section, explicitly marked a
  mechanism argument, not a measured figure.

## [2.33.2] - 2026-07-06

## [2.33.1] - 2026-07-06

### Fixed

- **`init` now recommends the extra that matches your choice.** If you enable
  semantic search during `contextlake init`, the "Next" hint recommends
  `contextlake[kb-full]` (which ships the built-in embedder) instead of plain
  `[kb]` — previously it suggested `[kb]`, so the very next `bootstrap` embed
  step failed for every repo because no embedder was installed.
- **`embed` fails fast on an unavailable embedder.** A whole-environment problem
  (missing `kb-local` extra, unreachable Ollama/API) is now detected once by an
  up-front readiness probe and reported with a single actionable message, instead
  of repeating the same error for every repo in the fleet.
- **Empty repositories no longer count as branch-switch errors.** A freshly-cloned
  repo with no commits (git: "ambiguous argument 'HEAD'") is now skipped cleanly
  as "Empty repo (no commits)" rather than reported as an error.

### Documentation

- **Update & uninstall guides.** The quickstart and README now document how to upgrade
  contextlake in place (pipx / pip / uv / Docker) and how to uninstall it and, optionally,
  remove the local store, config, mirror, and cached models — noting that nothing is ever
  written inside your repositories.

## [2.33.0] - 2026-07-06

### Added

- **`--repos` — mirror and index just a subset.** Every mirror command, plus
  `bootstrap` and `index --workspace`, now accepts `--repos PATTERN`, a comma-separated
  glob/substring filter over repo paths (e.g. `--repos "team/api,billing,frontend/*"`).
  `fetch` narrows the cached project list, so `clone` / `update` / `branches` /
  `verify` / `status` / `bootstrap` all scope to that set; `bootstrap` / `index
  --workspace` also filter which repos get indexed. Perfect for a demo or a
  try-before-fleet run — `contextlake bootstrap --repos "…"` goes from nothing to a
  wired workspace over just the chosen repos.

### Changed

- **`embed` now vectorizes only meaningful nodes** — code definitions (class /
  function / method / interface / struct / enum) and HTTP endpoints — and skips file,
  module, package, and topic nodes. A file path or a shared package name carries
  little semantic signal, and the shared cross-repo nodes were being re-embedded once
  per referencing repo, inflating the "vectors written" count (it now matches the
  store total) and diluting search results. Eval-gated: no relevance regression on the
  golden-query harness; semantic search returns cleaner definition hits. Found while
  dogfooding a full multi-repo `bootstrap`.

### Added

- **`ask` answers "what extends X?"** A new `subclasses` route makes the inheritance
  graph queryable in natural language: `ask("what extends BaseController")`,
  `ask("who implements Store")`, `ask("subclasses of Embedder")` resolve the base type
  and return the classes/interfaces with an incoming `inherits` edge — cited graph
  facts, not a fuzzy search. (Surfaced by dogfooding `ask` on a real 800-node codebase,
  where inheritance questions previously fell through to semantic search.)

### Fixed

- **Text-format graph output is no longer log-polluted.** Streaming a large graph to
  stdout as `--format json` / `dot` / `mermaid` / `classdiagram` could prepend a
  timestamped log line (e.g. the node-truncation warning) to the payload, producing
  invalid JSON or a Mermaid diagram that starts with a stray line. Logs now switch to
  stderr up front whenever a text format is streamed to stdout, so the payload on
  stdout is always clean. Found by generating a class diagram for a real 800+-node
  package.

## [2.30.0] - 2026-07-06

### Added

- **Class diagrams — `graph --format classdiagram`.** Now that the graph carries
  inheritance, `contextlake graph --repo <r> --format classdiagram` renders a Mermaid
  **UML class diagram**: classifiers (class / interface / struct / enum) with their
  methods as members (signatures included), `<|--` for extends and `<|..` for interface
  implements, and an `<<interface>>` stereotype. Files and call/import edges are
  dropped so it reads as a class view, not the flat relation graph. Paste it straight
  into a PR or design doc. (Payloads now also carry each node's `signature`.)

### Added

- **Inheritance graph — `inherits` edges.** The code parser now extracts class
  inheritance and interface implementation across all four languages (Python bases,
  JS/TS `extends` + `implements`, C# base lists), resolved repo-wide like calls
  (INFERRED for a unique base, AMBIGUOUS when a base name matches several, external
  bases dropped). So "what extends `BaseController`?" is a single `get_neighbors`
  hop, and `blast_radius` now includes `inherits` by default — changing a base class
  surfaces its subclasses as impacted. This is also the extraction prerequisite for
  class diagrams.

## [2.28.0] - 2026-07-06

### Changed

- **`ask`'s explain route degrades usefully.** When a question like "explain the
  catalog-api" hits a repo with no generated wiki, `ask` now returns that repo's
  grounded anatomy (top symbols, packages, languages) from the graph instead of a
  blind semantic search — a structured `brief` beats fuzzy hits for "explain this."
  (Surfaced by a full end-to-end test sweep of the CLI + MCP server, which otherwise
  found no defects.)

## [2.27.0] - 2026-07-06

### Added

- **`ask` — one MCP tool, natural language, auto-routed.** A small-context IDE agent
  no longer has to pick among twenty graph tools: `ask("who calls charge_order")`,
  `ask("what breaks if I change CatalogService")`, `ask("explain the catalog-api")`. A
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
  `.contextlake.ini`, `.contextlake.kb.toml`) so a local `docker build .`
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

- **Local development hygiene.** Secret and machine-specific tokens used by local
  pre-publish checks are read from the environment or a git-ignored file, never
  committed to the repository.
- **Genericized example figures in the docs.** The example `status` output and the
  overview-feature notes use illustrative values.
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

- Broadened the local pre-publish checks to cover the whole tree (`docs/`,
  `examples/`, `.github/`, and every top-level doc), not just `src/`.

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
private data lives in the package.

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
  deployment-specific facts live here, never in the package.
- CI now runs a separate knowledge-layer job (Python 3.10-3.13) alongside the
  core job.

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
