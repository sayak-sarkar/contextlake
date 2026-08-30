"""macOS LaunchAgent, the per-user scheduler on a Mac.

A LaunchAgent is a plist in ``~/Library/LaunchAgents`` loaded into the user's
GUI domain. ``StartInterval`` takes SECONDS and launchd replays a run missed
while the machine was asleep, which is why ``catches_up_after_sleep`` is true
here as it is for systemd.

Not runnable on this machine. Everything here is verified by rendering the
plist and by asserting the exact ``launchctl`` argv, never by executing it.
Do not describe this backend as verified by execution.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys

from .base import Adapter, check_name

#: Reverse-DNS, because launchd labels share ONE namespace per user. A bare
#: ``contextlake-default`` would sit in the same namespace as anything else
#: using that name. Prefixed unconditionally, one rule, the same choice
#: ``systemd.unit_name`` made after an earlier draft's conditional prefix gave
#: two naming rules that both passed their own tests.
LABEL_PREFIX = "in.sayak.contextlake."


def agent_dir() -> str:
    return os.path.expanduser("~/Library/LaunchAgents")


def label(job_name) -> str:
    """``default`` to ``in.sayak.contextlake.default``."""
    return LABEL_PREFIX + check_name(job_name)


def plist_name(job_name) -> str:
    return label(job_name) + ".plist"


def _launchctl(*argv):
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True,
                          errors="replace", check=False)


def _domain() -> str:
    """``gui/<uid>``, the domain a LaunchAgent belongs to.

    ``bootstrap gui/$UID`` and not the older ``load``: ``load`` is deprecated
    and under a modern launchd it can return 0 while doing nothing, which is
    the silent-success case this package refuses to ship.
    """
    return f"gui/{os.getuid()}"


class LaunchdAdapter(Adapter):
    id = "launchd"
    # launchd runs a missed StartInterval job when the machine wakes.
    catches_up_after_sleep = True
    # `label` is a fact about the install, not a file to write.
    metadata_keys = frozenset({"label", "interval_s"})

    def usable(self) -> bool:
        return sys.platform == "darwin" and bool(shutil.which("launchctl"))

    def render(self, job, interval_s, exec_argv, on_battery="skip", **_options) -> dict:
        name = check_name(job.name)
        # StartInterval is an INTEGER NUMBER OF SECONDS. A float is rejected by
        # launchd and a unit suffix is not accepted at all, so 70 minutes is
        # 4200 here and never "70m".
        seconds = max(1, int(round(float(interval_s))))
        plist = {
            "Label": label(name),
            "ProgramArguments": list(exec_argv),
            "StartInterval": seconds,
            # Nice and idle IO, matching the systemd unit: a scheduled index
            # must not compete with whatever the person is actually doing.
            "Nice": 10,
            "ProcessType": "Background",
            "RunAtLoad": False,
        }
        # launchd has no ConditionACPower. The battery gate is enforced in
        # `gates.check` at run time instead, which is why nothing is added
        # here rather than a key being invented that launchd would ignore.
        return {
            plist_name(name): plistlib.dumps(plist).decode("utf-8"),
            "label": label(name),
            "interval_s": seconds,
        }

    def install(self, job, interval_s, exec_argv, **options) -> list:
        directory = agent_dir()
        os.makedirs(directory, exist_ok=True)
        rendered = self.render(job, interval_s, exec_argv, **options)
        path = os.path.join(directory, plist_name(job.name))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(rendered[plist_name(job.name)])
        # Replace an existing agent rather than layering a second one: bootout
        # first, ignoring its exit code, because "not loaded" is the normal
        # case on a first install and is not a failure.
        _launchctl("bootout", f"{_domain()}/{label(job.name)}")
        loaded = _launchctl("bootstrap", _domain(), path)
        if loaded.returncode != 0:
            # Same rule as the systemd adapter: a plist on disk with no loaded
            # agent is the silent-failure state this exists to rule out. The
            # caller degrades on OSError, prints the plist, and says to load it
            # by hand. The file stays, and loading it later is a valid recovery.
            raise OSError(
                f"launchctl bootstrap {_domain()} {path} failed: "
                f"{loaded.stderr.strip() or loaded.stdout.strip() or 'no output'}")
        return [path]

    def uninstall(self, job) -> list:
        _launchctl("bootout", f"{_domain()}/{label(job.name)}")
        path = os.path.join(agent_dir(), plist_name(job.name))
        try:
            os.unlink(path)
        except OSError:
            return []       # Already gone. Not an error.
        return [path]

    def installed_names(self):
        """Read the agent directory, not ``launchctl list``.

        The plist is what survives a deleted job record, and it is on disk
        whether or not the agent is currently loaded or a GUI session exists.
        A directory that does not exist IS a measurement and its answer is
        none; a directory that cannot be read is ``None``, meaning cannot tell.
        """
        directory = agent_dir()
        if not os.path.isdir(directory):
            return []
        try:
            entries = os.listdir(directory)
        except OSError:
            return None
        return sorted(e[len(LABEL_PREFIX):-len(".plist")] for e in entries
                      if e.startswith(LABEL_PREFIX) and e.endswith(".plist"))

    def state(self, job) -> dict:
        path = os.path.join(agent_dir(), plist_name(job.name))
        installed = os.path.exists(path)
        notes, interval_s, exec_path = [], None, None
        if installed:
            # Read the interval back off the INSTALLED plist rather than
            # reporting what was requested. They differ whenever an install
            # rounded, and reporting the request would hide that.
            try:
                with open(path, "rb") as fh:
                    data = plistlib.load(fh)
            except (OSError, ValueError) as e:
                notes.append(f"could not read {path}: {e}")
                data = {}
            raw = data.get("StartInterval")
            if isinstance(raw, (int, float)):
                interval_s = float(raw)
            argv = data.get("ProgramArguments") or []
            if argv and isinstance(argv, list):
                exec_path = str(argv[0])
        return {
            "installed": installed,
            "interval_s": interval_s,
            # launchd exposes no next-fire time for a StartInterval agent.
            # None is "cannot tell", which is the honest answer, rather than a
            # computed guess that would drift from what launchd actually does.
            "next_run": None,
            "exec_path": exec_path,
            "notes": notes,
        }
