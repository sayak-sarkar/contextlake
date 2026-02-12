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
design files they reference, classifying `figma.com` URLs to a stable file key.
Connectors share one seam, so adding another is a small, self-contained module;
output lands in an isolated graph partition, so re-indexing a repo's code never
disturbs its external links.

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
only overwrites files it manages (re-run any time to refresh).

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

## Technical Documentation

### Architecture

The tool is built as a modular Python CLI application with the following components:

#### Configuration System

The tool uses a hierarchical configuration system with the following precedence:

1. **Configuration Files** (using Python's `configparser`):
   - Local config: `.gitlab_sync.ini` in current directory
   - Global config: `~/.gitlab_sync.ini` in home directory
   - Custom config: Specified via `--config` CLI argument

2. **Default Values**: Built-in defaults for all settings

3. **CLI Arguments**: Override all other settings

**Configuration Loading Flow:**

```text
load_config()
  ↓
Check for local config (.gitlab_sync.ini)
  ↓
Check for global config (~/.gitlab_sync.ini)
  ↓
Merge with DEFAULT_CONFIG
  ↓
Apply CLI argument overrides
  ↓
Return final config dictionary
```

#### Core Functions

1. **`load_config()`**

   - Loads configuration from INI files
   - Supports local, global, and custom config files
   - Expands tilde (~) for home directory paths
   - Returns configuration dictionary

2. **`get_cache_paths(config)`**

   - Constructs cache file paths from configuration
   - Returns tuple of (cache_file, cache_json)

3. **`fetch_gitlab_projects(gitlab_group, config)`**

   - Uses `glab api projects?membership=true` to fetch all accessible projects
   - Filters projects by group prefix (e.g., `your-gitlab-group/`)
   - Works even without direct access to top-level group
   - Supports subgroups automatically via prefix filtering
   - Filters out archived repositories
   - Caches results in JSON and plain text formats (paths from config)

4. **`load_gitlab_projects(config, gitlab_group)`**

   - Loads cached project data from configured cache file
   - Parses pipe-delimited format: `path|ssh_url|http_url|default_branch|archived`
   - Returns dictionary keyed by local path

5. **`get_local_repos(work_dir)`**

   - Uses `find` command to locate all `.git` directories
   - Extracts repository paths by removing `.git` suffix
   - Returns set of local repository paths

6. **`clone_repository(project, work_dir, config)`**

   - Creates parent directories as needed
   - Executes `git clone` with timeout from config
   - Returns status tuple: `(status, path, message)`

7. **`clone_missing_repos(work_dir, config, gitlab_group)`**

   - Identifies repositories to clone by comparing GitLab vs local
   - Uses ThreadPoolExecutor with max_workers from config
   - Tracks successes and failures separately

8. **`update_repository(local_path, work_dir, config)`**

   - Fetches all remote branches (timeout from config)
   - Identifies current branch
   - Pulls latest changes from origin (timeout from config)
   - Handles detached HEAD states

9. **`update_repositories(work_dir, config)`**

   - Processes all local repositories concurrently
   - Uses max_workers from config
   - Reports updates, no-change cases, and errors

10. **`switch_repository_branch(local_path, projects, work_dir, config)`**

    - Fetches all remote branches (timeout from config)
    - Calculates commit count for each branch using `git rev-list --count` (timeout from config)
    - Sorts branches by commit count (descending)
    - Switches to most active branch if different from current
    - Pulls latest changes after switching (timeout from config)

11. **`switch_active_branches(work_dir, config, gitlab_group)`**

    - Processes all repositories concurrently
    - Uses max_workers from config
    - Tracks switched, already-correct, skipped, and error cases

12. **`verify_structure(work_dir, config, gitlab_group)`**

    - Detects nested `.git` directories (indicates incorrect structure)
    - Compares local vs GitLab repository lists
    - Reports extra and missing repositories
    - Validates structure matches GitLab exactly

13. **`show_status(work_dir, config, gitlab_group)`**

    - Displays current synchronization state
    - Shows GitLab project count, local repo count
    - Lists synchronized, missing, and extra repositories

14. **`main()`**

    - Entry point for CLI application
    - Loads configuration
    - Parses CLI arguments
    - Overrides config with CLI arguments
    - Dispatches to appropriate command handler

### Data Flow

```text
User Command
    ↓
Argument Parsing (argparse)
    ↓
Command Dispatch
    ↓
┌─────────────────────────────────────┐
│ GitLab API (glab)                    │
│ ↓                                    │
│ Cache Files (/tmp/*.txt, *.json)     │
│ ↓                                    │
│ Local Workspace (find .git)          │
│ ↓                                    │
│ Comparison & Analysis                │
│ ↓                                    │
│ Git Operations (clone, fetch, pull)  │
│ ↓                                    │
│ Logging & Reporting                  │
└─────────────────────────────────────┘
```

### Concurrency Model

The tool uses Python's `ThreadPoolExecutor` for concurrent operations:

- **Cloning**: 8 parallel workers
- **Updating**: 8 parallel workers
- **Branch Switching**: 8 parallel workers

Each worker operates independently with its own timeout:

- Clone operations: 300s timeout
- Fetch operations: 60s timeout
- Branch operations: 30s timeout
- Pull operations: 60s timeout

### Error Handling

The tool implements comprehensive error handling:

1. **Timeout Handling**: All subprocess calls have explicit timeouts
2. **Exception Catching**: All functions catch and report exceptions
3. **Status Reporting**: Operations return status tuples for tracking
4. **Graceful Degradation**: Failed operations don't stop the entire process
5. **Detailed Logging**: All errors are logged with context

### Cache Management

The tool uses two cache files:

1. **`/tmp/gitlab_projects.json`**

   - Full JSON response from GitLab API
   - Used for debugging and detailed inspection
   - Contains complete project metadata

2. **`/tmp/gitlab_projects.txt`**

   - Pipe-delimited format for faster loading
   - Format: `path_with_namespace|ssh_url|http_url|default_branch|archived`
   - Primary data source for all operations

Cache is refreshed by running the `fetch` command or `sync` (which includes fetch).

### Branch Selection Algorithm

To identify the most active branch the tool:

1. **Fetches all branches** with `git for-each-ref` (collecting each branch's last
   commit date)
2. **Calculates commit count** per branch via `git rev-list --count origin/branch`
3. **Scores and ranks** branches according to `branch_strategy`
4. **Switches** to the top branch (and pulls) if it differs from the current one

The `branch_strategy` setting controls step 3:

- `commits` — rank purely by commit count (legacy behaviour)
- `recency` — rank purely by most recent commit
- `hybrid` (default) — a weighted blend of normalized commit count and recency,
  so a branch that is both busy and recently active wins; this avoids picking a
  long-lived branch that has gone stale, or a brand-new branch with few commits

Branch switching is skipped entirely for repositories checked out on a working
branch (see [Branch Safety](#branch-safety)).

### Directory Structure Mapping

The tool maintains GitLab's exact directory structure:

```text
GitLab Path: your-gitlab-group/backend/services/api-gateway
Local Path:  backend/services/api-gateway

GitLab Path: your-gitlab-group/backend/pricing/quote-engine
Local Path:  backend/pricing/quote-engine

GitLab Path: your-gitlab-group/frontend/platform/ui-toolkit
Local Path:  frontend/platform/ui-toolkit
```

The `your-gitlab-group/` prefix is stripped when creating local paths.

### Performance Characteristics

**Typical Performance Metrics** (based on a large workspace of several hundred repositories):

- **Fetch**: 30-60 seconds (depends on GitLab API response time)
- **Clone**: 5-10 minutes (for missing repos, concurrent)
- **Update**: 3-5 minutes (all repos, concurrent)
- **Branch Switching**: 5-10 minutes (all repos, concurrent)
- **Verify**: 30-60 seconds
- **Full Sync**: 15-30 minutes (all operations)

**Performance Optimization Tips:**

1. Run `fetch` less frequently if repository list doesn't change often
2. Use `update` for frequent syncs (faster than full sync)
3. Run `branches` only when branch management is needed
4. Adjust ThreadPoolExecutor worker count based on system resources

### Security Considerations

1. **Authentication**: Uses `glab` authentication (tokens, SSH keys, etc.)
2. **HTTPS Cloning**: Default cloning method uses HTTPS for better compatibility
3. **No Credential Storage**: Does not store credentials; relies on `glab` auth
4. **Local Operations**: All git operations are local; no external API calls beyond initial fetch
5. **File Permissions**: Respects existing file permissions; creates directories with default umask

### Troubleshooting

#### Issue: "Cache file not found"

**Solution**: Run `python3 gitlab_sync.py fetch` first to populate cache

#### Issue: "Permission denied" during cloning

**Solution**: Ensure `glab` is authenticated and you have access to the repositories

#### Issue: "Timeout" errors

**Solution**:

- Increase timeout values in the script
- Check network connectivity
- Reduce ThreadPoolExecutor worker count
- Run operations sequentially instead of concurrently

#### Issue: "Detached HEAD" states

**Solution**: The tool handles this automatically by skipping pull operations in detached HEAD state

#### Issue: Nested `.git` directories detected

**Solution**: This indicates incorrect cloning. Manually fix by:

```bash

# Example: repo/repo/.git
cd repo
mv repo/* .
mv repo/.* . 2>/dev/null || true
rmdir repo
```

#### Issue: Cron job not running

**Solution**:

- Check cron syntax: `crontab -l`
- Verify script is executable: `ls -l gitlab_sync.py`
- Check cron logs: `grep CRON /var/log/syslog`
- Test cron command manually in shell

#### Issue: Large log files

**Solution**: Set up log rotation (see "Log Rotation" section above)

### Extension Points

The tool can be extended by:

1. **Adding New Commands**: Add new function and update `main()` command dispatch
2. **Custom Branch Selection**: Modify `switch_repository_branch()` algorithm
3. **Additional Verification**: Add checks in `verify_structure()`
4. **Custom Output Formats**: Modify logging functions
5. **Integration Hooks**: Add pre/post operation hooks
6. **Configuration File**: Add support for `.gitlab_sync.yml` config

### Dependencies

- **Python 3.6+**: Core language
- **configparser**: Configuration file parsing (standard library)
- **argparse**: CLI argument parsing (standard library)
- **subprocess**: Git and system command execution (standard library)
- **concurrent.futures**: Parallel processing (standard library)
- **json**: Data serialization (standard library)
- **datetime**: Timestamp generation (standard library)
- **pathlib**: Path manipulation (standard library)
- **glab**: GitLab CLI tool (external dependency)
- **git**: Version control system (external dependency)

### Git Integration

To avoid committing sensitive configuration to version control, add the configuration file to your `.gitignore`:

```bash

# Add to .gitignore
.gitlab_sync.ini
~/.gitlab_sync.ini
```

For team usage, consider including a sample configuration file:

```bash
# Add .gitlab_sync.ini.example to git
cp .gitlab_sync.ini .gitlab_sync.ini.example
git add .gitlab_sync.ini.example

# Update .gitignore
echo ".gitlab_sync.ini" >> .gitignore
```

Team members can then:

```bash
cp .gitlab_sync.ini.example .gitlab_sync.ini
# Edit with their personal settings
```

### Version History

- **v1.11** (2026-06-22): Steering-layer generation

  - Added a `steer` command that writes workspace-specific `AGENTS.md`, `CLAUDE.md`,
    `.windsurfrules`, `.kiro/steering`, and a merged `.mcp.json` — so local AI tools
    pick up the knowledge graph and guardrails natively

- **v1.10** (2026-06-21): OpenAI-compatible providers and MCP integration docs

  - Embeddings and wiki tiers can use any OpenAI-compatible API (`provider =
    "openai"`) — hosted or a local server — as an alternative to Ollama; the key
    comes from an env var, never config
  - Documented using `gitlab-sync serve` as an MCP server from Claude Code and
    Windsurf/Devin

- **v1.9** (2026-06-21): Curated wiki tier, watch mode, and time-travel queries

  - Added a `wiki` command: local-first LLM synthesis of provenance-stamped pages
    gated by an LLM verification council (off unless `[llm]` enabled)
  - `index --watch` for continuous incremental refresh
  - Bi-temporal `query --as-of <commit>` over per-commit shard snapshots

- **v1.8** (2026-06-21): Incremental indexing, lint, and a colorful CLI

  - `index --workspace` is now incremental (re-indexes only repos whose HEAD
    moved; `--force` to rebuild) — cheap scheduled refresh via cron
  - Added a `lint` command (stale repos + dangling edges)
  - Colorful terminal output (status glyphs + progress bar), `NO_COLOR`-aware

- **v1.7** (2026-06-21): Hybrid retrieval and ANN backend

  - Added a `hybrid_search` tool (embedding seeds + Personalized PageRank over the
    graph) that surfaces structurally-related nodes a pure semantic match misses
  - Added an optional sqlite-vec ANN vector-store backend (`gitlab-sync[kb-vec]`),
    selectable via `vector_backend`, with automatic fallback to the exact scan

- **v1.6** (2026-06-21): Semantic-search tier

  - Added an optional, local-first embeddings tier: a pluggable embedder (Ollama
    provider first), a SQLite vector store with cosine search, an `embed` command,
    and a `semantic_search` MCP tool exposed by `serve` when enabled
  - Off by default; code never leaves the machine

- **v1.5** (2026-06-21): Figma connector

  - Added a Figma knowledge connector that links repos to the design files they
    reference (classifies `figma.com` URLs to a stable key, names them from the
    URL slug, and best-effort verifies reachability over a configured Figma MCP)
  - Extracted connector-agnostic helpers to a shared module
  - Fixed `link_scrape` rules expressed as a `patterns` list being ignored

- **v1.4** (2026-06-21): Knowledge layer

  - Added the optional `gitlab_sync.kb` subsystem (the `[kb]` extra): index
    repositories into a queryable knowledge graph and serve it to AI agents over
    MCP (`index`, `connect`, `query`, `serve`, `doctor`)
  - tree-sitter code graph for Python, JavaScript, TypeScript/TSX, and C#
    (definitions, imports, containment, and an intra-repo call graph)
  - Cross-repo dependency graph from manifests, with a `find_dependents` tool
  - Atlassian knowledge connector: links repos to the Jira issues and Confluence
    pages they reference, verified and enriched against live sites over MCP
  - SQLite + FTS5 cross-repo index with per-repo JSON shards; every fact is
    provenance-stamped and confidence-tagged
  - Generic and config-driven — no organization-specific data in the package
  - See CHANGELOG.md for the full list

- **v1.3** (2026-06-21): Stabilization, packaging, and tests

  - Fixed a critical bug where boolean config settings were silently ignored,
    which had disabled branch protection and the clean-workspace requirement by
    default
  - Repaired the adaptive worker pool and wired in retry/backoff for clones
  - `update` now distinguishes updated / unchanged / error instead of reporting
    failed pulls as "Already up to date"
  - Rewrote `fetch` with a correct, URL-encoded GitLab API call; restored the
    pipe-delimited text cache and nested-repository detection in `verify`
  - Made the tool installable (`gitlab-sync` command, `python -m gitlab_sync`,
    or the bare script); added `--dry-run`, logging flags, `clone_method`,
    and a recency-aware `branch_strategy`
  - Added a pytest suite and GitHub Actions CI (Python 3.9-3.13)
  - See CHANGELOG.md for the full list

- **v1.2** (2026-06-16): Branch safety and workspace protection

  - Added branch safety checks to protect working branches from sync conflicts
  - Implemented workspace protection requiring clean workspace before operations
  - Added automatic stashing support for uncommitted changes
  - Added configurable safe branches list
  - Added CLI arguments for branch safety control (--protect-working-branches, --safe-branches, --require-clean-workspace, --auto-stash)
  - Enhanced error classification for better retry strategies
  - Added adaptive worker pool for dynamic parallelism
  - Updated documentation with branch safety section

- **v1.1** (2026-05-24): Configuration system enhancement

  - Added INI-based configuration file support
  - Removed all hardcoded company/personal identifiers
  - Added local and global config file support
  - CLI arguments now override config file settings
  - Improved security with externalized configuration
  - Added tilde expansion for home directory paths
  - Configurable timeouts and worker counts
  - Added exponential backoff retry mechanism
  - Added adaptive worker pool for dynamic parallelism
  - Enhanced error classification for better retry strategies

- **v1.0** (2026-05-10): Initial release

  - Full synchronization pipeline
  - Branch management
  - Structure verification
  - Concurrent processing
  - Comprehensive error handling

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
