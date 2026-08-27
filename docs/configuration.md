# Configuration

contextlake reads persistent settings from a config file, with CLI arguments overriding them. (Installing
contextlake is covered in the [Quickstart](../QUICKSTART.md); the knowledge-layer extras are in the
[Knowledge layer](knowledge-layer.md) overview.)

Finding the file is a walk up the directory tree, not a lookup in one fixed place. A worked example,
run three directories below the config that ends up applying:

```mermaid
flowchart TD
  CWD(["you run contextlake in<br/>~/work/billing/core/src"]) --> W["no .contextlake.ini here,<br/>so step up a directory"]
  W --> HIT[("~/work/billing/core/.contextlake.ini,<br/>the nearest ancestor, and it wins")]
  HIT --> GL[("~/.contextlake.ini, consulted for<br/>anything the local file leaves out")]
  GL --> DEF["built-in defaults for the rest"]
  DEF --> FLAG(["a CLI flag overrides<br/>every layer above"])
```

<div class="dg-key">
  <i><b class="dg-sh-step"></b>a rectangle is something that runs</i>
  <i><b class="dg-sh-store"></b>a cylinder is something that persists</i>
  <i><b class="dg-sh-act"></b>a rounded box is a start or an end point</i>
</div>

The walk stops at the filesystem root, and the *nearest* ancestor wins if projects are nested.

## Using configuration files

Configuration is loaded in this precedence order:

1. **Local config**: the nearest ancestor directory's `.contextlake.ini`, walking up from the current
   directory to the filesystem root (like git's `.git` discovery), highest priority. A config at your
   project root is inherited by every subdirectory underneath it; you don't need to be in the exact
   directory that holds the file.
2. **Global config**: `~/.contextlake.ini` in the home directory
3. **Default values**: built-in defaults (lowest priority)
4. **CLI arguments**: override all other settings

Run `contextlake init --local` inside a project to create that project's local config
(`.contextlake.ini` + `.contextlake/kb.toml`'s knowledge-layer equivalent, `.contextlake.kb.toml`) instead
of the global one, see [Directory-scoped config](#directory-scoped-config) below.

An example `.contextlake.ini`:

```ini
[contextlake]
work_dir = ~/work
gitlab_group = your-gitlab-group

# Which repositories to mirror. Omit to take the whole group.
repo_filter = team/*,shared-libs

# Which branch to put them on. Omit both to track each repo's most active branch.
branch = release/24.1
branch_map = team/api=develop,legacy-*=maintenance

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
contextlake --work-dir /path/to/workspace mirror sync   # override work_dir
contextlake --group my-gitlab-group mirror sync          # override the group
contextlake --config /path/to/custom.ini mirror sync     # use a different config file
contextlake --work-dir /home/user/dev --group your-gitlab-group mirror status  # combine
```

## Directory-scoped config

A single global config (`~/.contextlake.ini` + `~/.contextlake/kb.toml`) works well when you only ever
mirror one org into one workspace. If you work across several orgs or projects, each with its own
platform, group, or knowledge-layer sources, scope a config to a project instead:

```bash
cd ~/work/some-project
contextlake init --local        # writes .contextlake.ini + .contextlake.kb.toml here
contextlake kb source add jira --type atlassian --local   # scopes a source the same way
```

Every command run from that directory, or any subdirectory under it, picks up the local config
automatically. You do not need to pass `--config`, and you do not need to be in the exact
directory the file lives in.

Resolution walks up from the current directory to the filesystem root, looking for
`.contextlake.ini` or `.contextlake.kb.toml`. That is the same way `git` finds `.git` from
anywhere inside a repo.

If more than one project is nested, the **nearest** ancestor wins.

`--local` on `source add`/`remove`/`enable`/`disable` targets that same nearest-ancestor file (creating
one in the current directory if none exists yet in the chain); once a project has a local config, those
commands land in it by default without needing `--local` again. An explicit `--config PATH` always takes
precedence over both the local and global tiers.

`init --local` also scopes one of the *values* it writes, not just the config file's location: the
knowledge-layer's `store_dir` defaults to a `.contextlake/kb` directory next to the workspace instead
of the global `~/.contextlake/kb` (override with `--store-dir`), so two separate `--local` projects
never end up sharing one store. `work_dir` is not part of that: **every** `contextlake init` defaults
it to the current directory, `--local` or not (override interactively, or with `--work-dir`).

## Workspace trust

Because a local config is *discovered* rather than named, one can take effect that you never wrote:
notably a `.contextlake.kb.toml` that came inside a repository you cloned. So the few config keys that
become part of a command contextlake executes are honoured **only** from the global
`~/.contextlake/kb.toml` or from a path you pass to `--config`:

| Key | Reaches |
| --- | --- |
| `[llm] command`, `[llm] args` | the agent CLI run when `provider = "cli"` |
| `[llm] provider`, `[llm] review_provider` | only when set to `"cli"` |
| `[[sources]] command`, `args`, `mcp_command` | the MCP server spawned over stdio |

One key is gated by **direction** rather than outright. A discovered file may set
`[kb] anonymize = "always"` and may not set it to `"never"`: turning anonymising on is
always allowed, turning off the setting that hides contributor identities on a dashboard
you are about to share is not. Same provenance hole, a privacy answer instead of an
execution one.

In a discovered `.contextlake.kb.toml` those keys are ignored, with a warning naming the file and the
key. Nothing else changes: `store_dir`, `languages`, `max_file_bytes`, `[embeddings]`, `[[rules]]`, and
any non-`cli` LLM provider all keep working from a local file exactly as before. The one thing to know
when scoping sources with `contextlake kb source add ... --local` is that an *MCP* source's `command`
has to live in the global file (or be reached with `--config`); an `mcp` **URL** is fine locally.

Set `CONTEXTLAKE_NO_LOCAL_CONFIG=1` to skip ancestor discovery entirely, for both `.contextlake.ini` and
`.contextlake.kb.toml`, recommended in CI, containers, and anywhere untrusted checkouts are processed in
bulk. With it set, `source add --local` writes to the global config too, rather than to a local file that
would never be read. See [SECURITY.md](../SECURITY.md#workspace-trust) for the full model.

## Settings reference

| Setting | Description | Default | Example |
| --- | --- | --- | --- |
| `work_dir` | Working directory for repositories | `~/work` | `/home/user/projects` |
| `platform` | Platform to mirror: `gitlab`, `github`, `bitbucket`, `gitea` (+ `codeberg`/`forgejo` flavors) | `gitlab` | `github` |
| `group` | The group / org / workspace / owner to mirror (`gitlab_group` is its alias) | none | `your-org` |
| `gitlab_group` | GitLab group to synchronize | `your-gitlab-group` | `mycompany-group` |
| `token_env` | Env var holding the platform token | per platform (`GITHUB_TOKEN`, and so on) | `MY_TOKEN` |
| `gitlab_token_env` | GitLab-specific alias for `token_env`; checked first, then `token_env`, then `GITLAB_TOKEN` | `GITLAB_TOKEN` | `MY_GITLAB_PAT` |
| `api_base` | REST endpoint for self-hosted / enterprise instances | per platform | `https://github.example.com/api/v3` |
| `gitlab_host` | GitLab host for the REST API. The `GITLAB_HOST` env var wins over it | `gitlab.com` | `gitlab.example.com` |
| `repo_filter` | Comma-separated glob patterns limiting every command to matching repositories, the permanent form of `--repos` (see [Branch safety and scoping](mirroring-repositories.md)) | none, meaning every repository | `team/*,shared-libs` |
| `network_timeout` | HTTP timeout (seconds) for REST API enumeration | `30` | `60` |
| `dns_timeout` | Per-lookup DNS timeout (seconds) for child git operations, applied through `RES_OPTIONS`; skipped entirely if you already export `RES_OPTIONS` | `15` | `30` |
| `dns_attempts` | DNS retry attempts, set alongside `dns_timeout` and subject to the same `RES_OPTIONS` rule | `3` | `5` |
| `cache_dir` | Directory for cache files. Unset, the cache goes to a per-workspace subdirectory of `~/.cache/contextlake` (`$XDG_CACHE_HOME/contextlake` when set), created `0700`. Set it and that exact directory is used, with no subdirectory. | `~/.cache/contextlake/<workspace>-<id>` | `/srv/cache` |
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
| `branch` | Pin every repository to this branch instead. Empty keeps the selection above; a repository without the branch is reported and left alone | *(empty)* | `release/24.1` |
| `branch_map` | Per-repository pins, for when one branch does not fit the whole fleet. Comma-separated `pattern=branch` pairs using the same globs as `repo_filter`. First match wins, so list the specific before the general. Beats `branch`; falls back to it, then to the selection above | *(empty)* | `team/api=develop,legacy-*=maintenance` |
| `schedule_interval` | `auto` measures and adjusts; a duration (`45s`, `30m`, `2h`, `7d`) pins it and turns auto-adjust off | `auto` | `2h` |
| `schedule_min` | Lower bound the auto-adjusted interval is clamped to. Not applied to a pinned `schedule_interval` | `1h` | `30m` |
| `schedule_max` | Upper bound the auto-adjusted interval is clamped to. Not applied to a pinned `schedule_interval` | `24h` | `12h` |
| `schedule_duty_cycle` | Share of wall-clock time a run may occupy, as a fraction above 0 and below 1 | `0.10` | `0.05` |
| `schedule_full_every` | How long since the last successful full rebuild before the next cycle forces one (`bootstrap --force`) | `7d` | `3d` |
| `schedule_adjust_threshold` | How far the installed interval must drift from the current recommendation before `status` flags it | `0.5` | `0.25` |
| `schedule_gate_retry` | How long a gated (skipped) cycle waits before trying again | `10m` | `5m` |
| `schedule_on_battery` | `skip` refuses a run on battery power; `run` ignores battery state | `skip` | `run` |
| `schedule_require_idle` | Refuse a run unless the user is idle. Cannot be detected under a systemd timer or cron: see [Scheduling runs](scheduling.md#platform-differences) | `false` | `true` |
| `schedule_max_load` | Refuse a run when the 1-minute load average exceeds this. Empty turns the check off | *(empty)* | `4.0` |

Full detail, the interval formula, and what each command writes are on [Scheduling
runs](scheduling.md).

A worked block with all ten together:

```ini
[contextlake]
schedule_interval = auto
schedule_min = 1h
schedule_max = 24h
schedule_duty_cycle = 0.10
schedule_full_every = 7d
schedule_adjust_threshold = 0.5
schedule_gate_retry = 10m
schedule_on_battery = skip
schedule_require_idle = false
schedule_max_load =
```

The branch-safety settings (`require_clean_workspace`, `protect_working_branches`, `safe_branches`,
`auto_stash`) live with [Mirror repositories](mirroring-repositories.md#branch-safety).

## See also

- [Quickstart](../QUICKSTART.md)
- [Mirror repositories](mirroring-repositories.md)
- [Knowledge layer](knowledge-layer.md)
- [Scheduling runs](scheduling.md), the `schedule_*` keys in depth
