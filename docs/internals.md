# Architecture & internals

> Deep-dive companion to the [README](../README.md): how the core sync and the
> knowledge layer work under the hood.

## Core sync internals

### Architecture

The tool is built as a modular Python CLI application with the following components:

#### Configuration System

The tool uses a hierarchical configuration system with the following precedence:

1. **Configuration Files** (using Python's `configparser`):
   - Local config: `.contextlake.ini` in current directory
   - Global config: `~/.contextlake.ini` in home directory
   - Custom config: Specified via `--config` CLI argument

2. **Default Values**: Built-in defaults for all settings

3. **CLI Arguments**: Override all other settings

**Configuration Loading Flow:**

```text
load_config()
  ↓
Check for local config (.contextlake.ini)
  ↓
Check for global config (~/.contextlake.ini)
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

- `commits`, rank purely by commit count (legacy behaviour)
- `recency`, rank purely by most recent commit
- `hybrid` (default), a weighted blend of normalized commit count and recency,
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

**Solution**: Run `python3 contextlake.py fetch` first to populate cache

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
- Verify script is executable: `ls -l contextlake.py`
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
6. **Configuration File**: Add support for `.contextlake.yml` config

### Dependencies

- **Python 3.9+**: Core language (the optional knowledge layer needs 3.10+)
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
.contextlake.ini
~/.contextlake.ini
```

For team usage, consider including a sample configuration file:

```bash
# Add .contextlake.ini.example to git
cp .contextlake.ini .contextlake.ini.example
git add .contextlake.ini.example

# Update .gitignore
echo ".contextlake.ini" >> .gitignore
```

Team members can then:

```bash
cp .contextlake.ini.example .contextlake.ini
# Edit with their personal settings
```

## Knowledge-layer architecture

The optional `contextlake.kb` subsystem (the `[kb]` extra) layers a knowledge graph
over the mirrored repos. Its pieces:

- **Model & store** (`kb/model.py`, `kb/store/`): pydantic `Node`/`Edge`/`Repo` carry
  provenance + confidence; a SQLite + FTS5 cross-repo index (`sqlite_store.py`) is
  built from per-repo JSON **shards** (`shards.py`), the durable source of truth.
  Each shard is also snapshotted by commit under `history/` for bi-temporal queries.
- **Extraction** (`kb/parse.py`, `kb/manifest.py`, `kb/references.py`): tree-sitter
  builds the code graph (defs/imports/containment + an inferred call graph) for
  Python/JS/TS/C#; manifests yield the cross-repo dependency graph; references capture
  issue keys and doc links.
- **Connectors** (`kb/connectors/`): Atlassian, Figma, and GitLab sources on one
  generic seam (fetched over MCP / `glab`), written into an isolated graph partition
  so code re-indexing never disturbs them.
- **Semantic tier** (`kb/embeddings/`): a pluggable `Embedder` (Ollama / OpenAI), a
  vector store (pure-Python cosine or optional `sqlite-vec`), and hybrid graph+vector
  (Personalized PageRank) retrieval.
- **Wiki tier** (`kb/llm/`, `kb/wiki/`): a pluggable `LlmClient` generates
  provenance-stamped pages gated by a verification council.
- **Serving & steering** (`kb/server.py`, `kb/steer/`): a FastMCP server exposes the
  graph tools; `steer` writes the per-tool steering files + skills library.
- **CLI** (`kb/commands.py`): the `index/connect/embed/lint/wiki/steer/serve/query/
  doctor` handlers, dispatched from the main CLI and imported lazily so the core tool
  runs without the extra installed.
