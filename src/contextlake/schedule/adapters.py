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
from . import recommend
from .platform.base import NO_CATCH_UP_PHRASE


def _adapter_for(args, job=None):
    from .platform import base

    name = getattr(args, "platform", None) or (job.platform if job else None) or base.detect()
    return base.get(name)


def exec_argv_for(name) -> list:
    """The command line the unit runs.

    ``sys.executable`` rather than a bare ``contextlake``: a unit runs with a
    different PATH from a login shell, and a venv that moved would otherwise
    fail silently forever. `status` resolves this back and reports it missing.
    """
    return [sys.executable, "-m", "contextlake", "schedule", "run", "--job", name]


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
    rounding_note = rendered.get("notes")
    if rounding_note:
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
