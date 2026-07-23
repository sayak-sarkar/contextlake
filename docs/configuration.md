# Configuration

contextlake reads persistent settings from a config file, with CLI arguments overriding them. (Installing
contextlake is covered in the [Quickstart](quickstart.md); the knowledge-layer extras are in the
[Knowledge layer](knowledge-layer.md) overview.)

## Using configuration files

Configuration is loaded in this precedence order:

1. **Local config**: `.contextlake.ini` in the current directory (highest priority)
2. **Global config**: `~/.contextlake.ini` in the home directory
3. **Default values**: built-in defaults (lowest priority)
4. **CLI arguments**: override all other settings

> **Upgrading from `gitlab-sync`?** Your existing `~/.gitlab_sync.ini` / `.gitlab_sync.ini` (with its
> `[gitlab_sync]` section) is still read, and the knowledge store at `~/.gitlab-sync/` is reused as-is,
> nothing to migrate. New setups use `.contextlake.ini` and `~/.contextlake/`; the `gitlab-sync` command
> also still works as a deprecated alias.

An example `.contextlake.ini`:

```ini
[contextlake]
work_dir = ~/work
gitlab_group = your-gitlab-group
cache_dir = /tmp
clone_timeout = 300
fetch_timeout = 60
branch_timeout = 30
pull_timeout = 60
max_workers = 8
```

## Overriding on the command line

Any setting can be overridden per-invocation. The config file is the recommended home for persistent
values; use flags for one-off overrides:

```bash
contextlake --work-dir /path/to/workspace sync   # override work_dir
contextlake --group my-gitlab-group sync          # override the group
contextlake --config /path/to/custom.ini sync     # use a different config file
contextlake --work-dir /home/user/dev --group your-gitlab-group status  # combine
```

## Settings reference

| Setting | Description | Default | Example |
| --- | --- | --- | --- |
| `work_dir` | Working directory for repositories | `~/work` | `/home/user/projects` |
| `platform` | Platform to mirror: `gitlab`, `github`, `bitbucket`, `gitea` (+ `codeberg`/`forgejo` flavors) | `gitlab` | `github` |
| `group` | The group / org / workspace / owner to mirror (`gitlab_group` is its alias) | none | `your-org` |
| `gitlab_group` | GitLab group to synchronize | `your-gitlab-group` | `mycompany-group` |
| `token_env` | Env var holding the platform token | per platform (`GITHUB_TOKEN`, and so on) | `MY_TOKEN` |
| `api_base` | REST endpoint for self-hosted / enterprise instances | per platform | `https://github.example.com/api/v3` |
| `cache_dir` | Directory for cache files | `/tmp` | `~/.cache/contextlake` |
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
| `clone_method` | How repos are cloned: `auto` (git+token, else glab, else git), `git`, or `glab` | `auto` | `git` |
| `branch_strategy` | Most-active branch selection: `commits`, `recency`, or `hybrid` | `hybrid` | `recency` |

The branch-safety settings (`require_clean_workspace`, `protect_working_branches`, `safe_branches`,
`auto_stash`) live with [Mirror repositories](usage.md#branch-safety).

## See also

- [Quickstart](quickstart.md)
- [Mirror repositories](usage.md)
- [Knowledge layer](knowledge-layer.md)
