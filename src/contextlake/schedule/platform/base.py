"""How a schedule reaches the operating system.

Every adapter is the same five methods, and ``render`` is pure so a backend can
be golden-file tested without being installable. That matters because five of
the seven planned adapters cannot run on any one machine.

Adding an adapter means adding a file and one line to ``_REGISTRY``. It never
means editing another adapter.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

# A job name becomes a filename, a systemd unit name, and a crontab marker. Keep
# it to characters that cannot escape a directory, split a unit name, or need
# quoting in any of the three.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# One phrase, shared by every adapter that cannot replay a run missed
# while asleep or off, and by the status command that reports the same
# fact when an adapter's own state() has not already said it. Matching
# on the phrase rather than a whole sentence is what keeps the two from
# printing it twice when both sides know it.
NO_CATCH_UP_PHRASE = "does not replay a run missed"


class NoAdapter(RuntimeError):
    """No adapter by that name, or none usable here."""


def check_name(name) -> str:
    if not NAME_RE.match(str(name or "")):
        raise ValueError(
            f"invalid job name {name!r}: use letters, digits, dot, dash and "
            f"underscore, 1 to 64 characters, starting with a letter or digit")
    return str(name)


class Adapter:
    """The interface. See the plan for the contract of each method."""

    id = ""
    catches_up_after_sleep = False

    def usable(self) -> bool:
        raise NotImplementedError

    def render(self, job, interval_s, exec_argv, **options) -> dict:
        raise NotImplementedError

    def install(self, job, interval_s, exec_argv, **options) -> list:
        raise NotImplementedError

    def uninstall(self, job) -> list:
        raise NotImplementedError

    def state(self, job) -> dict:
        """What is really installed for ``job``, read from this platform, not
        from the job record.

        Returns a dict with five keys: ``installed`` (bool), ``interval_s``
        (the interval the unit itself holds, or ``None`` if unreadable),
        ``next_run`` (a platform-formatted timestamp, or ``None``),
        ``exec_path`` (the interpreter the installed unit runs, or ``None``
        when it cannot be read back), and ``notes`` (a list of strings, for
        anything only this adapter can see, such as systemd's linger check).
        ``None`` always means "cannot tell" and must never be reported as a
        finding; only a value that is present and wrong is one.
        """
        raise NotImplementedError


def _registry():
    from . import cron, systemd

    return {"systemd": systemd.SystemdAdapter, "cron": cron.CronAdapter}


def get(name) -> Adapter:
    registry = _registry()
    cls = registry.get(str(name or "").strip().lower())
    if cls is None:
        raise NoAdapter(
            f"no scheduler adapter named {name!r}. "
            f"Available here: {', '.join(available()) or 'none'}. "
            f"Known: {', '.join(sorted(registry))}")
    return cls()


def available() -> list:
    return [name for name, cls in sorted(_registry().items()) if cls().usable()]


def detect() -> str:
    """The best adapter for this machine.

    systemd first where it is init (a systemctl binary on a
    non-systemd box is not enough), then cron, which is the thin-client
    fallback. Never guesses: a machine with neither gets ``NoAdapter`` and a
    printed unit to install by hand.
    """
    for name in ("systemd", "cron"):
        try:
            if get(name).usable():
                return name
        except NoAdapter:
            continue
    raise NoAdapter(
        "no scheduler found on this machine (no systemd, no cron). "
        "Run `contextlake schedule install --platform systemd` to see the unit "
        "and install it yourself, or use `contextlake schedule run --foreground`.")


def systemd_is_init() -> bool:
    """Whether a systemd USER manager is reachable, not merely present.

    A systemctl binary and /run/systemd/system prove systemd is init. They do
    not prove `systemctl --user` can reach a user bus, which is what every
    call this adapter makes needs. A container, a CI runner, or an ssh session
    without lingering has the first two and not the third, and there
    `cmd_install` would write unit files that never fire.

    `show -p Version` rather than `is-system-running`: the latter exits
    non-zero on a merely degraded system, which is a working bus.
    """
    if shutil.which("systemctl") is None or not os.path.isdir("/run/systemd/system"):
        return False
    try:
        return subprocess.run(
            ["systemctl", "--user", "show", "-p", "Version"],
            capture_output=True, timeout=5, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
