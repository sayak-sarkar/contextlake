"""A systemd **user** timer plus its service.

User scope, not system scope: installing a system unit needs root, and the whole
point is that a developer can schedule their own mirror without one. The cost is
``Linger``: a user timer does not fire while that user is logged out unless
``loginctl enable-linger`` has been run. That is DETECTED and REPORTED by
``state()``, never assumed, because a schedule the user believes is running and
is not is worse than no schedule.
"""
from __future__ import annotations

import os
import re
import subprocess

from .base import Adapter, check_name, systemd_is_init

HEADER = ("# Managed by contextlake. Edits are overwritten by "
          "`contextlake schedule install`.\n")


def unit_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "systemd", "user")


_EXEC_PATH_RE = re.compile(r"path=(\S+)")


def _exec_path_from_show(value) -> str | None:
    """The interpreter path out of one ``ExecStart=`` show value.

    Modern systemd reports a structured form:
    ``{ path=/x/python ; argv[]=/x/python -m ... ; ... }``. Older systemd (or
    any show output that is already a bare command line) reports the
    command directly. Returns ``None`` on anything that does not parse,
    never raises: an unusual show value means "cannot tell", the same rule
    every sensor in this package follows, not "the interpreter is missing".
    """
    value = (value or "").strip()
    if not value:
        return None
    match = _EXEC_PATH_RE.search(value)
    if match:
        return match.group(1)
    if value.startswith("{"):
        # A structured value with no ``path=`` inside it: unparseable, not a
        # bare command that happens to start with a brace.
        return None
    parts = value.split()
    return parts[0] if parts else None


def unit_name(job_name) -> str:
    """``default`` to ``contextlake-default``. ALWAYS prefixed, no conditional.

    An earlier draft prefixed only names that did not already start with
    ``contextlake-``. That gave two naming rules, both individually testable and
    both passing, which is precisely how a real collision hides. One rule, one
    function, called by render, install, uninstall and state.
    """
    return f"contextlake-{check_name(job_name)}"


def _systemctl(*argv, check=False):
    return subprocess.run(["systemctl", "--user", *argv],
                          capture_output=True, text=True, errors="replace", check=check)


def _linger_status() -> str:
    """Raw ``loginctl show-user ... -p Linger`` stdout, e.g. ``Linger=no\\n``.

    Its own function so a test can control the answer without depending on
    the machine it runs on: linger is a login-manager setting, not something
    a test should read off whatever box happens to run it.
    """
    result = subprocess.run(
        ["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
        capture_output=True, text=True, errors="replace", check=False)
    return result.stdout


class SystemdAdapter(Adapter):
    id = "systemd"
    # Persistent=true replays a run missed while the machine was asleep or off.
    catches_up_after_sleep = True

    def usable(self) -> bool:
        return systemd_is_init()

    def render(self, job, interval_s, exec_argv, on_battery="skip", **_options) -> dict:
        name = check_name(job.name)
        seconds = max(1, int(round(float(interval_s))))
        exec_line = " ".join(exec_argv)
        condition = ("ConditionACPower=true\n"
                     if str(on_battery).lower() != "run" else "")
        service = (
            HEADER
            + "[Unit]\n"
            + f"Description=contextlake scheduled run ({name})\n"
            + condition
            + "\n[Service]\n"
            + "Type=oneshot\n"
            + f"ExecStart={exec_line}\n"
            # The job takes the run lock itself, so a hung run is bounded by
            # this rather than by the next timer tick.
            + "TimeoutStartSec=infinity\n"
            + "Nice=10\n"
            + "IOSchedulingClass=idle\n")
        timer = (
            HEADER
            + "[Unit]\n"
            + f"Description=contextlake scheduled run ({name})\n"
            + "\n[Timer]\n"
            # Not OnCalendar: the interval is relative, so a DST change or a
            # clock correction cannot double-fire or skip it.
            + "OnBootSec=2m\n"
            + f"OnUnitInactiveSec={seconds}s\n"
            + "Persistent=true\n"
            + "AccuracySec=1m\n"
            + "\n[Install]\n"
            + "WantedBy=timers.target\n")
        unit = unit_name(name)
        return {f"{unit}.service": service, f"{unit}.timer": timer}

    def timer_unit(self, job) -> str:
        return unit_name(job.name) + ".timer"

    def install(self, job, interval_s, exec_argv, **options) -> list:
        directory = unit_dir()
        os.makedirs(directory, exist_ok=True)
        written = []
        for filename, text in self.render(job, interval_s, exec_argv, **options).items():
            path = os.path.join(directory, filename)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            written.append(path)
        _systemctl("daemon-reload")
        enabled = _systemctl("enable", "--now", self.timer_unit(job))
        if enabled.returncode != 0:
            # `usable()` proves a user bus answered at DETECT time; it does not
            # prove it still answers now. Files on disk with no enabled timer
            # is the silent-failure state this adapter exists to rule out, so
            # a non-zero exit here is raised rather than swallowed. The caller
            # (`cmd_install`) already degrades on `OSError`: prints the unit,
            # says to install it by hand. The files stay on disk; they match
            # what was printed, and enabling them later is a valid recovery.
            raise OSError(
                f"systemctl --user enable --now {self.timer_unit(job)} failed: "
                f"{enabled.stderr.strip() or enabled.stdout.strip() or 'no output'}")
        return written

    def uninstall(self, job) -> list:
        removed = []
        unit = unit_name(job.name)
        names = [unit + ".service", unit + ".timer"]
        _systemctl("disable", "--now", self.timer_unit(job))
        for filename in names:
            path = os.path.join(unit_dir(), filename)
            try:
                os.unlink(path)
                removed.append(path)
            except OSError:
                pass  # Already gone. Not an error.
        if removed:
            _systemctl("daemon-reload")
            _systemctl("reset-failed")
        return removed

    def installed_names(self):
        """Read from the unit directory, not from systemctl.

        A timer file is what survives a deleted job record, and it is there
        whether or not the unit is currently loaded or the user has a session.
        Returns ``None`` when the directory cannot be read at all, which is
        "cannot tell" rather than "nothing installed"; a directory that does
        not exist yet IS a measurement, and its answer is none.
        """
        directory = unit_dir()
        if not os.path.isdir(directory):
            return []
        try:
            entries = os.listdir(directory)
        except OSError:
            return None
        prefix, suffix = "contextlake-", ".timer"
        return sorted(e[len(prefix):-len(suffix)] for e in entries
                      if e.startswith(prefix) and e.endswith(suffix))

    def state(self, job) -> dict:
        timer = self.timer_unit(job)
        service = unit_name(job.name) + ".service"
        installed = os.path.exists(os.path.join(unit_dir(), timer))
        notes, interval_s, next_run, exec_path = [], None, None, None
        if installed:
            show = _systemctl("show", timer, "-p", "NextElapseUSecRealtime")
            value = show.stdout.strip().split("=", 1)[-1].strip()
            if value and value != "n/a":
                next_run = value
            try:
                with open(os.path.join(unit_dir(), timer), encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("OnUnitInactiveSec="):
                            interval_s = float(line.split("=", 1)[1].strip().rstrip("s"))
            except (OSError, ValueError):
                pass
            # The interpreter the INSTALLED unit runs, not the one running
            # this check: a venv deleted and reinstalled elsewhere leaves the
            # unit pointing at a path this process has no other way to see.
            exec_show = _systemctl("show", service, "-p", "ExecStart")
            exec_value = exec_show.stdout.strip().split("=", 1)[-1]
            exec_path = _exec_path_from_show(exec_value)
        if "Linger=yes" not in _linger_status():
            notes.append(
                "Linger is off, so this timer does NOT fire while you are logged "
                "out. Turn it on with: loginctl enable-linger $USER")
        return {"installed": installed, "interval_s": interval_s,
                "next_run": next_run, "exec_path": exec_path, "notes": notes}
