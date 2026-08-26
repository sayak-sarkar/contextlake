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

# A job name becomes a filename, a systemd unit name, and a crontab marker. Keep
# it to characters that cannot escape a directory, split a unit name, or need
# quoting in any of the three.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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
        raise NotImplementedError


def _registry():
    # Only systemd is registered in this task. Task 9 adds a `cron` line
    # here; `platform/cron.py` does not exist yet, so importing it
    # unconditionally would turn every call below into an ImportError.
    from . import systemd

    return {"systemd": systemd.SystemdAdapter}


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
    return (shutil.which("systemctl") is not None
            and os.path.isdir("/run/systemd/system"))
