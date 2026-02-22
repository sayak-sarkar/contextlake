# Command & configuration guide

> Every command, plus configuration, branch safety, and scheduling. New here?
> Start with [QUICKSTART](../QUICKSTART.md). For the knowledge layer see
> [knowledge-layer.md](knowledge-layer.md).

### Basic Commands

The tool supports the following commands:

#### 1. `status` - Check Current Synchronization Status

Shows the current state of your workspace compared to GitLab.

```bash
gitlab-sync status
```

**Example output:**

```text
GitLab projects (cached): 481      # repos you can see on GitLab
Local repositories:       481      # repos cloned in your workspace
Synchronized:             480      # present in both and matching
Missing:                  1        # on GitLab but not cloned yet
Extra:                    1        # cloned locally but not on GitLab
```

- **Missing** = a repo exists on GitLab but isn't in your workspace — `clone` (or
  `sync`) will fetch it.
- **Extra** = a repo is in your workspace but not on GitLab — usually one that was
  renamed, archived, or removed there; `gitlab-sync` leaves it alone for you to review.

A fully synced workspace shows `0` for both.

#### 2. `fetch` - Fetch All GitLab Projects

Retrieves all repositories from the specified GitLab group and caches them locally.

```bash
gitlab-sync fetch
```

This command:

- Uses the GitLab API with pagination to fetch all projects
- Includes subgroups automatically
- Skips archived repositories
- Caches results in `/tmp/gitlab_projects.txt` and `/tmp/gitlab_projects.json`

#### 3. `clone` - Clone Missing Repositories

Clones any repositories that exist in GitLab but are missing locally.

```bash
gitlab-sync clone
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
gitlab-sync update
```

This command:

- Fetches all remote branches
- Updates the current branch with latest changes from origin
- Handles detached HEAD states appropriately
- Reports repositories that are already up to date

#### 5. `branches` - Switch to Most Active Branches

Analyzes all repositories and switches them to their most active development branch.

```bash
gitlab-sync branches
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
gitlab-sync verify
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
gitlab-sync sync
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
gitlab-sync --work-dir /path/to/workspace sync
```

#### Custom GitLab Group

```bash
# Using config file (recommended)
# Edit .gitlab_sync.ini and set gitlab_group

# Or override with CLI argument
gitlab-sync --group my-gitlab-group sync
```

#### Combined Options

```bash
gitlab-sync --work-dir /home/user/dev --group your-gitlab-group status
```

#### Custom Config File

```bash
gitlab-sync --config /path/to/custom.ini sync
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
gitlab-sync update

# Output:
# [2026-06-16 10:00:00] ⊘ backend/services/api-gateway: Skipped branch switch (on working branch: feature/my-feature)
```

#### Scenario 2: Uncommitted Changes

```bash
# Repository has uncommitted changes
gitlab-sync update

# Output:
# [2026-06-16 10:00:00] ⊘ backend/services/api-gateway: Skipped (unsafe: Uncommitted changes detected)
```

#### Scenario 3: Auto-Stash Enabled

```bash
# Repository has uncommitted changes, auto-stash enabled
gitlab-sync --auto-stash update

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
gitlab-sync --safe-branches main,master,develop,staging --auto-stash update
```

### Disabling Safety Checks

If you want to disable safety checks (not recommended for production workflows):

```bash
# Disable all safety checks
gitlab-sync --no-protect-working-branches --no-require-clean-workspace update
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
   cd /home/user/work && gitlab-sync sync
   ```

### Basic Daily Sync

Run a full synchronization daily at 2 AM:

```bash
# Edit crontab
crontab -e

# Add the following line (replace paths as needed)
0 2 * * * cd /home/user/work && /usr/bin/gitlab-sync sync >> /tmp/gitlab_sync.log 2>&1
```

**Note**: This uses the configuration from `~/.gitlab_sync.ini`. No need to specify work_dir or gitlab_group in the cron command.

### Hourly Updates (No Branch Switching)

Update repositories hourly without changing branches (for CI/CD environments):

```bash
0 * * * * cd /home/user/work && /usr/bin/gitlab-sync update >> /tmp/gitlab_hourly.log 2>&1
```

### Weekly Full Sync with Branch Management

Run full sync including branch switching weekly on Sunday at 3 AM:

```bash
0 3 * * 0 cd /home/user/work && /usr/bin/gitlab-sync sync >> /tmp/gitlab_weekly.log 2>&1
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
0 2 * * * cd /home/user/work && /usr/bin/gitlab-sync --config ~/.gitlab_sync_primary.ini sync >> /tmp/gitlab_primary.log 2>&1

# Sync secondary workspace every 6 hours
0 */6 * * * cd /home/user/work && /usr/bin/gitlab-sync --config ~/.gitlab_sync_secondary.ini update >> /tmp/gitlab_secondary.log 2>&1
```

### Monitoring and Alerts

Add email notifications for failures:

```bash
# Create a wrapper script
cat > /home/user/scripts/gitlab_sync_wrapper.sh << 'EOF'
#!/bin/bash
cd /home/user/work
gitlab-sync sync >> /tmp/gitlab_sync.log 2>&1
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

## Best Practices

1. **Initial Setup**: Run `gitlab-sync sync` once to set up full workspace
2. **Regular Updates**: Use `gitlab-sync update` for frequent, fast updates
3. **Branch Management**: Run `gitlab-sync branches` periodically to stay on active branches
4. **Monitoring**: Check logs regularly for errors or failures
5. **Backup**: Commit workspace state to git before major branch switches
6. **Testing**: Test cron commands manually before adding to crontab
7. **Documentation**: Keep this documentation updated with any custom configurations
