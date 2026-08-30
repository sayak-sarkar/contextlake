"""The read-only schedule commands.

Split out of ``cmds.py``. ``recommend``, ``list`` and ``status`` answer
questions and change nothing on disk or on the platform, which is the seam the
Plan 1 review named. Keeping them apart from the install and reset paths makes
"does this command mutate anything?" answerable from the import line.

Core tier. Nothing here may import ``contextlake.kb`` at module level, which
``tests/test_schedule_source_tier.py`` enforces.
"""
from __future__ import annotations

import json as jsonlib

from ..logging_setup import log
from . import adapters, gates, history, recommend
from .platform.base import NO_CATCH_UP_PHRASE
from .settings import _float_or, current_recommendation, resolve_interval


def cmd_recommend(args, config) -> int:
    """Print the interval and why, and change nothing."""
    rec, runs = current_recommendation(config)
    summary = history.summarize(runs)
    if getattr(args, "json", False):
        print(jsonlib.dumps({
            "interval": recommend.format_duration(rec.interval_s),
            "interval_seconds": rec.interval_s,
            "basis": rec.basis, "reason": rec.reason, "measured": rec.measured,
            "samples": rec.samples, "clamped": rec.clamped,
            "floor_duty_seconds": rec.floor_duty_s,
            "floor_activity_seconds": (None if rec.floor_activity_s in (None, float("inf"))
                                       else rec.floor_activity_s),
            # `floor_activity_seconds` is null for two different reasons, and a
            # consumer cannot tell them apart from the number alone: nothing ever
            # recorded activity, or activity was recorded and nothing changed
            # (an infinite floor). The text output has always distinguished them;
            # JSON collapsed both to null.
            "activity": ("not-measured" if rec.floor_activity_s is None
                         else "no-change" if rec.floor_activity_s == float("inf")
                         else "measured"),
            "history": summary,
        }, indent=2, sort_keys=True))
        return 0
    from .. import style

    print(f"{style.ok() if rec.measured else style.warn()} "
          f"Recommended interval: {recommend.format_duration(rec.interval_s)}")
    print(f"  Because: {rec.reason}")
    if rec.measured:
        print(f"  From {rec.samples} measured run(s) over {summary['days']:.1f} day(s)")
        if rec.floor_duty_s is not None:
            print(f"    duty-cycle floor: {recommend.format_duration(rec.floor_duty_s)}")
        if rec.floor_activity_s is not None:
            print("    activity floor:   "
                  + ("no change measured"
                     if rec.floor_activity_s == float("inf")
                     else recommend.format_duration(rec.floor_activity_s)))
        else:
            # Printing nothing here left the reader unable to tell an unmeasured
            # bound from a bound that is switched off. The absence is a real
            # answer: no run recorded how many repositories changed, so only the
            # duty bound sets the interval. `kb index` is what records it, and a
            # core-only install never runs it, so this is the normal state there
            # rather than a fault.
            print("    activity floor:   not measured. The interval uses the "
                  "duty-cycle floor only.")
            print("                      Repository activity is recorded by the "
                  "index stage, which needs")
            print("                      the kb extra (pip install "
                  "'contextlake[kb]').")
    else:
        print("  Nothing has been measured yet. Run `contextlake mirror sync` or "
              "`contextlake bootstrap` once, or install the schedule and let the "
              "first run replace this default.")
    print("\n  Install it:  contextlake schedule install")
    return 0


def orphaned_units(known_names):
    """``([(platform, job_name), ...], [unchecked_platform, ...])``.

    ``state()`` can only answer "is job X installed?", so it can only be asked
    about jobs that still HAVE a record. The reverse has no reader: delete a
    record and its unit keeps firing on schedule, invisible to `list` and
    unreachable by `uninstall`, which resolves a name through the record.

    Adapters that return ``None`` cannot enumerate and are skipped, rather
    than being read as "no orphans here".

    Every REGISTERED adapter is asked, not only the usable ones. A unit
    installed under systemd stays a unit after the machine stops offering
    systemd, and that is when it is most likely to be forgotten, so filtering
    on ``usable()`` would skip exactly the case worth reporting. Enumeration is
    a read-only scan and does not need the platform to be working; an adapter
    whose tool is missing raises, and lands in ``unchecked``.
    """
    from .platform import base

    found, unchecked = [], []
    for name in base.registered():
        try:
            installed = base.get(name).installed_names()
        except Exception as e:  # noqa: BLE001 - a broken probe must not break `list`
            # Said out loud rather than swallowed: a probe that raises means
            # orphans on this platform were NOT checked, and silence there
            # reads as "none found".
            log(f"  Could not check {name} for orphaned units: {e}")
            unchecked.append(name)
            continue
        if installed is None:
            # Returned separately, not folded into an empty result. Skipping a
            # platform and finding nothing on it produce the same empty list,
            # so a caller given only the list cannot tell "checked, clean" from
            # "never looked" -- which is the exact defect this feature exists
            # to remove, one level up.
            unchecked.append(name)
            continue
        found.extend((name, unit) for unit in installed if unit not in known_names)
    return sorted(found), sorted(unchecked)


def cmd_list(args, config) -> int:
    """Every job this tool installed. Reads only."""
    from . import jobs as jobstore

    path = jobstore.jobs_path(config)
    mapping = jobstore.read_jobs(path)
    orphans, unchecked = orphaned_units(set(mapping))
    if getattr(args, "json", False):
        # Jobs stay at the TOP LEVEL, where 8.8.0 put them. Nesting them under a
        # "jobs" key to make room for the new fields would break every script
        # that reads this, and the versioning promise in README's "Versioning and
        # compatibility" says a break needs a major bump. This is additive
        # instead, so it ships in a minor.
        #
        # The new keys lead with an underscore because `check_name` requires a
        # job name to START with an alphanumeric: `_orphaned_units` can never be
        # a job, so it cannot collide with one. `orphaned_units` without the
        # underscore IS a legal job name, which is what made a flat namespace
        # look unusable in the first place.
        payload = {name: job._asdict() for name, job in sorted(mapping.items())}
        payload["_orphaned_units"] = [{"platform": p, "name": n} for p, n in orphans]
        # Separate from the orphan list, so a consumer can tell an empty result
        # that was measured from one that nothing could measure.
        payload["_unchecked_platforms"] = unchecked
        print(jsonlib.dumps(payload, indent=2, sort_keys=True))
        return 0
    if not mapping and not orphans and not unchecked:
        print("No scheduled jobs. Create one with `contextlake schedule install`.")
        return 0
    # Guarded: the early return above now also requires no orphans, so this
    # is reachable with an empty mapping, and `max()` over one raises.
    if mapping:
        width = max(3, max(len(name) for name in mapping))
        print(f"{'JOB'.ljust(width)}  INTERVAL  ADAPTER   LAST RUN              COMMAND")
        for name, job in sorted(mapping.items()):
            last = job.last_run or "never"
            mark = "" if job.last_exit in (0, None) else f" (exit {job.last_exit})"
            print(f"{name.ljust(width)}  {job.interval:<8}  {job.platform:<8}  "
                  f"{last:<20}  contextlake {' '.join(job.argv)}{mark}")
    else:
        print("No scheduled jobs.")
    _print_orphans(orphans, unchecked)
    return 0


def _print_orphans(orphans, unchecked) -> None:
    """Name each unit and say how to remove it. A count alone would tell the
    reader something is wrong without telling them which thing."""
    if unchecked:
        print(f"\n  Not checked for orphaned units: {', '.join(unchecked)}. "
              "This platform cannot list what")
        print("  it has installed, so a unit there with no job record would "
              "not be reported here.")
    if not orphans:
        return
    print("\n  Installed units with no job record. These still run on schedule "
          "and `uninstall` cannot")
    print("  reach them, because it resolves a job name through the record "
          "that is gone:")
    for platform_name, unit in orphans:
        print(f"    {platform_name}: {unit}")
    print("  Remove one by recreating its record and uninstalling "
          "(`contextlake schedule --job NAME install`,")
    print("  then `contextlake schedule --job NAME uninstall`), or delete "
          "the unit on the platform.")


def cmd_status(args, config) -> int:
    """What is installed, what it will do, and every way it might be dead.

    Reads only, writes nothing: three sources (the job record, the platform
    unit, and the measured history) are read and compared, and never
    reconciled here. `install` is the only action that changes any of them.
    """
    from .. import style
    from . import jobs as jobstore

    mapping = jobstore.read_jobs(jobstore.jobs_path(config))
    only = getattr(args, "job", None)
    all_names = sorted(mapping)
    if only:
        mapping = {k: v for k, v in mapping.items() if k == only}

    rec, runs = current_recommendation(config)
    summary = history.summarize(runs)
    threshold = _float_or(config, "schedule_adjust_threshold", 0.5, low=0.0)

    payload = {"history": summary,
               "recommendation": {
                   "interval": recommend.format_duration(rec.interval_s),
                   "interval_seconds": rec.interval_s, "basis": rec.basis,
                   "measured": rec.measured, "reason": rec.reason},
               "jobs": []}

    if not mapping and not getattr(args, "json", False):
        if only and all_names:
            # Jobs exist, just not this one: saying "No schedule installed"
            # here would invite a reinstall over a schedule that is working.
            print(f"No job named {only!r}. Installed jobs: {', '.join(all_names)}.")
            return 0
        print("No schedule installed.")
        print(f"  Recommended interval: {recommend.format_duration(rec.interval_s)} "
              f"({'measured' if rec.measured else 'a default, not a measurement'})")
        print("  Install it:  contextlake schedule install")
        return 0

    require_idle = str(config.get("schedule_require_idle", "false")).strip().lower() \
        in ("true", "yes", "1")
    idle_inert = False
    if require_idle:
        try:
            idle_inert = gates.user_is_idle() is None
        except Exception:  # noqa: BLE001 - a sensor is never worth failing status over
            idle_inert = True

    for name, job in sorted(mapping.items()):
        notes = []
        # Constructing the adapter and reading its state are two separate
        # failure points. A construction failure means the catch-up property
        # is not knowable (``catches_up = None``, no claim printed either
        # way). A `state()` failure still leaves a constructed adapter, whose
        # `catches_up_after_sleep` is a class attribute, not a read of live
        # state, so it is known even when `state()` itself failed. Collapsing
        # both into one `except` and defaulting to ``False`` printed a false
        # "does not replay a run missed" for systemd, which sets
        # ``Persistent=true`` and does replay one.
        adapter_id, catches_up = job.platform or "unknown", None
        try:
            adapter = adapters._adapter_for(args, job)
        except Exception as e:  # noqa: BLE001 - a broken adapter must not hide the record
            state = {"installed": False, "interval_s": None, "next_run": None,
                     "notes": [f"could not build the {job.platform} adapter: {e}"]}
        else:
            adapter_id, catches_up = adapter.id, adapter.catches_up_after_sleep
            try:
                state = adapter.state(job)
            except Exception as e:  # noqa: BLE001 - ditto, for a live read
                state = {"installed": False, "interval_s": None, "next_run": None,
                         "notes": [f"could not read the {job.platform} state: {e}"]}
        for note in (state.get("notes") or []):
            if note not in notes:
                notes.append(note)

        if not state.get("installed"):
            notes.append("This job is recorded but its unit is NOT installed. "
                         "Re-run `contextlake schedule install` to put it back.")
        # The interpreter the INSTALLED unit references, not the one running
        # this check: `exec_argv_for` builds the argv for a fresh install and
        # always resolves to sys.executable, so checking it here would ask
        # whether the interpreter running `status` right now exists, which is
        # never missing. `exec_path` comes from the adapter reading the unit
        # back (systemd's ExecStart, cron's crontab line), and `None` means
        # the adapter could not tell, which must not read as "missing".
        exec_path = state.get("exec_path")
        gone = adapters.executable_missing([exec_path]) if exec_path else None
        if gone:
            notes.append(f"The interpreter this job runs ({gone}) has moved or been "
                         f"deleted, so every run fails silently. Re-run "
                         f"`contextlake schedule install` from the current install.")
        # The adapter's own state() may already say this (cron's does, on every
        # installed job); matching on the phrase rather than the whole
        # sentence keeps the line from printing twice for the one backend
        # that reports both, regardless of which noun starts its sentence.
        # ``catches_up is False`` (not merely falsy): ``None`` means unknown,
        # which is a different fact than "known not to catch up".
        if catches_up is False and not any(NO_CATCH_UP_PHRASE in n for n in notes):
            notes.append(f"{adapter_id} {NO_CATCH_UP_PHRASE} while this machine "
                         f"was asleep or off.")
        if idle_inert:
            notes.append("schedule_require_idle is on, but user idleness cannot "
                         "be detected here (no login session), so the gate is "
                         "inert: this job runs whether or not you are at the "
                         "keyboard.")

        effective_s, why = resolve_interval(config, job.interval)
        live_s = state.get("interval_s")
        if (job.interval.lower() == "auto" and live_s and rec.measured
                and abs(live_s - rec.interval_s) / max(live_s, 1.0) > threshold):
            notes.append(
                f"Drift: this job runs every {recommend.format_duration(live_s)}, "
                f"but the measurements now suggest "
                f"{recommend.format_duration(rec.interval_s)}. The next run "
                f"rewrites the unit.")

        payload["jobs"].append({
            "name": name, "interval_setting": job.interval,
            "effective_interval": recommend.format_duration(effective_s), "why": why,
            "unit_interval": (recommend.format_duration(live_s) if live_s else None),
            "unit_installed": bool(state.get("installed")), "adapter": adapter_id,
            "next_run": state.get("next_run"), "command": job.argv,
            "recommendation": recommend.format_duration(rec.interval_s),
            "last_run": job.last_run, "last_exit": job.last_exit,
            "failures": job.failures, "notes": notes})

    if getattr(args, "json", False):
        print(jsonlib.dumps(payload, indent=2, sort_keys=True))
        return 0

    for entry in payload["jobs"]:
        glyph = style.ok() if (entry["unit_installed"] and not entry["notes"]) else style.warn()
        print(f"{glyph} {entry['name']}  ({entry['adapter']})")
        print(f"    runs:      contextlake {' '.join(entry['command'])}")
        print(f"    interval:  {entry['effective_interval']}"
              + (f"  (set to {entry['interval_setting']})"
                 if entry["interval_setting"] != "auto" else "  (auto)"))
        if entry["interval_setting"] != "auto":
            # `why` is `rec.reason` for an auto job, already printed once
            # below for the whole command; repeating it per job would be
            # noise. A pinned job's reason ("auto-adjust is off") is not
            # printed anywhere else, so it belongs here.
            print(f"               {entry['why']}")
        if entry["unit_interval"] and entry["unit_interval"] != entry["effective_interval"]:
            print(f"    on disk:   {entry['unit_interval']}")
        print(f"    next run:  {entry['next_run'] or 'unknown'}")
        print(f"    last run:  {entry['last_run'] or 'never'}"
              + (f"  (exit {entry['last_exit']})"
                 if entry["last_exit"] not in (0, None) else ""))
        if entry["failures"]:
            print(f"    {style.warn()} {entry['failures']} consecutive failure(s); "
                  f"the interval is backing off.")
        for note in entry["notes"]:
            print(f"    {style.warn()} {note}")

    # Built as a plain string first. A nested same-quote f-string is PEP 701,
    # which is Python 3.12; on the 3.10 and 3.11 matrix cells it is a
    # SyntaxError that takes the whole CLI down at import.
    if rec.measured:
        provenance = (f"from {rec.samples} measured run(s) over "
                      f"{summary['days']:.1f} day(s)")
    else:
        provenance = "a built-in default, not a measurement"
    print(f"\n  Recommended interval: "
          f"{recommend.format_duration(rec.interval_s)} ({provenance})")
    print(f"    {rec.reason}")
    return 0
