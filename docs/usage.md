# Mirror repositories

Mirror your Git repositories locally and keep them current: fetch, clone, update, switch to the most
active branch, verify, and audit, with branch-safety guardrails and scheduling. New here? Start with the
[Quickstart](../QUICKSTART.md). For settings see [Configuration](configuration.md); for the knowledge
layer see the [Knowledge layer](knowledge-layer.md) overview.

> [!NOTE]
> Knowledge-layer commands (`index`, `connect`, `embed`, `wiki`, `query`, `graph`, and so on) are
> documented in the [Knowledge layer](knowledge-layer.md) overview and the
> [command reference](cli-reference.md). This page covers the mirror and sync commands.

## Command reference

Every command carries its own scoped help, `contextlake <command> --help` shows
just that command's flags with worked examples, and bare `contextlake` prints the
full command list.

### Shell completion

On by default, and set up automatically, no command to remember. `argcomplete` is a core
dependency, so `pip install contextlake` alone is enough, no separate extra; a plain `pip install`
has no post-install hook to run anything at (that's a deliberate Python packaging limitation, not a
gap here), so instead the **first time any command runs in a real interactive terminal**,
contextlake registers completion for you, once, and says so in the log. Skipped entirely in
non-interactive contexts (CI, Docker, a piped command) where there's no shell to configure, set
`CONTEXTLAKE_NO_AUTO_COMPLETION=1` to opt out of this check altogether. An **interactive**
`contextlake init` offers the same registration explicitly (on by default; pass `--no-completion`
to skip) if you'd rather decide up front, and an explicit decline there is remembered, the
automatic check never overrides it later. A **non-interactive** `init` (`--skip-interactive`, or a
piped stdin) never touches your shell startup file: nobody was asked, so pass `--completion` to opt
in, or run `contextlake completion` afterwards.

Run it again on demand (a different shell, after skipping it once, or just to re-run it) with:

```bash
contextlake completion          # auto-detect $SHELL and register
contextlake completion zsh      # register for zsh explicitly, regardless of $SHELL
```

This, `init`'s own prompt, and the automatic first-run check all write whichever of these applies,
once, idempotently:

```bash
# bash — appended to ~/.bashrc
eval "$(register-python-argcomplete contextlake)"

# zsh — appended to ~/.zshrc (needs bashcompinit; most zsh setups already load it)
autoload -U bashcompinit && bashcompinit
eval "$(register-python-argcomplete contextlake)"

# fish — a dedicated file, written once
register-python-argcomplete --shell fish contextlake > ~/.config/fish/completions/contextlake.fish
```

For a shell other than bash/zsh/fish, or to do it by hand, copy the block above for your shell and
open a new shell afterward. `contextlake <TAB>` then completes every command and, inside a command,
every one of its flags, generated live from the same parser that runs the command, so it can never
drift out of sync with the actual CLI surface. Uses
[argcomplete](https://github.com/kislyuk/argcomplete) (pure Python, no dependencies of its own).

**Mirror a subset with `--repos`.** Every mirror command (and `bootstrap` / `kb index
--workspace`) accepts `--repos PATTERN`, a comma-separated **glob/substring** filter
over your repo paths, so you can mirror and index just a handful instead of the whole
group. Ideal for a demo or a try-before-fleet run:

```bash
contextlake bootstrap --repos "team/api,billing,frontend/*"   # mirror + index just these
contextlake mirror sync --repos "team/*"                             # sync one namespace
contextlake kb index --workspace ~/work --repos "billing/core,team/api"
```

Each pattern matches if it's a substring of, or a glob against, the repo's
group-qualified path or its local path (case-insensitive). It scopes the whole
pipeline: `fetch` narrows the cached project list, and `clone` / `update` / `branches`
/ `verify` / `status` / `bootstrap` all follow from that.

**Preview with `-n` / `--dry-run`.** Every mirror command accepts `-n` (the short form, matching the
near-universal `rm`/`cp`/`make` convention) to show what would happen without cloning, updating, or
switching a single branch:

```bash
contextlake mirror update -n
# DRY RUN: no repositories will be cloned, updated, or switched
```

Pair it with `--repos` to preview a change scoped to just the repos you're about to touch.

`contextlake mirror sync` runs the whole mirror pipeline end to end; each stage is also
available as its own command:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/pipeline-sync.png" alt="The contextlake mirror sync pipeline: fetch, then clone, then update, then branches, then verify, then audit." width="760">
</p>

### `mirror status`: check current synchronization status

Shows the current state of your workspace compared to GitLab.

```bash
contextlake mirror status
```

**Example output:**

```text
GitLab projects (cached): 128      # repos you can see on GitLab
Local repositories:       128      # repos cloned in your workspace
Synchronized:             127      # present in both and matching
Missing:                  1        # on GitLab but not cloned yet
Extra:                    1        # cloned locally but not on GitLab
```

- **Missing** = a repo exists on GitLab but isn't in your workspace, `clone` (or
  `sync`) will fetch it.
- **Extra** = a repo is in your workspace but not on GitLab, usually one that was
  renamed, archived, or removed there; `contextlake` leaves it alone for you to review.

A fully synced workspace shows `0` for both.

### `mirror fetch`: fetch all GitLab projects

Retrieves all repositories from the specified GitLab group and caches them locally.

```bash
contextlake mirror fetch
```

This command:

- Uses the GitLab API with pagination to fetch all projects
- Includes subgroups automatically
- Skips archived repositories
- Caches results in `gitlab_projects.txt` and `gitlab_projects.json`, under a
  per-workspace subdirectory of `~/.cache/contextlake` (override with `cache_dir`)

### `mirror clone`: clone missing repositories

Clones any repositories that exist in GitLab but are missing locally.

```bash
contextlake mirror clone
```

This command:

- Compares cached GitLab projects with local repositories
- Creates directory structure matching GitLab's group/subgroup hierarchy
- Uses HTTPS cloning for better authentication
- Clones up to 8 repositories concurrently
- Handles timeouts gracefully (300s per repository)

How each repo is cloned (`clone_method = auto`, the default): with `GITLAB_TOKEN`
set, contextlake clones with plain `git`, passing the token as an auth header
through the child environment, never on the command line and never in the URL, so
it can't leak into `ps` output or `.git/config`. Without a token it uses `glab repo
clone` (glab's own auth) when glab is installed, else plain `git clone` over HTTPS.
Set `clone_method = git` or `glab` to force one path.

### `mirror update`: update existing repositories

Fetches and pulls the latest changes for all local repositories.

```bash
contextlake mirror update
```

This command:

- Fetches all remote branches
- Updates the current branch with latest changes from origin
- Handles detached HEAD states appropriately
- Reports repositories that are already up to date

### `mirror branches`: switch to most active branches

Analyzes all repositories and switches them to their most active development branch.

```bash
contextlake mirror branches
```

This command:

- Fetches all remote branches for each repository
- Calculates commit count for each branch
- Identifies the branch with the most commits (most active)
- Switches to the most active branch if different from current
- Pulls latest changes after switching

**Branch selection:** the default `branch_strategy = "hybrid"` scores each branch on a
weighted blend of **60% normalized commit count + 40% normalized recency**, so a branch
that is both busy and recently touched wins. Two alternatives exist: `commits` (highest
commit count, the legacy behaviour) and `recency` (most recent commit). Archived repos,
repos without branches, and detached-HEAD states are skipped.

### `mirror verify`: verify repository structure

Checks that the local workspace structure matches GitLab exactly.

```bash
contextlake mirror verify
```

This command:

- Compares local repositories with GitLab project list
- Identifies nested `.git` directory structures (indicates incorrect cloning)
- Lists extra local repositories (not in GitLab)
- Lists missing repositories (in GitLab but not local)
- Reports synchronization status

### `mirror sync`: full synchronization

Runs the complete synchronization pipeline in sequence.

```bash
contextlake mirror sync
```

This command executes:

1. `fetch` - Get latest GitLab project list
2. `clone` - Clone missing repositories
3. `update` - Update existing repositories
4. `branches` - Switch to active branches
5. `verify` - Verify structure
6. `audit` - Report repo health & age (skip with `--no-audit`)

### `mirror audit`: repo health & age report

Scans every local clone and reports which repos are effectively empty and how old/active
they are. Runs automatically at the end of `sync`/`bootstrap`, or on demand:

```bash
contextlake mirror audit                       # summary to console + report to <cache_dir>/repo_audit.json
contextlake mirror audit --report ./audit.json # choose where the per-repo JSON + .csv are written
contextlake mirror sync --no-audit             # run sync without the audit step
```

It classifies each repo as **empty** (no commits/files), **readme-only** (just a template
README), **boilerplate** (only meta files), or **content**, and reports each repo's
**creation date** (GitLab `created_at`, captured during fetch; falls back to the first git
commit) and **last commit date** (from the local clone), with an aggregate summary
(counts, oldest/newest, how many stale over 1–2 years, repos with no commits). The full
per-repo table is written as JSON **and** CSV. The scan is parallel, read-only, and works
offline from the fetch cache.

## Configuration

contextlake's config-file precedence and the full settings reference now have their own page: **[Configuration](configuration.md)**.

## Branch safety

The tool protects your local work without getting in your way. The guiding rule:
**a clean repo is always safe to act on, the branch name alone never causes a skip.**
The only thing that blocks an `update` is a *dirty working tree*.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/branch-safety.png" alt="Branch-safety decision: a dirty working tree is skipped (or stashed if auto_stash); branches stays off a non-safe branch when protect_working_branches is set; otherwise contextlake acts, update pulls and branches switches." width="720">
</p>

### Safety checks

1. **Clean Workspace Check** (the main guard): detects a dirty working tree,
   uncommitted, unstaged, or untracked changes. A dirty repo is skipped by both
   `update` and `branches` so local work is never clobbered.
2. **Automatic Stashing**: optionally stashes a dirty tree so `update` can proceed
   instead of skipping.
3. **Working-Branch Protection** (applies to `branches` only): keeps the `branches`
   command from switching a repo off a branch outside `safe_branches`, so you are
   never moved off a feature branch you are working on. This does **not** affect
   `update`, a clean feature branch is still pulled.

### Configuration

| Setting | Description | Default |
| --- | --- | --- |
| `require_clean_workspace` | Skip repos with a dirty working tree (the main guard) | `true` |
| `protect_working_branches` | Keep `branches` from switching a repo off a non-safe branch | `true` |
| `safe_branches` | Branches the `branches` command may switch away from | `main,master,develop,development` |
| `auto_stash` | Stash a dirty tree before `update` instead of skipping | `false` |
| `branch_strategy` | How `branches` picks the most-active branch: `hybrid` (60% commits + 40% recency), `commits`, or `recency` | `hybrid` |

### Behavior

**`update` (fetch + fast-forward the current branch):**

- A **clean** repo is updated on whatever branch it is on, feature branches included.
- A repo with a **dirty working tree** is skipped (or stashed first, if `auto_stash` is on).

**`branches` (switch to the most active branch):**

- A repo with a **dirty working tree** is skipped.
- With `protect_working_branches = true`, a repo on a branch outside `safe_branches`
  is left where it is instead of being switched away.

### Example scenarios

#### Scenario 1: Working-Branch Protection (branches command)

```bash
# Repository is on feature/my-feature branch (not in safe branches)
contextlake mirror branches

# Output:
# [2026-06-16 10:00:00] ⊘ backend/services/api-gateway: Skipped branch switch (on working branch: feature/my-feature)
```

> A plain `contextlake mirror update` would instead **pull `feature/my-feature`** here,
> since the working tree is clean.

#### Scenario 2: Uncommitted Changes

```bash
# Repository has uncommitted changes
contextlake mirror update

# Output:
# [2026-06-16 10:00:00] ⊘ backend/services/api-gateway: Skipped (unsafe: Uncommitted changes detected)
```

#### Scenario 3: Auto-Stash Enabled

```bash
# Repository has uncommitted changes, auto-stash enabled
contextlake --auto-stash mirror update

# Output:
# [2026-06-16 10:00:00] ⚠ backend/services/api-gateway: Changes stashed successfully
# [2026-06-16 10:00:00] ✓ backend/services/api-gateway: Updated main
```

### Customization

These resilience/safety flags are kept out of `contextlake <command> --help`'s default listing (run
`contextlake mirror update --help-advanced` to see them alongside every other mirror-tier command's flags) --
they're automation levers, not something to guess at interactively, and every one has the
`.contextlake.ini` equivalent below as its primary home.

You can customize branch safety behavior via configuration or CLI:

```ini
# In .contextlake.ini
[contextlake]
protect_working_branches = true
safe_branches = main,master,develop,staging
require_clean_workspace = true
auto_stash = false
```

```bash
# Or via CLI
contextlake --safe-branches main,master,develop,staging --auto-stash mirror update
```

### Disabling safety checks

If you want to disable safety checks (not recommended for production workflows):

```bash
# Disable all safety checks
contextlake --no-protect-working-branches --no-require-clean-workspace mirror update
```

**Warning**: Disabling safety checks can lead to conflicts, lost work, or corruption of your local branches. Only disable if you understand the risks.

## Scheduling and automation

### Prerequisites for cron jobs

Before setting up cron jobs, ensure you have:

1. **Configuration file set up**: Create `~/.contextlake.ini` with your settings

   ```bash
   contextlake init                       # writes the file for you, prompting for each value
   # or copy the template and fill it in by hand:
   cp .contextlake.ini.example ~/.contextlake.ini
   # Edit with your work_dir and gitlab_group
   nano ~/.contextlake.ini
   ```

2. **Absolute path to script**: Cron requires absolute paths

   ```bash
   which python3  # Note the path
   # Example: /usr/bin/python3
   ```

3. **Test the command manually first**:

   ```bash
   cd /home/user/work && contextlake mirror sync
   ```

### Basic daily sync

Run a full synchronization daily at 2 AM:

```bash
# Edit crontab
crontab -e

# Add the following line (replace paths as needed)
0 2 * * * cd /home/user/work && /usr/bin/contextlake mirror sync >> /tmp/contextlake.log 2>&1
```

**Note**: This uses the configuration from `~/.contextlake.ini`. No need to specify work_dir or gitlab_group in the cron command.

### Hourly updates (no branch switching)

Update repositories hourly without changing branches (for CI/CD environments):

```bash
0 * * * * cd /home/user/work && /usr/bin/contextlake mirror update >> /tmp/gitlab_hourly.log 2>&1
```

### Weekly full sync with branch management

Run full sync including branch switching weekly on Sunday at 3 AM:

```bash
0 3 * * 0 cd /home/user/work && /usr/bin/contextlake mirror sync >> /tmp/gitlab_weekly.log 2>&1
```

### Multiple workspaces

For multiple workspaces, use separate config files:

```bash
# Create workspace-specific config files
cat > ~/.contextlake_primary.ini << EOF
[contextlake]
work_dir = ~/work
gitlab_group = example-group-primary
EOF

cat > ~/.contextlake_secondary.ini << EOF
[contextlake]
work_dir = ~/Projects/Secondary
gitlab_group = example-group-secondary
EOF

# Add to crontab

# Sync primary workspace daily
0 2 * * * cd /home/user/work && /usr/bin/contextlake --config ~/.contextlake_primary.ini mirror sync >> /tmp/gitlab_primary.log 2>&1

# Sync secondary workspace every 6 hours
0 */6 * * * cd /home/user/work && /usr/bin/contextlake --config ~/.contextlake_secondary.ini mirror update >> /tmp/gitlab_secondary.log 2>&1
```

### Monitoring and alerts

Add email notifications for failures:

```bash
# Create a wrapper script
cat > /home/user/scripts/contextlake_wrapper.sh << 'EOF'
#!/bin/bash
cd /home/user/work
contextlake mirror sync >> /tmp/contextlake.log 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "GitLab sync failed with exit code $EXIT_CODE" | mail -s "GitLab Sync Failure" user@example.com
fi
EOF
chmod +x /home/user/scripts/contextlake_wrapper.sh

# Add to crontab
0 2 * * * /home/user/scripts/contextlake_wrapper.sh
```

### Exit codes

`mirror fetch`, `clone`, `update`, `branches`, `verify` and `sync` exit `0` when nothing failed and
`1` when anything did, including a partial run, where some repositories synced and others did not.
`sync` aggregates across all five stages, so one failed clone fails the whole run, and `bootstrap`
counts a failed mirror stage the same way it counts a failed knowledge-layer stage. (`mirror status`
and `mirror audit` only report; they do not fail on what they find.)

Work that was deliberately skipped (already up to date, a protected working branch, `--dry-run`) is
never a failure, and `mirror verify` fails only on a cloned path that is not a valid git repository,
not on repos that are merely missing or extra.

`Ctrl-C` exits `130`; a bad `--config` path exits `1`.

Pass `--exit-zero-on-partial` to exit `0` anyway when some repositories failed, the failures are
still reported, they just do not fail the job.

`--verbose` also changes what a crash leaves behind: the top-level handler re-raises instead of
printing `Error: <message>` alone, so a bug report can carry the traceback without anyone having to
reproduce the failure under a debugger.

### Observing an unattended run

An exit code tells you *that* something broke; these four flags tell you *what*, after the fact.

```bash
0 2 * * * cd /home/user/work && /usr/bin/contextlake mirror sync \
    --log-format json --log-file /var/log/contextlake/run.log \
    --metrics-file /var/lib/node_exporter/textfile/contextlake.prom
```

- **`--log-format json`** emits one JSON object per line, each stamped with a `run_id` (pin your own
  with `CONTEXTLAKE_RUN_ID`), the `command`, and, on per-repo lines, `repo`, `status`,
  `duration_ms`, and `error_type` on failures.
- **`--metrics-file`** writes Prometheus textfile-collector output: run duration, exit code, repo
  counts by outcome, graph size, and a last-success timestamp that survives a failing run. Point
  node_exporter's `--collector.textfile.directory` at that directory.
- **`--log-file`** is redacted by default (workspace paths, `$HOME`, group and repository names), so
  the copy you attach to a bug report needs no manual scrubbing. `--redact` extends that to the
  console; `--no-redact` turns it off entirely.
- **`--access-log`** turns on request logging for the local HTTP servers (`kb dashboard`,
  `kb graph --serve`, `kb serve --transport http|sse`), which are otherwise silent.

See [Reading the console output](console-output.md) for the exact JSON shape and metric names, and
[`examples/contextlake.service`](../examples/contextlake.service) for a systemd unit wired up this way.

### Log rotation

To prevent log files from growing indefinitely, set up log rotation:

```bash
# Create logrotate configuration
sudo cat > /etc/logrotate.d/contextlake << 'EOF'
/tmp/contextlake.log {
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

## Troubleshooting

The mirror's symptom table, along with the install-side problems, now lives on one page:
[Troubleshooting](troubleshooting.md#the-mirror). Keeping it there means a reader who is
stuck looks in one place rather than guessing which page owns their symptom.

## Best practices

1. **Initial Setup**: Run `contextlake mirror sync` once to set up full workspace
2. **Regular Updates**: Use `contextlake mirror update` for frequent, fast updates
3. **Branch Management**: Run `contextlake mirror branches` periodically to stay on active branches
4. **Monitoring**: Check logs regularly for errors or failures
5. **Backup**: Commit workspace state to git before major branch switches
6. **Testing**: Test cron commands manually before adding to crontab
7. **Documentation**: Keep this documentation updated with any custom configurations
