# Scheduling runs

`contextlake schedule` measures how long a run takes and how often your repositories change,
works out an interval from those two numbers, and installs a background job that keeps the
mirror and the knowledge layer current on its own. It runs on the core tier, so it works even
without the `[kb]` extra. This page covers reading its recommendation, installing it, the
set/reset/unset model, ad-hoc jobs, the interval formula, its config keys, platform differences,
and containers.

## Prerequisites

- A configured workspace: `contextlake init`, or `.contextlake.ini` in place. See
  [Configuration](configuration.md).
- Linux, with either a systemd user session or `crontab` available. Nothing else is supported
  yet; see [Platform differences](#platform-differences).

## `schedule recommend`: what interval your measurements suggest

```console
$ contextlake schedule recommend
⚠ Recommended interval: 6h
  Because: no measured runs yet, so this is the built-in default of 6h, not a measurement. The first completed run replaces it.
  Nothing has been measured yet. Run `contextlake mirror sync` or `contextlake bootstrap` once, or install the schedule and let the first run replace this default.

  Install it:  contextlake schedule install
```

That is the cold-start default: nothing has run yet, so there is nothing to measure. Once a few
runs have completed, the same command reads real numbers:

```console
$ contextlake schedule recommend
✓ Recommended interval: 70m
  Because: duty cycle: the median incremental run takes 7m, and at 10% of wall-clock time that needs 70m between runs
  From 3 measured run(s) over 0.1 day(s)
    duty-cycle floor: 70m
    activity floor:   20m

  Install it:  contextlake schedule install
```

The glyph names the answer: `⚠` for a default nobody has measured yet, `✓` for one built from
real runs. `recommend` changes nothing. Add `--json` for the same answer as a
machine-readable payload (`interval`, `basis`, `reason`, `floor_duty_seconds`,
`floor_activity_seconds`, `history`, and so on).

## `schedule install`: measure, decide, and install

```bash
contextlake schedule install
```

This is idempotent: run it again after a config change or after more runs have accumulated, and
it recomputes and rewrites the job in place. It writes to three places:

| What | Where |
| --- | --- |
| The job record (name, command, interval setting, failure count, last run) | `schedule-jobs.json`, beside the project cache |
| The measured run history, appended to by every run | `schedule-history.jsonl`, beside the project cache |
| The platform unit that fires | a systemd user timer (`~/.config/systemd/user/contextlake-<name>.timer` + `.service`), or a marked block in your crontab |

"Beside the project cache" is the same directory `cache_dir` in [Configuration](configuration.md)
resolves to: `~/.cache/contextlake/<workspace>-<id>` by default.

`install` picks systemd if a systemd user session answers, cron otherwise, and prints the
platform's own unit file (or crontab line) when it cannot write one, so you can install it by
hand. Force a specific platform with `--platform systemd` or `--platform cron`.

The default job runs `contextlake bootstrap` on most cycles, and switches to
`contextlake bootstrap --force` (a full rebuild: every repository re-parsed, every node
re-embedded) once `schedule_full_every` has passed since the last successful full run.

## Set, reset, and unset

Three layers decide what interval a job runs on, and only one of them is read back as the truth:

| Layer | Role | Changed by |
| --- | --- | --- |
| The INI (`schedule_interval`, or `--interval` on `install`) | Supplies the starting value **when the job is created**. Ignored after that. | Editing `.contextlake.ini` before the first `install` |
| The job record (`schedule-jobs.json`) | **Authoritative.** Holds `auto` or a pinned duration, plus the failure count that drives backoff. | `install`, `interval`, `reset`, `uninstall` |
| The platform unit (the systemd timer or the crontab line) | Rendered *from* the job record's resolved interval. Never read back as truth; `status` reads it only to report drift or cron's rounding. | Rewritten every `install` / `reset` / `interval` |

Three commands change the job record:

- **Set** an interval with `contextlake schedule --interval 2h install` (or pin an ad-hoc job the
  same way with `schedule interval 2h run -- ...`). Auto-adjust turns off for that job: the
  number you gave is used as-is, never clamped to `schedule_min` / `schedule_max`.
- **Reset** with `contextlake schedule reset`. Clears a pin back to `auto`, clears the failure
  count (so a backed-off interval snaps back to the recommendation), recomputes, and reinstalls
  immediately. Add `--history` to also discard the measurements (see below); `reset` installs
  first and discards second, so a failed install never destroys history for a reset that did not
  happen.
- **Unset** with `contextlake schedule uninstall`. Removes the job record and the installed unit.
  Add `--purge` to also discard the measurements. Without `--purge`, the history is kept, so
  installing again later starts warm instead of cold.

### Discarding history is recoverable

`--purge` and `reset --history` both ask first, printing the count of runs and the span of days
they cover, because a useful median takes days to earn back:

```console
$ contextlake schedule --history --yes reset
About to discard 3 measured run(s) spanning 0.1 day(s) (2026-08-24T01:00:00Z to 2026-08-24T03:00:00Z).
  The recommender starts cold and re-learns from the next run.
✓ Discarded 3 measured run(s).
  Saved to /home/you/.cache/contextlake/work-ab12/schedule-history.jsonl.discarded in case you want it back.
```

The discard renames the history file to a `.discarded` sidecar instead of deleting it. To get it
back, move the sidecar over the live file yourself:

```bash
mv schedule-history.jsonl.discarded schedule-history.jsonl
```

Only one sidecar is kept. A second discard replaces it, so this is undo for the discard you just
did, not an archive of every one you have ever run.

## Ad-hoc jobs

`schedule interval` creates or replaces a named job that runs any contextlake command on its own
interval, separate from the default `bootstrap` cycle. Give it a duration (or `auto`), the word
`run`, a `--` separator, then the command:

```bash
contextlake schedule interval 6h run -- kb wiki --force
contextlake schedule --job wiki-refresh interval 2h run -- kb wiki --namespace acme/widgets
```

**The `--` separator is not optional the moment your command has its own flags.** `schedule`
parses everything before `--` as its own flags and action; without it, a flag meant for the
trailing command is read as a flag of `schedule` itself and the whole thing fails:

```console
$ contextlake schedule interval 6h run kb wiki --force
✗ '--force' isn't a flag on 'schedule'

It's used by: bootstrap, kb embed, kb index, init, kb steer, kb wiki.

Run 'contextlake schedule --help' to see schedule's own flags.
```

A flag that no other contextlake command uses fails the same way, with `Unknown flag: '<flag>'
(on 'schedule')` instead. Either way, the fix is the same: add `--`.

The command is validated before the job is written: it must parse, and if it needs the `[kb]`
extra and that extra is not installed here, `interval` warns rather than installing a job that
fails on every run. A job named `schedule` itself is refused outright, because that would recurse.

Every job appends to the same history file, and each record carries the job that wrote it.
Reads are scoped to one job: a job's full-rebuild schedule and its measured duration come from
its own runs only. Without that, a rebuild by one job satisfied `schedule_full_every` for all
of them, and a two-minute `kb index` and a forty-minute `bootstrap` shared one median. Records
written before the job name was recorded carry no job and count as the default job's.

## The interval formula

```
floor_duty     = median(incremental run duration) / duty_cycle
floor_activity = k / change_rate
interval       = clamp(max(floor_duty, floor_activity), schedule_min, schedule_max)
```

`floor_duty` is a cost bound: never occupy more than `duty_cycle` of wall-clock time.
`floor_activity` is a freshness bound: there is no point running more often than the fleet
produces roughly `k` changed repositories. Whichever bound needs the longer gap wins.

`floor_activity` needs a measurement that only the index stage produces: how many
repositories changed on a run. That stage is part of the knowledge layer, so on an install
without the `kb` extra nothing ever records it and the activity bound never engages. The
interval then rests on `floor_duty` alone, which is correct behaviour rather than a fault.
`contextlake schedule recommend` says so on the `activity floor` line, and its `--json`
output carries an `activity` field reading `not-measured`, `no-change` or `measured`.

Three worked examples, all at the default `duty_cycle = 0.10` and `k = 1.0`:

| Median incremental run | Change rate | `floor_duty` | `floor_activity` | Interval | Why |
| --- | --- | --- | --- | --- | --- |
| 7 min | ~3 repos/hour | 70m | 20m | **70m** | Duty wins: a busy fleet, so the cost bound is what limits how often you can afford to run |
| 7 min | ~0.1 repos/hour | 70m | 10h | **10h** | Activity wins: a quiet fleet, so there is nothing to gain from running every 70 minutes |
| 40 min | ~3 repos/hour | 6.7h | 20m | **6.7h** | Duty wins again: a bigger, slower fleet costs more per run, which raises the cost bound past the freshness one |

## Configuration

Ten `schedule_*` keys, read from the same `.contextlake.ini` `[contextlake]` section as
`work_dir` and the rest of the mirror settings. Full descriptions and defaults are in
[Configuration](configuration.md#settings-reference); the worked block below shows all ten
together:

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

A typo or an out-of-range value logs a warning and falls back to the built-in default, rather
than stopping a run at 3am when nobody is there to fix it.

## Platform differences

Two adapters exist today: systemd and cron. Both are Linux-only. macOS (`launchd`), Windows
(Task Scheduler), Kubernetes, OpenShift, AWS and Azure are not covered yet.

| | systemd (user timer) | cron |
| --- | --- | --- |
| Interval | Exact, any duration | Rounded. Cron's minute field only divides the hour, so `*/70` does not mean "every 70 minutes". The adapter picks the nearest interval it can express, rounding down above one minute and up below it, and tells you the difference |
| Missed a run while asleep or off | Replays it (`Persistent=true`) | Lost. cron has no equivalent |
| Runs while logged out | Only if linger is on: `loginctl enable-linger $USER`. `schedule status` reports when it is off | Yes, cron runs independent of any login session |
| Skips on battery | `ConditionACPower=true` in the unit itself | The `schedule_on_battery` gate at run time |
| `schedule_require_idle` | Same limitation as cron: see below | Inert. Neither cron nor a systemd timer sets `XDG_SESSION_ID`, which idleness detection needs, so the gate cannot tell and passes every time. `status` and the run itself both warn when this is on |

## Containers

A container's writable layer is thrown away on every restart. Running the scheduler there without
persistent storage would mean every cycle re-indexes the whole fleet from scratch, so `schedule
run` refuses:

```console
$ contextlake schedule run
Refusing to run: this container's state does not survive a restart, so every run would re-index the whole fleet from scratch.
  Mount a volume at the cache directory (a PVC on Kubernetes, EFS on AWS, Azure Files on Azure), or pass --allow-ephemeral if that is what you want.
```

The check is whether the cache directory sits on its own mount point, not what filesystem type it
reports: a container's own writable layer is part of the root mount, while any volume you attach
(a PVC, an `emptyDir`, a bind mount, EFS, Azure Files) shows up as its own mount, whatever its
underlying filesystem. Fix it by pointing `cache_dir` at a mounted volume, or pass
`--allow-ephemeral` if a from-scratch run every cycle is what you want. There is no daemon to
install inside a container; run the cycle in the foreground instead:

```bash
contextlake schedule run --foreground
```

`--foreground` loops here, sleeping between cycles, and does not re-read the config file
mid-loop; restart the container to pick up an edited `schedule_interval`.

## Troubleshooting: it is installed but nothing runs

Start with `contextlake schedule status`. It reads the job record, the platform unit, and the
measured history, and reports every way they disagree:

1. **Linger is off (systemd only).** A user timer does not fire while you are logged out unless
   linger is on. `status` reports this; fix it with `loginctl enable-linger $USER`.
2. **The interpreter has moved.** `status` resolves the path the installed unit runs and reports
   it missing if the venv was moved or deleted. Re-run `contextlake schedule install`
   from the current install to fix it.
3. **The unit was removed outside contextlake.** `status` reports "recorded but NOT installed"
   when the job record exists but the systemd timer or crontab block does not. Re-run
   `contextlake schedule install`.
4. **The job record was removed and the unit was not.** `schedule list` reports these as
   orphaned units, by name and platform. Such a unit keeps firing on schedule, and `uninstall`
   cannot reach it, because it resolves a job name through the record that is gone. Recreate the
   record with `contextlake schedule --job NAME install` and then `uninstall` it, or delete the
   unit on the platform. `list` also names any platform it could not enumerate, so an empty
   result is never mistaken for one that was checked.
5. **`schedule_require_idle` is on and you expected it to gate a run.** It cannot: see
   [Platform differences](#platform-differences).
6. **Consecutive failures are backing off the interval.** `status` reports the failure count. Fix
   the underlying command failure (run it by hand first) and the next success resets it to zero.
7. **Nothing has fired yet at all.** Check the platform directly: `systemctl --user list-timers`
   for systemd, `crontab -l` for cron. A gap between what `status` reports and what the platform
   shows is the bug to chase.

## See also

- [Bootstrap and keep it fresh](keeping-it-fresh.md), the command the default job runs
- [Configuration](configuration.md), the full settings reference
- [Command reference](cli-reference.md), every `schedule` flag
- [Reading the console output](console-output.md), exit codes and log formats
