"""Windows Task Scheduler, driven through ``schtasks``.

``schtasks /SC MINUTE /MO n`` repeats every n minutes. Two limits shape this
adapter and both are reported rather than papered over:

- ``/MO`` takes WHOLE MINUTES, so an interval is rounded the way cron's is.
- ``schtasks`` cannot set *StartWhenAvailable*, so a run missed while the
  machine was off is NOT replayed. cron has the same gap and shares the phrase
  ``base.NO_CATCH_UP_PHRASE`` for it, so `status` says it once rather than
  twice.

``schtasks /Create`` has no option for the directory a task starts in. A job that needs
one runs ``cmd /d /s /c "cd /d "DIR" && "WORD" "WORD" ..."``, see ``_task_command``.

Not runnable on this machine. Everything here is verified by rendering the
command and by asserting the exact ``schtasks`` argv, never by executing it.
Do not describe this backend as verified by execution.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys

from .base import NO_CATCH_UP_PHRASE, Adapter, check_name, usable_text

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


# What `_task_command` writes around the command of a job that has a working directory.
_IN_DIRECTORY = re.compile(r'cmd /d /s /c "cd /d "(?P<cwd>[^"]+)" && (?P<command>.+)"', re.S)


def _quote_word(word) -> str:
    """``word`` inside double quotes, as the C runtime reads them. A run of backslashes at the
    end is doubled, so that it cannot escape the closing quote. ``word`` holds no quote, see
    ``_task_command``."""
    tail = "\\" * (len(word) - len(word.rstrip("\\")))
    return '"' + word + tail + '"'


def _task_command(job, exec_argv) -> str:
    """The ``/TR`` value: ``exec_argv`` as one command line, run from ``job.cwd`` if it has one.

    ``schtasks /Create`` has no option for the directory a task starts in. A job that must
    find a config by walking up from the directory it was installed in therefore runs
    ``cmd``, which changes to that directory and then starts the command. ``/s`` makes
    ``cmd`` strip the first and last quote of the text after ``/c`` and keep the rest, which
    is what lets the command hold quotes of its own. ``/d`` skips the AutoRun commands in
    the registry. A job with no working directory is not run through a shell.

    ``cmd`` reads the text before the program does. ``list2cmdline`` quotes a word only when
    it holds white space, so a path with ``&``, ``^``, ``|``, ``<`` or ``>`` would reach
    ``cmd`` as shell syntax. In the wrapped form every word is therefore in double quotes,
    which ``cmd`` reads as text. Quotes cannot make three characters safe, so they are refused:
    ``%``, which ``cmd`` expands even inside quotes, ``!``, which ``cmd`` expands even inside
    quotes when delayed expansion is on (a registry setting, off by default, that only
    ``/v:off`` overrides, and this command does not pass it), and ``"``, which ends the quoted
    text for ``cmd`` whatever backslashes come before it.
    """
    if not job.cwd:
        return subprocess.list2cmdline([str(a) for a in exec_argv])
    words = [str(a) for a in exec_argv]
    for what, text in [("working directory", job.cwd), *(("command", w) for w in words)]:
        if not usable_text(text) or any(c in text for c in '"%!'):
            raise ValueError(
                f"the {what} {text!r} has a quote, a percent sign, an exclamation mark or a "
                f"control character in it, so it cannot be written into a Task Scheduler "
                f"command. Install from a directory and a config path without them.")
    command = " ".join(_quote_word(w) for w in words)
    return f'cmd /d /s /c "cd /d "{job.cwd}" && {command}"'


def _split_cmdline(text) -> list:
    """The words of a command line by the Windows C runtime rules, which ``list2cmdline``
    writes for: a double quote groups, a backslash is literal unless a run of them ends in a
    quote, where each pair is one backslash and an odd one escapes the quote."""
    words, i, n = [], 0, len(text)
    while i < n:
        if text[i] in " \t":
            i += 1
            continue
        word, quoted = [], False
        while i < n and (quoted or text[i] not in " \t"):
            c = text[i]
            if c == "\\":
                j = i
                while j < n and text[j] == "\\":
                    j += 1
                run = j - i
                if j < n and text[j] == '"':
                    word.append("\\" * (run // 2))
                    if run % 2:
                        word.append('"')
                        j += 1
                else:
                    word.append("\\" * run)
                i = j
            elif c == '"':
                quoted = not quoted
                i += 1
            else:
                word.append(c)
                i += 1
        words.append("".join(word))
    return words


def _launch(command) -> dict | None:
    """``{"argv": [...], "cwd": str | None}`` out of a ``Task To Run`` value, or ``None``
    when it holds no word. Undoes :func:`_task_command`."""
    wrapped = _IN_DIRECTORY.fullmatch(command.strip())
    words = _split_cmdline(wrapped.group("command") if wrapped else command)
    return {"argv": words, "cwd": wrapped.group("cwd") if wrapped else None} if words else None


def _program(command):
    """The program in a ``Task To Run`` value, or ``None`` for an empty one.

    ``render`` writes the command with ``list2cmdline``, which wraps a path that holds a space
    in double quotes. Cutting at the first space then returned the opening quote and the text
    before the space, and ``status`` reported the interpreter as moved. A command that
    `_task_command` ran from a working directory is read for the program inside it, not
    ``cmd``.
    """
    wrapped = _IN_DIRECTORY.fullmatch(command.strip())
    match = re.match(r'"([^"]*)"|(\S+)', wrapped.group("command") if wrapped else command)
    return (match.group(1) or match.group(2)) if match else None


def _task_names(csv_output) -> list:
    """The names of the tasks under the contextlake folder in ``/Query /FO CSV /NH`` output.

    The first column is the full task path. ``/NH`` drops the header row, which is the part
    that is localised.
    """
    names = []
    for line in csv_output.splitlines():
        line = line.strip()
        if not line:
            continue
        first = line.split(",")[0].strip().strip('"')
        if first.startswith(TASK_FOLDER + "\\"):
            names.append(first[len(TASK_FOLDER) + 1:])
    return names


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
        command = _task_command(job, exec_argv)
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
        command = _task_command(job, exec_argv)
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
        if result.returncode == 0:
            return [task]
        # A task that was already gone is not an error, the same as an already-removed
        # unit file or crontab block. A denied delete is: the task still fires, and
        # returning [] made `schedule uninstall` drop the job record. The failure text is
        # localised, so it cannot be matched for "not found". List every task and read
        # the answer: gone only when the listing worked and the task is not in it. The
        # listing names no folder, so a missing \contextlake folder is an empty answer
        # and not a failure. A listing that fails cannot say, so the task counts as there.
        listing = _schtasks("/Query", "/FO", "CSV", "/NH")
        if listing.returncode == 0 and not any(
                n.casefold() == job.name.casefold() for n in _task_names(listing.stdout)):
            return []
        raise OSError(
            f"schtasks /Delete {task} failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'no output'}")

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
        return sorted(_task_names(result.stdout))

    def state(self, job) -> dict:
        task = task_name(job.name)
        result = _schtasks("/Query", "/TN", task, "/FO", "LIST", "/V")
        installed = result.returncode == 0
        notes, interval_s, next_run, exec_path, launch = [], None, None, None, None
        if installed:
            for line in result.stdout.splitlines():
                key, _, value = line.partition(":")
                key, value = key.strip().lower(), value.strip()
                if key == "next run time" and value:
                    next_run = value
                elif key == "task to run" and value:
                    exec_path = _program(value)
                    launch = _launch(value)
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
            "launch": launch,
        }
