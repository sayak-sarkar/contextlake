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
import os

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


def _read_jobs_or_warn(path) -> tuple:
    """``(jobs, reason)``. ``reason`` is ``None`` when the store was read, and an absent
    store is read (no jobs). When it cannot be read, ``jobs`` is ``{}``, ``reason`` says why,
    and a warning names the file.

    Read quietly as empty, an unreadable store made `list` call every installed job an
    orphan and advise recreating its record, while `install` (correctly) refused to touch it.
    The reason lets a caller tell "no jobs" from "jobs it could not read".
    """
    from . import jobs as jobstore

    try:
        return jobstore.read_jobs(path, strict=True), None
    except jobstore.JobStoreUnreadable as e:
        log(f"WARNING: {e}. Its jobs cannot be listed until it is repaired or moved; "
            f"commands that would change it refuse until then.")
        return {}, str(e)


def cmd_list(args, config) -> int:
    """Every job this tool installed. Reads only."""
    from . import jobs as jobstore

    path = jobstore.jobs_path(config)
    mapping, unreadable = _read_jobs_or_warn(path)
    if unreadable is not None:
        # An orphan is a unit with no record. With the store unreadable, no record can be
        # matched to any unit, so every installed unit would be called an orphan and the
        # advice would be to recreate a record that may still exist. Say what was skipped
        # instead, and exit 1 because `list` could not do its job.
        if getattr(args, "json", False):
            from .platform import base

            # The same keys as the readable case, so a script reading them still gets
            # lists. No platform was asked, so every registered one is unchecked: an
            # empty `_unchecked_platforms` would tell a script written before this key
            # that all of them were checked and are clean. `registered()` only names the
            # adapters, it does not enumerate units. The underscore on
            # `_job_store_unreadable` keeps it from colliding with a job name.
            print(jsonlib.dumps({
                "_job_store_unreadable": {"path": path, "reason": unreadable},
                "_orphaned_units": [],
                "_unchecked_platforms": sorted(base.registered()),
            }, indent=2, sort_keys=True))
        else:
            print("Orphaned-unit check skipped: the job store could not be read, so "
                  "installed units cannot be matched to job records.")
        return 1
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


def _schedule_command(name, action, config=None, cwd=None, interval=None,
                      kb_config=None) -> str:
    """The `schedule` command a person can paste, with ``--config``, ``--kb-config`` and a
    directory to start in when the job has them. Both flags go before ``schedule``, which takes
    ``--kb-config`` only there. ``--job`` is added for a job that is not the default one.
    ``--interval`` is added, before the action, for an interval other than ``auto``: ``install``
    sets ``auto`` unless it is given one, so a hint without it would unpin a job."""
    import shlex

    from . import jobs as jobstore

    words = (["contextlake"] + (["--config", config] if config else [])
             + (["--kb-config", kb_config] if kb_config else []) + ["schedule"])
    if name != jobstore.DEFAULT_JOB:
        words += ["--job", name]
    if interval and str(interval).strip().lower() != "auto":
        words += ["--interval", str(interval)]
    command = shlex.join(words + [action])
    return f"cd {shlex.quote(cwd)} && {command}" if cwd else command


def _explicit(path, source):
    """``path`` when the user named it, else ``None``. Only a named path is printed as a flag: a
    discovered one is reached by the job's working directory, and ``--config`` would raise it to
    the trust of a file the user named. See ``jobs.EXPLICIT``."""
    from . import jobs as jobstore

    return path if path and source == jobstore.EXPLICIT else None


def _install_hint(job) -> str:
    """The command that installs ``job`` again as it was: its name, its interval, and the
    config files the user named for it. Without them the install it names would write the
    default job, set the interval to ``auto``, or drop the pins of the record."""
    return _schedule_command(job.name, "install", interval=job.interval,
                             config=_explicit(job.config, job.config_source),
                             kb_config=_explicit(job.kb_config, job.kb_config_source))


# The platforms where a missing directory was checked to stop the command. systemd: the
# systemd.exec(5) text was read, nothing was run. cron: the line `cd DIR && COMMAND` was run
# with sh. Task Scheduler: `cmd /d /c "cd /d DIR && COMMAND"` was run without the quotes
# around DIR, not through schtasks. launchd is not here: nothing was read or run for it.
_STOPS_WHEN_MISSING = ("systemd", "cron", "windows")


def _missing_directory_note(job, launch):
    """A warning when the directory the job's unit starts in is gone, else ``None``.

    Every unit that has a directory changes to it before it runs the command (systemd
    ``WorkingDirectory=``, launchd ``WorkingDirectory``, and the ``cd`` that cron and Task
    Scheduler write). systemd treats a missing one as fatal (systemd.exec(5)), and the ``&&``
    after the ``cd`` of cron and Task Scheduler skips the command. Those three are in
    ``_STOPS_WHEN_MISSING``; any other platform, launchd included, gets a sentence that says
    what a missing directory does there was not checked, because nothing here ran it.
    ``launch`` is what ``state()`` read from the installed unit. A unit that was read decides:
    one that starts in no directory is not a finding, whatever the record holds. When the unit
    could not be read (``None``), the directory in the record stands in, and the note says so.
    A found config is named only while its file exists: when it sat in the directory that is
    gone, no install directory finds it again.
    """
    from . import jobs as jobstore

    if launch is None:
        directory, subject = job.cwd, "The working directory recorded for this job"
        effect = "A unit that starts in it cannot run the job."
    else:
        directory, subject = launch.get("cwd"), "The working directory of this job's unit"
        effect = "The job cannot start there."
    if not directory or os.path.isdir(directory):
        return None
    if job.platform not in _STOPS_WHEN_MISSING:
        effect = (f"What {job.platform or 'this platform'} does with a missing directory "
                  "was not checked.")
    found = (job.config if job.config and job.config_source == jobstore.DISCOVERED
             and os.path.isfile(job.config) else None)
    again = _install_hint(job)
    sentences = [f"{subject}, {directory}, is not an existing directory.", effect]
    if found:
        sentences.append(f"The config it reads, {found}, was found from that directory.")
    sentences.append("Install the job again from a directory "
                     + ("that finds that file" if found else "that exists") + f": {again}")
    return " ".join(sentences)


def _config_notes(args, job, launch) -> list:
    """Warnings about the directory the job's unit starts in, and about the sync INI this
    command loaded against the one the job's unit reads.

    ``launch`` is what ``state()`` read back from the installed unit: ``None`` means it could
    not be read, which is not a finding about the config. A directory that is gone is the
    first and only note, whatever the platform does with it: the ``cd`` that the mismatch note
    suggests would fail in it. A unit with no ``--config`` and no
    working directory loads only the global config. Three jobs look like that: a unit written
    by an earlier version, a job installed from a directory with no config, and a job installed
    from the global config alone and checked from a directory that has a local one. A job
    record reads the same for all three, so the first config note below reads the unit, not the
    record, and says nothing about how the unit came to be.
    """
    from . import jobs as jobstore

    gone = _missing_directory_note(job, launch)
    if gone:
        return [gone]
    loaded = adapters.loaded_configs(args)
    if (loaded and launch is not None and launch.get("cwd") is None
            and "--config" not in launch.get("argv", [])):
        named = getattr(args, "config", None)
        # This run's --config wins. Without one, the file the record names is kept: the loaded
        # one may be a found file, and the install would then replace the pin with it.
        # The lead names the file the command keeps for the unit's --config.
        pinned = None if named else _explicit(job.config, job.config_source)
        again = _schedule_command(job.name, "install", interval=job.interval,
                                  config=adapters._absolute(named) if named else pinned,
                                  kb_config=_explicit(job.kb_config, job.kb_config_source))
        lead = ("To make the job use the config it names" if pinned
                else "To make the job use that config")
        return [f"The unit for this job starts with no --config and no working directory, so "
                f"it reads only the global config. This command loaded {', '.join(loaded)}. "
                f"{lead}, install it again from this directory: {again}"]
    if job.config and not any(adapters._same_file(job.config, path) for path in loaded):
        explicit = job.config_source == jobstore.EXPLICIT
        how = "named with --config" if explicit else f"found from {job.cwd}"
        same_view = _schedule_command(job.name, "status",
                                      config=job.config if explicit else None, cwd=job.cwd)
        return [f"The unit for this job reads {job.config} ({how}). This command loaded "
                f"{', '.join(loaded) or 'no config besides the global one'}. To see the job "
                f"as its unit does, run: {same_view}"]
    return []


def cmd_status(args, config) -> int:
    """What is installed, what it will do, and every way it might be dead.

    Reads only, writes nothing: three sources (the job record, the platform
    unit, and the measured history) are read and compared, and never
    reconciled here. `install` is the only action that changes any of them.
    """
    from .. import style
    from . import jobs as jobstore

    path = jobstore.jobs_path(config)
    mapping, unreadable = _read_jobs_or_warn(path)
    if unreadable is not None:
        # The same case as in `list`: "No schedule installed" plus an install command would
        # invite a reinstall over jobs that may still be installed and only unreadable here.
        if getattr(args, "json", False):
            print(jsonlib.dumps({"_job_store_unreadable": {"path": path, "reason": unreadable},
                                 "jobs": []}, indent=2, sort_keys=True))
        else:
            print("Job status not shown: the job store could not be read, so installed jobs "
                  "cannot be listed.")
        return 1
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

    origin_text = {}
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
        # A unit that starts a process here reads the config files named in the record. A
        # container job (k8s, aws, azure) reads its own, so a host path and a "rerun status
        # with it" hint would be false for it. Unknown, when no adapter could be built, is
        # treated as the host case: the record is shown and the notes are given.
        host_config = True
        try:
            adapter = adapters._adapter_for(args, job)
        except Exception as e:  # noqa: BLE001 - a broken adapter must not hide the record
            state = {"installed": None, "interval_s": None, "next_run": None,
                     "notes": [f"could not build the {job.platform} adapter: {e}"]}
        else:
            adapter_id, catches_up = adapter.id, adapter.catches_up_after_sleep
            host_config = getattr(adapter, "reads_host_config", True)
            try:
                state = adapter.state(job)
            except Exception as e:  # noqa: BLE001 - ditto, for a live read
                state = {"installed": None, "interval_s": None, "next_run": None,
                         "notes": [f"could not read the {job.platform} state: {e}"]}
        for note in (state.get("notes") or []):
            if note not in notes:
                notes.append(note)

        # ``is False``: ``None`` means the platform could not be asked, and its own note says
        # why. Telling the user to re-run install then is advice that cannot help, because
        # install needs the same access.
        installed = state.get("installed")
        if installed is False:
            notes.append("This job is recorded but its unit is NOT installed. "
                         f"Re-run `{_install_hint(job)}` to put it back.")
        origin_text[name] = adapters.origin_lines(job) if host_config else []
        if host_config:
            for note in _config_notes(args, job, state.get("launch")):
                if note not in notes:
                    notes.append(note)
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
                         f"`{_install_hint(job)}` from the current install.")
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
            "unit_installed": None if installed is None else bool(installed),
            "adapter": adapter_id,
            "next_run": state.get("next_run"), "command": job.argv,
            **jobstore.origin_of(job),
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
        for line in origin_text[entry["name"]]:
            print(f"    {line}")
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
