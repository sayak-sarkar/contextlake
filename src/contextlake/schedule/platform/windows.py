"""Windows Task Scheduler, driven through ``schtasks``.

``schtasks /SC MINUTE /MO n`` repeats every n minutes. Two limits shape this
adapter and both are reported rather than papered over:

- ``/MO`` takes WHOLE MINUTES, so an interval is rounded the way cron's is.
- ``schtasks`` cannot set *StartWhenAvailable*, so a run missed while the
  machine was off is NOT replayed. cron has the same gap and shares the phrase
  ``base.NO_CATCH_UP_PHRASE`` for it, so `status` says it once rather than
  twice.

Not runnable on this machine. Everything here is verified by rendering the
command and by asserting the exact ``schtasks`` argv, never by executing it.
Do not describe this backend as verified by execution.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

from .base import NO_CATCH_UP_PHRASE, Adapter, check_name

#: Task Scheduler paths are a tree. A folder keeps every job this tool creates
#: together and makes them enumerable without matching on a name prefix.
TASK_FOLDER = r"\contextlake"

#: One minute is the finest resolution `/SC MINUTE /MO` offers.
MIN_MINUTES = 1


def task_name(job_name) -> str:
    return f"{TASK_FOLDER}\\{check_name(job_name)}"


def nearest_expressible(seconds):
    """``(seconds, minutes)`` for the nearest interval schtasks can run.

    Rounds DOWN above one minute and UP below it, matching
    ``cron.nearest_expressible`` deliberately: two backends that round
    differently would give the same request two different intervals, and the
    reason for rounding down is the same one. Running more often costs duty
    cycle, which the user bounded and can see; running less often costs
    freshness, which is what a scheduler is for.
    """
    minutes = int(float(seconds) // 60)
    if minutes < MIN_MINUTES:
        minutes = MIN_MINUTES
    return float(minutes * 60), minutes


def _schtasks(*argv):
    return subprocess.run(["schtasks", *argv], capture_output=True, text=True,
                          errors="replace", check=False)


class WindowsAdapter(Adapter):
    id = "windows"
    # schtasks has no StartWhenAvailable. A run missed while the machine was
    # off is lost, the same as cron.
    catches_up_after_sleep = False
    metadata_keys = frozenset({"task", "interval_s", "minutes", "notes", "name"})

    def usable(self) -> bool:
        return sys.platform == "win32" and bool(shutil.which("schtasks"))

    def render(self, job, interval_s, exec_argv, **_options) -> dict:
        name = check_name(job.name)
        actual_s, minutes = nearest_expressible(interval_s)
        # list2cmdline, NOT shlex.quote. /TR takes ONE command string parsed by
        # Windows rules, which are not POSIX rules, and a venv path containing
        # a space is the ordinary case rather than the edge one.
        command = subprocess.list2cmdline([str(a) for a in exec_argv])
        notes = []
        if abs(actual_s - float(interval_s)) > 1:
            from ..recommend import format_duration

            notes.append(
                f"Task Scheduler counts whole minutes, so this job runs every "
                f"{format_duration(actual_s)} instead of "
                f"{format_duration(interval_s)}.")
        notes.append(f"Task Scheduler {NO_CATCH_UP_PHRASE} while this machine was "
                     f"asleep or off.")
        return {
            "schtasks-command": subprocess.list2cmdline(
                self._create_argv(name, minutes, command)),
            "task": task_name(name),
            "interval_s": actual_s,
            "minutes": minutes,
            "notes": notes,
            "name": name,
        }

    def _create_argv(self, name, minutes, command) -> list:
        return ["schtasks", "/Create", "/TN", task_name(name),
                "/SC", "MINUTE", "/MO", str(minutes),
                "/TR", command,
                # Replace rather than fail when the task already exists: an
                # install that refuses on re-run cannot fix a wrong interval.
                "/F"]

    def install(self, job, interval_s, exec_argv, **options) -> list:
        rendered = self.render(job, interval_s, exec_argv, **options)
        command = subprocess.list2cmdline([str(a) for a in exec_argv])
        result = _schtasks(*self._create_argv(
            rendered["name"], rendered["minutes"], command)[1:])
        if result.returncode != 0:
            # Same rule as every other adapter: a failed create must not read
            # as a working schedule. cmd_install degrades on OSError by
            # printing the command to run by hand.
            raise OSError(
                f"schtasks /Create {rendered['task']} failed: "
                f"{result.stderr.strip() or result.stdout.strip() or 'no output'}")
        return [rendered["task"]]

    def uninstall(self, job) -> list:
        task = task_name(job.name)
        result = _schtasks("/Delete", "/TN", task, "/F")
        # A task that was already gone is not an error, the same as an
        # already-removed unit file or crontab block.
        return [task] if result.returncode == 0 else []

    def installed_names(self):
        """Every task under the contextlake folder.

        ``/FO CSV`` because the human-readable table is localised: on a
        non-English Windows its column headers differ, and parsing them would
        work on the developer's machine and fail on the user's.

        ``None`` when schtasks cannot be run at all, which is "cannot tell"
        rather than "nothing installed".
        """
        try:
            result = _schtasks("/Query", "/TN", TASK_FOLDER, "/FO", "CSV", "/NH")
        except OSError:
            return None
        if result.returncode != 0:
            # A missing folder means nothing is installed, which IS a
            # measurement. Any other failure is not distinguishable here, so
            # this stays the conservative reading: report none rather than
            # claim the platform could not be checked.
            return []
        names = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            first = line.split(",")[0].strip().strip('"')
            if first.startswith(TASK_FOLDER + "\\"):
                names.append(first[len(TASK_FOLDER) + 1:])
        return sorted(names)

    def state(self, job) -> dict:
        task = task_name(job.name)
        result = _schtasks("/Query", "/TN", task, "/FO", "LIST", "/V")
        installed = result.returncode == 0
        notes, interval_s, next_run, exec_path = [], None, None, None
        if installed:
            for line in result.stdout.splitlines():
                key, _, value = line.partition(":")
                key, value = key.strip().lower(), value.strip()
                if key == "next run time" and value:
                    next_run = value
                elif key == "task to run" and value:
                    exec_path = value.split()[0] if value.split() else None
            # The repeat interval is not reported in a form that survives
            # localisation, so it is left as None: "cannot tell" rather than a
            # parse that works on one Windows and not another.
            notes.append(f"Task Scheduler {NO_CATCH_UP_PHRASE} while this machine "
                         f"was asleep or off.")
        return {
            "installed": installed,
            "interval_s": interval_s,
            "next_run": next_run,
            "exec_path": exec_path,
            "notes": notes,
        }
