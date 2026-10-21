"""Choosing a platform adapter, running it, and reporting what it installed.

Its own module because everything reaches for it: the report commands, the
install and reset actions, and the ad-hoc `interval` path all resolve an
adapter, and all of them would otherwise have to import it from ``cmds``,
which imports them back.

Tests patch ``_adapter_for`` more than any other name in this package. Patch it
HERE. Its callers read this module's global, so patching a re-export elsewhere
sets a name nothing calls.
"""
from __future__ import annotations

import os
import sys

from ..logging_setup import log
from . import jobs as jobstore
from . import recommend
from .platform.base import NO_CATCH_UP_PHRASE

# The knowledge config `find_ancestor_config` looks for. The same name as
# ``kb.config.LOCAL_CONFIG``, repeated here because this package is core tier and cannot
# import ``contextlake.kb`` at module level. tests/test_schedule_origin.py holds the two equal.
KB_LOCAL_CONFIG = ".contextlake.kb.toml"


def _adapter_for(args, job=None):
    from .platform import base

    name = getattr(args, "platform", None) or (job.platform if job else None) or base.detect()
    return base.get(name)


def exec_argv_for(name, config=None, kb_config=None) -> list:
    """The command line the unit runs.

    ``sys.executable`` rather than a bare ``contextlake``: a unit runs with a
    different PATH from a login shell, and a venv that moved would otherwise
    fail silently forever. `status` resolves this back and reports it missing.

    ``config`` and ``kb_config`` are absolute paths the user named, put in the command as
    ``--config`` and ``--kb-config``. Both go before ``schedule``: the `schedule` command
    takes ``--config`` in either place and ``--kb-config`` only before it. A path found by
    walking up from a directory is never passed here, see :func:`job_exec_argv`.
    """
    pinned = []
    if config:
        pinned += ["--config", str(config)]
    if kb_config:
        pinned += ["--kb-config", str(kb_config)]
    return [sys.executable, "-m", "contextlake", *pinned, "schedule", "run", "--job", name]


def job_exec_argv(job) -> list:
    """The command line for ``job``'s unit, from its record.

    Install, reset and the ad-hoc `interval` command all build the argv here, so a dry run,
    the install, the printed fallback and a later reset cannot differ. Only a path recorded as
    explicit becomes ``--config``. A discovered path is reached by the unit's working
    directory, so the file keeps the trust it has when found by walking up: a key that
    chooses a forge host or a cache location is dropped from it, as it is for a run in that
    directory. Passing the path as ``--config`` would keep those keys.
    """
    explicit = jobstore.EXPLICIT
    return exec_argv_for(
        job.name,
        config=job.config if job.config_source == explicit else None,
        kb_config=job.kb_config if job.kb_config_source == explicit else None)


def _absolute(path) -> str:
    from ..config import expand_path

    return os.path.abspath(expand_path(str(path)))


def _same_file(a, b) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def capture_origin(args) -> dict:
    """The config files a new job's unit must read, and the directory it must run in.

    A file the user named with ``--config`` or ``--kb-config`` is recorded as explicit, with
    its absolute path. A file found by walking up from the current directory (the
    ``.contextlake.ini`` that ``init --local`` writes, or a ``.contextlake.kb.toml``) is
    recorded as discovered, and the current directory is recorded with it. A discovered file
    that is also the global one, or the one named with ``--config``, needs neither.

    The directory is recorded whenever a discovered file is read, even when ``--config`` is
    also given: the run merges the two, and one without the other can look in a different
    cache directory.

    Raises ``ValueError``, with a message for the person, when a path cannot be written into
    a unit file or a crontab line, or when ``--kb-config`` names no file.
    """
    from .. import config as cfgmod

    explicit = getattr(args, "config", None)
    explicit = _absolute(explicit) if explicit else None
    explicit_kb = getattr(args, "kb_config", None)
    explicit_kb = _absolute(explicit_kb) if explicit_kb else None
    if explicit_kb and not os.path.isfile(explicit_kb):
        raise ValueError(f"--kb-config path not found: {explicit_kb}. Every scheduled run "
                         f"would fail on it.")

    def read_here(found, *named):
        """The discovered file a run from here would read, or None."""
        found = _absolute(found) if found else None
        return None if found is None or any(
            _same_file(found, n) for n in named if n) else found

    local = read_here(cfgmod.find_ancestor_config(cfgmod.LOCAL_CONFIG_FILE),
                      cfgmod.CONFIG_FILE, explicit)
    local_kb = read_here(cfgmod.find_ancestor_config(KB_LOCAL_CONFIG), explicit_kb)

    origin = {key: None for key in jobstore.ORIGIN_KEYS}
    if explicit:
        origin.update(config=explicit, config_source=jobstore.EXPLICIT)
    elif local:
        origin.update(config=local, config_source=jobstore.DISCOVERED)
    if explicit_kb:
        origin.update(kb_config=explicit_kb, kb_config_source=jobstore.EXPLICIT)
    elif local_kb:
        origin.update(kb_config=local_kb, kb_config_source=jobstore.DISCOVERED)
    if local or local_kb:
        origin["cwd"] = os.getcwd()
    for key in ("config", "kb_config", "cwd"):
        if origin[key] is not None and not jobstore.usable_path(origin[key]):
            raise ValueError(
                f"{origin[key]!r} has a control character in it, so it cannot be written "
                f"into a scheduler unit. Install from a directory whose path has none.")
    return origin


def loaded_configs(args) -> list:
    """The sync INI files, besides the global one, that a run from here reads: the one found by
    walking up from the current directory, then the one named with ``--config``. Absolute
    paths, once each. ``schedule status`` compares them with the config its job's unit reads.
    """
    from .. import config as cfgmod

    files = []
    for path in (cfgmod.find_ancestor_config(cfgmod.LOCAL_CONFIG_FILE),
                 getattr(args, "config", None)):
        path = _absolute(path) if path else None
        if (path and not _same_file(path, cfgmod.CONFIG_FILE)
                and not any(_same_file(path, seen) for seen in files)):
            files.append(path)
    return files


def executable_missing(exec_argv):
    """The interpreter path in ``exec_argv`` if it no longer exists, else None.

    A unit that references a deleted venv fails on every fire, forever, and
    nothing surfaces it: systemd logs a start failure the user never reads.
    Resolving the actual file is the check; the string being present is not.
    """
    if not exec_argv:
        return None
    candidate = str(exec_argv[0])
    return None if os.path.exists(candidate) else candidate


def origin_lines(job) -> list:
    """What the job's unit reads, one line each, for the install report. Empty for a job that
    keeps no config of its own."""
    lines = []
    for path, source, label, flag in (
            (job.config, job.config_source, "config", "--config"),
            (job.kb_config, job.kb_config_source, "knowledge config", "--kb-config")):
        if path and source == jobstore.EXPLICIT:
            lines.append(f"{label}: {path} (in the unit's command as {flag})")
    if job.cwd:
        found = [p for p, s in ((job.config, job.config_source),
                                (job.kb_config, job.kb_config_source))
                 if p and s == jobstore.DISCOVERED]
        lines.append(f"working directory: {job.cwd} (the unit starts here, so it finds "
                     + (", ".join(found) if found else "the config files found at install")
                     + ")")
    return lines


def _report_installed(adapter, job, interval_s, exec_argv, on_battery, why, written,
                      headline) -> None:
    """Log the outcome of a successful install.

    Calls `render` again, which is free (`render` is pure) and is the only
    way to learn a fact `install` itself does not return: `install` returns
    the paths it wrote, not the rendered facts, and cron's `render` can
    round the interval requested down to the nearest one it can express.
    Reporting the interval requested instead of the interval installed is
    the R28 defect; this is the one place it can happen now.

    `headline` takes the interval INSTALLED, already formatted, and returns
    the caller's opening line: the job name, the command, and the adapter
    differ per call site, but everything below the headline (the reason,
    the paths written, the rounding note, the adapter's own state() notes,
    and the no-catch-up warning when state() has not already said it) is
    identical. Shared by `cmd_install`, `cmd_interval` and `cmd_reset`,
    because three separate copies is what let two of them drift.
    """
    from .. import style

    rendered = adapter.render(job, interval_s, exec_argv, on_battery=on_battery)
    actual_interval_s = rendered.get("interval_s", interval_s)
    log(headline(recommend.format_duration(actual_interval_s)))
    log(f"  {why}")
    for path in written:
        log(f"  wrote {path}")
    if getattr(adapter, "reads_host_config", True):
        for line in origin_lines(job):
            log(f"  {line}")
    # cron's note is one string; the cluster and cloud adapters return a list.
    rendered_notes = rendered.get("notes") or []
    for rounding_note in ([rendered_notes] if isinstance(rendered_notes, str)
                          else rendered_notes):
        log(f"  {style.warn()} {rounding_note}")
    # The install above already succeeded and is not undone by anything
    # below. A `state()` failure here (the bus answered at detect time and
    # is gone by the time this reads it back) must degrade to a note, the
    # same way `cmd_status` treats the same call, not abort a completed
    # operation and leave the caller thinking the install failed.
    try:
        state_notes = adapter.state(job).get("notes", [])
    except Exception as e:  # noqa: BLE001 - a live read must not undo a completed install
        state_notes = []
        log(f"  {style.warn()} could not read the {adapter.id} state: {e}")
    for note in state_notes:
        log(f"  {style.warn()} {note}")
    # cron's own state() already carries this note when installed; printing
    # it again unconditionally duplicated the sentence on every cron install.
    if (not adapter.catches_up_after_sleep
            and not any(NO_CATCH_UP_PHRASE in n for n in state_notes)):
        log(f"  {style.warn()} {adapter.id} {NO_CATCH_UP_PHRASE} while this "
            f"machine was asleep or off.")
