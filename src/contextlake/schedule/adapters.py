"""Choosing a platform adapter, and the command it will run.

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
