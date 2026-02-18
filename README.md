# gitlab-sync

> **Mirror every GitLab repo you can touch — cloned, current, and on the branch that's actually alive.**

[![CI](https://github.com/sayak-sarkar/gitlab-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/sayak-sarkar/gitlab-sync/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

You have access to dozens — maybe hundreds — of repositories scattered across a
GitLab group and its subgroups. You want them all on your laptop, in the same
shape they have on GitLab, each sitting on the branch where the real work is
happening, and you want a single command to keep it that way.

That's the whole job. `gitlab-sync` enumerates everything you can reach, clones
what's missing into a faithful mirror of the namespace tree, pulls what's stale,
and parks each repo on its most active branch — concurrently, with retries, and
**without ever stomping on the feature branch you're in the middle of.**

It carries no credentials of its own: authentication rides entirely on your
existing [`glab`](https://gitlab.com/gitlab-org/cli) login and `git` setup.

```bash
pip install .
gitlab-sync status      # see where you stand
gitlab-sync sync        # fetch → clone → update → branches → verify
```

> **New here?** [**QUICKSTART.md**](QUICKSTART.md) takes you from install to a
> fully-wired AI workspace (mirror → knowledge graph → Claude Code / Windsurf) in a
> few minutes.

## What's in the box

**The core loop**

- **Discovers everything** in a GitLab group and its subgroups via the API.
- **Clones what's missing**, preserving GitLab's exact directory structure.
- **Updates what's stale** with a fast-forward pull, honestly reporting whether
  anything actually changed.
- **Rides the active branch** — picks each repo's liveliest branch by commit
  count, recency, or a hybrid of both (your call).
- **Verifies the mirror** against GitLab and flags drift, orphans, and
  repos-nested-inside-repos.

**Because it runs across hundreds of repos**

- **Concurrent** by default, with an **adaptive worker pool** that backs off when
  the network starts misbehaving and ramps back up when it recovers.
- **Resilient** — exponential backoff with jitter on transient failures, fail-fast
  on the ones that won't recover (DNS, TLS).

**Because it's your working machine**

- **Branch safety**: never yanks you off a working branch or clobbers uncommitted
  changes — skip, or `--auto-stash`, your choice.
- **`--dry-run`** everything first if you're the cautious type.
- **Configurable** via INI files (local + global) with sensible precedence, plus
  per-run CLI overrides.

## Installation

### Prerequisites

- Python 3.9 or higher
- Git command-line tool
- GitLab CLI (`glab`) installed and authenticated
- Appropriate GitLab access permissions for the target group

### Setup

1. Ensure `glab` is installed and authenticated:

```bash
# Install glab (if not already installed) then authenticate
glab auth login
```

2. Install the tool. Either install it as a package (recommended) or run the
   script directly without installing:

```bash
# Option A — install (provides the `gitlab-sync` command)
pip install .            # or: pip install -e ".[dev]" for development

# Option B — run the bundled script directly, no install
python3 gitlab_sync.py --help
```

   Once installed, all three of these are equivalent:

```bash
gitlab-sync sync
python -m gitlab_sync sync
python3 gitlab_sync.py sync
```

3. **Configure the tool** (recommended):

   ```bash
   # Global config (affects all workspaces) OR local (workspace-specific)
   cp .gitlab_sync.ini.example ~/.gitlab_sync.ini
   # OR
   cp .gitlab_sync.ini.example <workspace>/.gitlab_sync.ini
   ```

   - Edit the configuration file with your settings:

   ```ini
   [gitlab_sync]
   work_dir = ~/work
   gitlab_group = your-gitlab-group
   max_workers = 8
   ```

   - See the "Configuration" section below for details. Precedence is
     `--config` > local `.gitlab_sync.ini` > global `~/.gitlab_sync.ini` > defaults.

### Security Best Practices

The tool uses external configuration files for security and flexibility:

- **No Hardcoded Credentials**: Company names, personal paths, and GitLab groups are not hardcoded in the script
- **Config File Isolation**: Sensitive configuration is stored in separate files that can be secured with appropriate file permissions
- **Git-Friendly**: Configuration files can be excluded from version control (add to `.gitignore`)
- **Environment-Specific**: Different configurations for different environments (dev, staging, prod)
- **Override Capability**: CLI arguments can override config file settings for temporary changes

**Configuration Files for GitHub Publication:**

When publishing this tool to GitHub:

- Use `.gitlab_sync.ini.example` as the published configuration template
- This file contains generic placeholders like `your-gitlab-group`
- Users should copy `.gitlab_sync.ini.example` to `.gitlab_sync.ini` and customize it
- Add `.gitlab_sync.ini` to `.gitignore` to prevent committing sensitive configuration
- The script will detect if you're using example values and warn you

**Setup Instructions for New Users:**

1. Copy the example configuration:

   ```bash
   cp .gitlab_sync.ini.example .gitlab_sync.ini
   ```

2. Edit `.gitlab_sync.ini` with your settings:
   - Set `gitlab_group` to your actual GitLab group
   - Set `work_dir` to your desired workspace location
   - Adjust other settings as needed

3. Ensure `.gitlab_sync.ini` is in `.gitignore`:

   ```bash
   echo ".gitlab_sync.ini" >> .gitignore
   ```

**Recommended file permissions:**

```bash
chmod 600 ~/.gitlab_sync.ini  # Only owner can read/write
chmod 600 .gitlab_sync.ini    # Only owner can read/write
```

## Usage

Examples below use `python3 gitlab_sync.py`; substitute `gitlab-sync` if you
installed the package.

### Commands at a glance

| Command | What it does |
| --- | --- |
| `status` | Show the workspace sync state vs GitLab (read-only) |
| `fetch` | Cache the GitLab project list |
| `clone` | Clone repos that exist on GitLab but not locally |
| `update` | Pull updates for local repos (skips in-progress working branches) |
| `branches` | Switch each repo to its most active branch |
| `verify` | Check the local mirror matches GitLab (drift, orphans, nesting) |
| `sync` | The full pipeline: fetch → clone → update → branches → verify |
| `bootstrap` | **Turnkey**: sync + index + connect + embed + wiki + steer |
| `index` | Build the code/dependency graph (`--workspace`, incremental, `--watch`) |
| `connect` | Link repos to Atlassian / Figma / GitLab sources |
| `embed` | Build semantic-search vectors (needs an embeddings model) |
| `lint` | Graph health — stale repos (HEAD moved) and dangling edges; exits non-zero if any |
| `wiki` | LLM-synthesized, council-verified wiki pages (needs a model) |
| `steer` | Write editor steering — `AGENTS.md`, `.mcp.json`, `.windsurfrules`, skills |
| `serve` | Expose the graph over MCP (`--transport stdio`/`http`) |
| `query` | Search the index (`--kind`, `--repo`, `--limit`, `--as-of <commit>`) |
| `doctor` | Check the knowledge-layer environment (SQLite FTS5, git/glab, store, embeddings) |

The first seven are the core sync (detailed below); the rest are the optional
**[knowledge layer](#knowledge-layer)**. Run any command with `--config` (sync INI)
and, for the knowledge layer, `--config`/`--kb-config` pointing at your `kb.toml`.

### Global options

These apply to any command:

- `--dry-run` — preview clone/update/branch actions without changing anything.
- `-v` / `--verbose`, `-q` / `--quiet` — control console verbosity.
- `--log-file PATH` — append a full timestamped audit log (rotating).
- `--config PATH` — use a specific config file (highest precedence).
- `--version` — print the version and exit.

Output is colorized on a terminal (status glyphs, a progress bar); set `NO_COLOR`
to disable or `FORCE_COLOR` to keep colours when piping. Colours are dropped
automatically for non-TTY output (pipes, cron, log files).

A read-only `status` followed by a `--dry-run sync` is the safest way to preview
what a sync would do:

```bash
gitlab-sync status
gitlab-sync --dry-run sync
```

### Basic Commands

The tool supports the following commands:

#### 1. `status` - Check Current Synchronization Status

Shows the current state of your workspace compared to GitLab.

```bash
python3 gitlab_sync.py status
```

**Output Example:**

```text
[2026-05-24 17:55:37] Working directory: /home/user/work
[2026-05-24 17:55:37] GitLab group: your-gitlab-group
[2026-05-24 17:55:37] 
[2026-05-24 17:55:37] Current synchronization status:
[2026-05-24 17:55:44] GitLab projects (cached): 481 (active: 481)
[2026-05-24 17:55:44] Local repositories: 481
[2026-05-24 17:55:44] Synchronized: 480
[2026-05-24 17:55:44] Missing: 1
[2026-05-24 17:55:44] Extra: 1
```

#### 2. `fetch` - Fetch All GitLab Projects

Retrieves all repositories from the specified GitLab group and caches them locally.

```bash
python3 gitlab_sync.py fetch
```

This command:

- Uses the GitLab API with pagination to fetch all projects
- Includes subgroups automatically
- Skips archived repositories
- Caches results in `/tmp/gitlab_projects.txt` and `/tmp/gitlab_projects.json`

#### 3. `clone` - Clone Missing Repositories

Clones any repositories that exist in GitLab but are missing locally.

```bash
python3 gitlab_sync.py clone
```

This command:

- Compares cached GitLab projects with local repositories
- Creates directory structure matching GitLab's group/subgroup hierarchy
- Uses HTTPS cloning for better authentication
- Clones up to 8 repositories concurrently
- Handles timeouts gracefully (300s per repository)

#### 4. `update` - Update Existing Repositories

Fetches and pulls the latest changes for all local repositories.

```bash
python3 gitlab_sync.py update
```

This command:

- Fetches all remote branches
- Updates the current branch with latest changes from origin
- Handles detached HEAD states appropriately
- Reports repositories that are already up to date

#### 5. `branches` - Switch to Most Active Branches

Analyzes all repositories and switches them to their most active development branch.

```bash
python3 gitlab_sync.py branches
```

This command:

- Fetches all remote branches for each repository
- Calculates commit count for each branch
- Identifies the branch with the most commits (most active)
- Switches to the most active branch if different from current
- Pulls latest changes after switching

**Branch Selection Criteria:**

- Primary: Commit count (more commits = more active development)
- Secondary: Latest commit date (used as tiebreaker)
- Skips: Archived repositories, repositories without branches, detached HEAD states

#### 6. `verify` - Verify Repository Structure

Checks that the local workspace structure matches GitLab exactly.

```bash
python3 gitlab_sync.py verify
```

This command:

- Compares local repositories with GitLab project list
- Identifies nested `.git` directory structures (indicates incorrect cloning)
- Lists extra local repositories (not in GitLab)
- Lists missing repositories (in GitLab but not local)
- Reports synchronization status

#### 7. `sync` - Full Synchronization

Runs the complete synchronization pipeline in sequence.

```bash
python3 gitlab_sync.py sync
```

This command executes:

1. `fetch` - Get latest GitLab project list
2. `clone` - Clone missing repositories
3. `update` - Update existing repositories
4. `branches` - Switch to active branches
5. `verify` - Verify structure

### Advanced Usage

#### Using Configuration Files

The tool supports configuration files for persistent settings. Configuration is loaded in the following precedence order:

1. **Local config**: `.gitlab_sync.ini` in the current directory (highest priority)
2. **Global config**: `~/.gitlab_sync.ini` in the home directory
3. **Default values**: Built-in defaults (lowest priority)
4. **CLI arguments**: Override all other settings

**Example configuration file (.gitlab_sync.ini):**

```ini
[gitlab_sync]
work_dir = ~/work
gitlab_group = your-gitlab-group
cache_dir = /tmp
clone_timeout = 300
fetch_timeout = 60
branch_timeout = 30
pull_timeout = 60
max_workers = 8
```

#### Custom Work Directory

```bash
# Using config file (recommended)
# Edit .gitlab_sync.ini and set work_dir

# Or override with CLI argument
python3 gitlab_sync.py --work-dir /path/to/workspace sync
```

#### Custom GitLab Group

```bash
# Using config file (recommended)
# Edit .gitlab_sync.ini and set gitlab_group

# Or override with CLI argument
python3 gitlab_sync.py --group my-gitlab-group sync
```

#### Combined Options

```bash
python3 gitlab_sync.py --work-dir /home/user/dev --group your-gitlab-group status
```

#### Custom Config File

```bash
python3 gitlab_sync.py --config /path/to/custom.ini sync
```

### Configuration Reference

| Setting | Description | Default | Example |
| --- | --- | --- | --- |
| `work_dir` | Working directory for repositories | `~/work` | `/home/user/projects` |
| `gitlab_group` | GitLab group to synchronize | `your-gitlab-group` | `mycompany-group` |
| `cache_dir` | Directory for cache files | `/tmp` | `~/.cache/gitlab_sync` |
| `cache_file` | Name of projects cache file | `gitlab_projects.txt` | `projects.txt` |
| `cache_json` | Name of JSON cache file | `gitlab_projects.json` | `projects.json` |
| `clone_timeout` | Clone operation timeout (seconds) | `300` | `600` |
| `fetch_timeout` | Fetch operation timeout (seconds) | `60` | `120` |
| `branch_timeout` | Branch operation timeout (seconds) | `30` | `60` |
| `pull_timeout` | Pull operation timeout (seconds) | `60` | `120` |
| `max_workers` | Maximum parallel workers | `8` | `4` |
| `clean_corrupted` | Auto-remove corrupted directories | `true` | `false` |
| `max_retries` | Maximum retry attempts for failed operations | `3` | `5` |
| `backoff_initial` | Initial backoff time in seconds | `1` | `2` |
| `backoff_max` | Maximum backoff time in seconds | `30` | `60` |
| `adaptive_workers` | Enable adaptive worker pool | `true` | `false` |
| `min_workers` | Minimum workers for adaptive pool | `2` | `4` |
| `error_threshold` | Error rate threshold for adaptive workers | `0.5` | `0.3` |
| `protect_working_branches` | Prevent operations on working branches | `true` | `false` |
| `safe_branches` | Comma-separated list of safe branches | `main,master,develop,development` | `main,production` |
| `require_clean_workspace` | Require clean workspace before operations | `true` | `false` |
| `auto_stash` | Automatically stash changes before operations | `false` | `true` |

## Branch Safety

The tool includes comprehensive branch safety features to prevent sync operations from impacting your local working branches.

### Safety Checks

The tool performs the following safety checks before operations:

1. **Branch Protection**: Detects if the current branch is a "working branch" (not in the safe branches list)
2. **Clean Workspace Check**: Detects uncommitted changes in the repository
3. **Automatic Stashing**: Optionally stashes changes if workspace is not clean

### Configuration

Branch safety is controlled by the following configuration options:

| Setting | Description | Default |
| --- | --- | --- |
| `protect_working_branches` | Prevent operations on working branches | `true` |
| `safe_branches` | Comma-separated list of safe branches | `main,master,develop,development` |
| `require_clean_workspace` | Require clean workspace before operations | `true` |
| `auto_stash` | Automatically stash changes before operations | `false` |

### Behavior

**When Branch Protection is Enabled:**

- **Update operations**: Skips repositories on working branches (unless auto-stash is enabled)
- **Branch switching operations**: Skips repositories on working branches
- **Warning messages**: Clear messages indicate why operations were skipped

**When Workspace Clean Check is Enabled:**

- **Update operations**: Skips repositories with uncommitted changes
- **Branch switching operations**: Skips repositories with uncommitted changes
- **Auto-stash**: If enabled, automatically stashes changes before proceeding

### Example Scenarios

#### Scenario 1: Working Branch Protection

```bash
# Repository is on feature/my-feature branch (not in safe branches)
python3 gitlab_sync.py update

# Output:
# [2026-06-16 10:00:00] ⊘ backend/services/api-gateway: Skipped branch switch (on working branch: feature/my-feature)
```

#### Scenario 2: Uncommitted Changes

```bash
# Repository has uncommitted changes
python3 gitlab_sync.py update

# Output:
# [2026-06-16 10:00:00] ⊘ backend/services/api-gateway: Skipped (unsafe: Uncommitted changes detected)
```

#### Scenario 3: Auto-Stash Enabled

```bash
# Repository has uncommitted changes, auto-stash enabled
python3 gitlab_sync.py --auto-stash update

# Output:
# [2026-06-16 10:00:00] ⚠ backend/services/api-gateway: Changes stashed successfully
# [2026-06-16 10:00:00] ✓ backend/services/api-gateway: Updated main
```

### Customization

You can customize branch safety behavior via configuration or CLI:

```ini
# In .gitlab_sync.ini
[gitlab_sync]
protect_working_branches = true
safe_branches = main,master,develop,staging
require_clean_workspace = true
auto_stash = false
```

```bash
# Or via CLI
python3 gitlab_sync.py --safe-branches main,master,develop,staging --auto-stash update
```

### Disabling Safety Checks

If you want to disable safety checks (not recommended for production workflows):

```bash
# Disable all safety checks
python3 gitlab_sync.py --no-protect-working-branches --no-require-clean-workspace update
```

**Warning**: Disabling safety checks can lead to conflicts, lost work, or corruption of your local branches. Only disable if you understand the risks.

## Automation with Cron Jobs

### Prerequisites for Cron Jobs

Before setting up cron jobs, ensure you have:

1. **Configuration file set up**: Create `~/.gitlab_sync.ini` with your settings

   ```bash
   cp .gitlab_sync.ini ~/.gitlab_sync.ini
   # Edit with your work_dir and gitlab_group
   nano ~/.gitlab_sync.ini
   ```

2. **Absolute path to script**: Cron requires absolute paths

   ```bash
   which python3  # Note the path
   # Example: /usr/bin/python3
   ```

3. **Test the command manually first**:

   ```bash
   cd /home/user/work && python3 gitlab_sync.py sync
   ```

### Basic Daily Sync

Run a full synchronization daily at 2 AM:

```bash
# Edit crontab
crontab -e

# Add the following line (replace paths as needed)
0 2 * * * cd /home/user/work && /usr/bin/python3 gitlab_sync.py sync >> /tmp/gitlab_sync.log 2>&1
```

**Note**: This uses the configuration from `~/.gitlab_sync.ini`. No need to specify work_dir or gitlab_group in the cron command.

### Hourly Updates (No Branch Switching)

Update repositories hourly without changing branches (for CI/CD environments):

```bash
0 * * * * cd /home/user/work && /usr/bin/python3 gitlab_sync.py update >> /tmp/gitlab_hourly.log 2>&1
```

### Weekly Full Sync with Branch Management

Run full sync including branch switching weekly on Sunday at 3 AM:

```bash
0 3 * * 0 cd /home/user/work && /usr/bin/python3 gitlab_sync.py sync >> /tmp/gitlab_weekly.log 2>&1
```

### Multiple Workspaces

For multiple workspaces, use separate config files:

```bash
# Create workspace-specific config files
cat > ~/.gitlab_sync_primary.ini << EOF
[gitlab_sync]
work_dir = ~/work
gitlab_group = example-group-primary
EOF

cat > ~/.gitlab_sync_secondary.ini << EOF
[gitlab_sync]
work_dir = ~/Projects/Secondary
gitlab_group = example-group-secondary
EOF

# Add to crontab

# Sync primary workspace daily
0 2 * * * cd /home/user/work && /usr/bin/python3 gitlab_sync.py --config ~/.gitlab_sync_primary.ini sync >> /tmp/gitlab_primary.log 2>&1

# Sync secondary workspace every 6 hours
0 */6 * * * cd /home/user/work && /usr/bin/python3 gitlab_sync.py --config ~/.gitlab_sync_secondary.ini update >> /tmp/gitlab_secondary.log 2>&1
```

### Monitoring and Alerts

Add email notifications for failures:

```bash
# Create a wrapper script
cat > /home/user/scripts/gitlab_sync_wrapper.sh << 'EOF'
#!/bin/bash
cd /home/user/work
python3 gitlab_sync.py sync >> /tmp/gitlab_sync.log 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "GitLab sync failed with exit code $EXIT_CODE" | mail -s "GitLab Sync Failure" user@example.com
fi
EOF
chmod +x /home/user/scripts/gitlab_sync_wrapper.sh

# Add to crontab
0 2 * * * /home/user/scripts/gitlab_sync_wrapper.sh
```

### Log Rotation

To prevent log files from growing indefinitely, set up log rotation:

```bash
# Create logrotate configuration
sudo cat > /etc/logrotate.d/gitlab_sync << 'EOF'
/tmp/gitlab_sync.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0644 user user
}
EOF
```

## Knowledge layer

An optional subsystem (`gitlab_sync.kb`) turns your mirrored repositories into a
queryable **knowledge graph** and serves it to AI agents over **MCP** — so an
assistant can ask "where is `X` defined?", "who calls `Y`?", or "which repos
depend on package `Z`?" instead of grepping hundreds of repos. It's generic: it
indexes *any* repositories and connects to *any* configured knowledge sources; no
organization-specific data lives in the package (your sites, keys, and rules go in
a private config file).

Install the extra (requires Python ≥ 3.10):

```bash
pip install "gitlab-sync[kb]"
gitlab-sync doctor                          # check the environment
gitlab-sync index --source ./my-repo        # index one repository
gitlab-sync index --workspace ~/work        # index every git repo (incremental; --force to rebuild)
gitlab-sync connect --workspace ~/work      # link repos to their issues/docs (see below)
gitlab-sync embed                           # build semantic vectors (optional, see below)
gitlab-sync lint                            # graph health: stale repos + dangling edges
gitlab-sync wiki                            # LLM-synthesized, council-verified wiki pages (optional)
gitlab-sync steer                           # write per-tool steering: AGENTS.md, .mcp.json, …
gitlab-sync query "OrderService"            # cited search across the index
gitlab-sync serve                           # expose the graph over MCP (stdio or --transport http)
```

`index --workspace` is **incremental** — it re-indexes only repos whose git HEAD
moved since their last index, so a scheduled (cron) run stays cheap; pass `--force`
to rebuild everything, or `--watch [--interval N]` to keep re-indexing in a loop.
Every indexed snapshot is kept, so `query "<text>" --repo R --as-of <commit>` does
**time-travel** — it searches repo `R` as it was at a previously-indexed commit.

**Health & maintenance.** `gitlab-sync doctor` is a quick environment check — SQLite
FTS5, `git`/`glab` on `PATH`, the store's reachability and counts, and the embeddings
status — and exits non-zero if something's wrong. `gitlab-sync lint` audits the graph
itself, reporting **stale repos** (HEAD moved since they were indexed, so the index is
behind) and **dangling edges** (an edge whose endpoint node is missing); it exits
non-zero when it finds problems, so it's CI-friendly.

**One command to set it all up.** Rather than running the steps by hand, `bootstrap`
chains them — mirror repos → index → connect → embed → wiki → write editor steering —
skipping anything not enabled, so a teammate goes from nothing to a fully-wired
workspace in one step:

```bash
gitlab-sync bootstrap --kb-config ~/.gitlab-sync/kb.toml
```

Skip stages with `--no-sync` / `--no-embed` / `--no-wiki` / `--no-connect`. For an
isolated CLI, install with `pipx install "git+https://github.com/sayak-sarkar/gitlab-sync"`
(add the `[kb]` extra for the knowledge layer), or run ad-hoc with `uvx`.

**Keep it fresh on a schedule.** `bootstrap` is incremental and branch-safe, so it's
safe to run repeatedly — it re-mirrors, re-indexes only the repos whose HEAD moved,
refreshes the knowledge layer, and rewrites the steering, without touching an
in-progress working tree. Run it from cron:

```cron
*/30 * * * * gitlab-sync bootstrap --config ~/.gitlab_sync.ini --kb-config ~/.gitlab-sync/kb.toml >> ~/.gitlab-sync/refresh.log 2>&1
```

or as a systemd user timer — see [`examples/gitlab-sync.service`](examples/gitlab-sync.service)
and [`examples/gitlab-sync.timer`](examples/gitlab-sync.timer).

**Code indexing** uses tree-sitter to extract files, classes, functions/methods,
interfaces, imports, and an intra-repo **call graph** from **Python, JavaScript,
TypeScript/TSX, and C#** (the parser registry is pluggable). It also reads
manifests (`pyproject.toml`, `package.json`, `*.csproj`) to build a **cross-repo
dependency graph** through shared package nodes. Over MCP, agents get tools to
traverse it: `search_code`, `find_definition`, `find_callers`, `find_dependents`,
`get_neighbors`, `shortest_path`, and `graph_stats`.

**Knowledge connectors** (`connect`) enrich the graph with external context. The
**Atlassian** connector links each repo to the Jira issues and Confluence pages it
references — issue keys harvested from branch/commit names are confirmed against
the live tracker (a single batched JQL call per site prunes false-positives and
fetches each issue's summary/status), and Atlassian URLs found in docs are
classified into issue/page links. It talks to one or more Atlassian sites over
MCP, each independently authenticated. The **Figma** connector links repos to the
design files they reference, classifying `figma.com` URLs to a stable file key. The
**GitLab** connector links each repo to its open **merge requests and issues** (read
through your authenticated `glab`). Connectors share one seam, so adding another is a
small, self-contained module; output lands in an isolated graph partition, so
re-indexing a repo's code never disturbs its external links.

Configure it by copying [`examples/kb.toml.example`](examples/kb.toml.example) to
`~/.gitlab-sync/kb.toml`. Every fact is provenance-stamped (source file + verified
date) and confidence-tagged (`EXTRACTED` for AST facts, `INFERRED` for resolved
calls/links, `AMBIGUOUS` for unconfirmed candidates), and all output is sanitized
before it reaches an agent.

**Semantic search** (optional) adds natural-language retrieval on top of the graph.
Enable `[embeddings]` in the config (local-first — vectors come from an Ollama model
by default, so code never leaves the machine), run `gitlab-sync embed` to vectorize
the indexed nodes into a local store, and `serve` then exposes two tools:
`semantic_search` for queries where the exact symbol name is unknown, and
`hybrid_search`, which seeds Personalized PageRank with the embedding hits and
propagates relevance across the graph (HippoRAG-style) to surface structurally
related nodes — a function's callers, a package's dependents — that a pure semantic
match would miss. The vector store uses an exact pure-Python cosine scan by default;
install the optional ANN backend with `pip install "gitlab-sync[kb-vec]"` (sqlite-vec)
for larger workspaces.

**Curated wiki** (optional, local-first) turns the graph into prose. Enable
`[llm]` in the config (generation runs on a local Ollama model by default — prompts
never leave the machine) and run `gitlab-sync wiki`: for each repo it synthesizes a
Markdown page grounded strictly in graph facts (top symbols, dependencies, files)
with a provenance footer citing the commit and sources, then puts the draft through
a **verification council** — reviewers score it for accuracy, completeness, and
clarity and a chairman publishes only pages above a configurable threshold. Nothing
that fails review is written.

**Model providers are pluggable.** The embeddings and wiki tiers default to a local
Ollama (`provider = "ollama"`); set `provider = "openai"` to use **any
OpenAI-compatible API** instead — a hosted key, or a local server like LM Studio,
Jan, llama.cpp, or vLLM. The key is read from an environment variable named by
`api_key_env` (default `OPENAI_API_KEY`) and is never stored in config; local
servers that need no key work with it unset. See `examples/kb.toml.example`.

### Use it from your editor or agent (MCP)

`gitlab-sync serve` is an MCP server, so any MCP client can query the graph — and
**most of it needs no model**: the graph tools (`search_code`, `find_definition`,
`find_callers`, `find_dependents`, `shortest_path`, `graph_stats`) work on their
own; only `semantic_search`/`hybrid_search` need embeddings.

**The quickest way** is to let the tool wire your editors for you. From your
workspace root:

```bash
gitlab-sync steer --config ~/.gitlab-sync/kb.toml
```

This writes workspace-specific **`AGENTS.md`** (overview, the knowledge tools, and
guardrails), a thin **`CLAUDE.md`** that imports it, **`.windsurfrules`**,
**`.kiro/steering/`**, and merges a **`.mcp.json`** entry — so Claude Code, Windsurf,
Kiro, and other agents pick up the workspace context and the MCP server natively. It
also installs a generic library of **agent skills/workflows** (`.claude/skills/`,
`.windsurf/workflows/`) — investigate-root-cause, plan-before-coding,
surgical-change, review-before-landing, ship-safely, use-knowledge-graph — so even a
small-context model has a strong operating playbook. **It never corrupts your
existing files**: if you already have an `AGENTS.md`, `CLAUDE.md`, `.windsurfrules`,
or `.kiro/steering`, your content is preserved and a clearly-delimited managed block
is appended (and only that block is refreshed on re-runs); `.mcp.json` is merged so
your other servers stay; a skill file you wrote with the same name is kept as-is.
Custom layers like `.devin/` are left untouched.

To wire Claude Code by hand instead:

```bash
claude mcp add gitlab-kb -- gitlab-sync serve --config ~/.gitlab-sync/kb.toml
```

**Windsurf / Devin** — add the same server in its MCP config (Cascade's *MCP
Servers* panel, or `~/.codeium/windsurf/mcp_config.json`):

```json
{
  "mcpServers": {
    "gitlab-kb": {
      "command": "gitlab-sync",
      "args": ["serve", "--config", "~/.gitlab-sync/kb.toml"]
    }
  }
}
```

Once connected, ask the agent things like "where is `OrderService` defined?", "who
calls `charge`?", or "which repos depend on `shared-core`?" and it calls the graph
tools directly — you can even have it draft wiki pages from the graph without the
built-in `wiki` command.

## Architecture & internals

The deep dive — core-sync internals, the knowledge-layer architecture, data flow,
performance, and troubleshooting — lives in
[`docs/internals.md`](docs/internals.md).

## Version history

See [CHANGELOG.md](CHANGELOG.md) for the full, per-release history.

## Best Practices

1. **Initial Setup**: Run `python3 gitlab_sync.py sync` once to set up full workspace
2. **Regular Updates**: Use `python3 gitlab_sync.py update` for frequent, fast updates
3. **Branch Management**: Run `python3 gitlab_sync.py branches` periodically to stay on active branches
4. **Monitoring**: Check logs regularly for errors or failures
5. **Backup**: Commit workspace state to git before major branch switches
6. **Testing**: Test cron commands manually before adding to crontab
7. **Documentation**: Keep this documentation updated with any custom configurations

## Roadmap

The mirroring core and the optional knowledge layer are shipped. See
[ROADMAP.md](ROADMAP.md) for future good-to-haves (hosted provider options, full
bi-temporal validity windows, more connectors, and more).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For issues or questions:

1. Check this documentation first
2. Review log files for error messages
3. Test individual commands to isolate issues
4. Verify `glab` authentication: `glab auth status`
5. Check GitLab access permissions in web interface
