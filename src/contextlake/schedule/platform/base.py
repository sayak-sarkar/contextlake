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

# The five keys every adapter's state() must return. Named here, not only in
# the docstring, so tests/test_schedule_platform_registry.py can enforce it
# against every registered adapter instead of trusting prose: `cmds.py` reads
# each with `.get()`, so an adapter that omits a key fails silently, not
# loudly, and a test fixture that omits one is exercising a path no real
# adapter takes.
STATE_KEYS = ("installed", "interval_s", "next_run", "exec_path", "notes")


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
    # Keys `render()` returns that are facts about the install, not files to
    # write or show. Empty by default: every key an adapter returns is an
    # artefact unless it lists the key here. Only `cron` needs this today
    # (`spec`, `interval_s`, `notes`, `name`); systemd's two keys are both
    # unit filenames. See `render` below for the full contract.
    metadata_keys: frozenset = frozenset()

    def usable(self) -> bool:
        raise NotImplementedError

    def render(self, job, interval_s, exec_argv, **options) -> dict:
        """The artefacts this adapter would install, plus whatever metadata
        it needs to report back.

        Pure: no filesystem or subprocess call, so a backend can be
        golden-file tested without being installable, and so calling it
        again after `install` (the only way to learn a fact `install` itself
        does not return, such as the interval cron rounded to) costs
        nothing.

        Every key NOT listed in `metadata_keys` is an artefact: a filename
        (or a platform-chosen label, for a backend with no filename until
        installed) mapped to the exact text `install` writes, and what the
        degrade path prints under "install these yourself" when `install`
        cannot run. A key listed in `metadata_keys` carries a fact about the
        install instead and must never be printed as if it were a file. A
        `notes` key, when present, holds one string here; `state()`'s own
        `notes` key below holds a list. They answer different questions
        ("what did this install decide" vs "what does the live unit show
        now") and are not interchangeable.
        """
        raise NotImplementedError

    def install(self, job, interval_s, exec_argv, **options) -> list:
        raise NotImplementedError

    def uninstall(self, job) -> list:
        raise NotImplementedError

    def state(self, job) -> dict:
        """What is installed for ``job``, read from this platform, not
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

    def installed_names(self):
        """Job names this platform currently has units for, or ``None``.

        ``state()`` answers "is job X installed?", which can only be asked
        about a job that still has a record. The reverse case has no reader:
        a unit whose record was deleted keeps firing on schedule, is absent
        from ``schedule list``, and cannot be removed by name because
        ``uninstall`` looks the name up in the record it no longer has.

        ``None`` means this adapter cannot enumerate, which is not the same
        as an empty list. An empty list is a measurement saying nothing is
        installed; ``None`` is the absence of one, and reporting "no orphans"
        from it would be a claim nothing checked. Adapters that cannot
        enumerate inherit this default.
        """
        return None


def _registry():
    from . import cloud, cron, k8s, launchd, systemd, windows

    return {"systemd": systemd.SystemdAdapter,
            "launchd": launchd.LaunchdAdapter,
            "windows": windows.WindowsAdapter,
            "k8s": k8s.K8sAdapter,
            "aws": cloud.AwsAdapter,
            "azure": cloud.AzureAdapter,
            "cron": cron.CronAdapter}


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


def registered() -> list:
    """Every adapter name, usable here or not.

    ``available()`` answers "what can this machine schedule with?" and is right
    for install. This answers "what could have installed something?", which is
    what orphan enumeration needs: a unit installed under systemd stays a unit
    after the machine stops offering systemd, and ``available()`` would filter
    out exactly that case.
    """
    return sorted(_registry())


def detect() -> str:
    """The best adapter for this machine.

    systemd first where it is init (a systemctl binary on a
    non-systemd box is not enough), then launchd on macOS, then Task Scheduler
    on Windows, then cron, which is the thin-client fallback. Never guesses: a
    machine with none of them gets ``NoAdapter`` and a printed unit to install
    by hand.

    Order matters on a Mac: cron still exists there and would answer
    ``usable()``, so launchd has to be asked first or every Mac would silently
    get the fallback. It also means this list is not the registry's order and
    must not be replaced by one.

    **Cluster and cloud adapters are never auto-detected.** ``k8s`` answers
    ``usable()`` whenever ``kubectl`` is on PATH, and ``aws`` and ``azure``
    whenever their CLI is installed, all of which are true on plenty of
    workstations that are not themselves running in a cluster or deploying to
    that account. Scheduling someone's laptop job into whatever cluster their
    kubeconfig points at, or whatever account their credentials reach, is not
    a guess this is allowed to make. They are reachable through
    ``--platform k8s|aws|azure`` and are registered, so `list` still reports
    orphans there, but they are deliberately absent from this loop.
    """
    for name in ("systemd", "launchd", "windows", "cron"):
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
