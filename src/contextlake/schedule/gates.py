"""Should this run happen right now?

Three signals, each of which can answer "yes", "no", or "I cannot tell".

**"I cannot tell" always passes.** Headless servers, containers, Wayland
sessions and most CI runners cannot report user idleness; plenty of machines
have no battery at all. Treating an unreadable sensor as a reason to skip would
silently stop the scheduler forever on those machines, and it would look
like success from the outside.

On Linux, systemd's own ``ConditionACPower=true`` handles the battery case with
no code at all, so the systemd adapter sets it and this module is the fallback
for cron and for the foreground loop.
"""
from __future__ import annotations

import glob
import os
from collections import namedtuple

from ..logging_setup import log

GateResult = namedtuple("GateResult", "allowed reason")

_POWER_SUPPLY = "/sys/class/power_supply"


def on_battery():
    """``True`` on battery, ``False`` on mains, ``None`` if it cannot be told.

    Reads the mains-adapter's ``online`` flag rather than a battery's charge:
    a laptop plugged in still has a battery, and its presence says nothing
    about whether this run costs someone their afternoon.
    """
    try:
        for path in sorted(glob.glob(os.path.join(_POWER_SUPPLY, "*", "type"))):
            with open(path, encoding="utf-8") as fh:
                if fh.read().strip() != "Mains":
                    continue
            online = os.path.join(os.path.dirname(path), "online")
            with open(online, encoding="utf-8") as fh:
                return fh.read().strip() == "0"
    except OSError:
        return None
    return None


def load_average():
    """The 1-minute load average, or ``None`` where the platform has none
    (Windows)."""
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        return None
    try:
        return float(getloadavg()[0])
    except (OSError, ValueError, IndexError):
        return None


def user_is_idle():
    """``True`` if nobody is at the keyboard, ``None`` if it cannot be told.

    ``loginctl show-session`` reports ``IdleHint`` under a seat. It is accurate
    on X11 and always reports ``no`` on Wayland, which is why this gate is
    opt-in and why ``None`` passes.
    """
    import subprocess

    session = os.environ.get("XDG_SESSION_ID", "").strip()
    if not session:
        return None
    try:
        out = subprocess.run(
            ["loginctl", "show-session", session, "-p", "IdleHint"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip().split("=", 1)[-1].strip().lower()
    if value in ("yes", "true"):
        return True
    if value in ("no", "false"):
        return False
    return None


def _safe(fn):
    """Any detector failure is 'cannot tell', never an exception into a
    scheduled run."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a sensor is never worth failing a run over
        return None


def check(config) -> GateResult:
    """Whether to run now, and why not if not."""
    if str(config.get("schedule_on_battery", "skip")).strip().lower() != "run":
        if _safe(on_battery) is True:
            return GateResult(False, "on battery power (schedule_on_battery=skip)")

    if str(config.get("schedule_require_idle", "false")).strip().lower() in ("true", "yes", "1"):
        idle = _safe(user_is_idle)
        if idle is False:
            return GateResult(False, "someone is using this machine "
                                     "(schedule_require_idle=true)")
        if idle is None:
            # Passing is correct: an unreadable sensor must never block a run.
            # Saying so is also correct: XDG_SESSION_ID is unset under systemd
            # timers and cron, which are how this runs, so the setting is
            # inert in the deployment that would set it.
            log("WARNING: schedule_require_idle is on, but user idleness "
                "cannot be detected here (no login session), so the gate "
                "is inert.")

    raw = str(config.get("schedule_max_load", "")).strip()
    if raw:
        try:
            threshold = float(raw)
        except ValueError:
            # A typo disables a gate rather than blocking every run.
            threshold = None
        if threshold is not None:
            load = _safe(load_average)
            if load is not None and load > threshold:
                return GateResult(
                    False, f"1-minute load is {load:.2f}, above "
                           f"schedule_max_load={threshold:g}")

    return GateResult(True, "")
