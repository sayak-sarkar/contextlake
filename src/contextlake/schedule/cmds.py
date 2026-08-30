"""Routing one `contextlake schedule` invocation to the command that serves it.

This module used to hold every command. They now live in five modules split by
what they do: ``report`` answers questions and changes nothing, ``actions``
writes job records and platform units, ``runner`` runs a scheduled cycle,
``adhoc`` handles `schedule interval`, and ``settings`` and ``adapters`` hold
what the rest share.

The command entry points are re-exported here because `dispatch` resolves them
and because ``cmds`` is the import path the CLI already uses.

**Patching in tests: patch the module that OWNS the name, not this one.** A
re-export binds a copy, so ``monkeypatch.setattr(cmds, "_spawn", ...)`` would
set a name nothing calls once the caller lives in ``runner``, and the test
would pass for the wrong reason. ``_adapter_for``, ``exec_argv_for``,
``executable_missing`` and ``_report_installed`` belong to ``adapters``;
``_spawn``, ``in_container``, ``_mount_point_of`` and ``store_is_ephemeral``
belong to ``runner``. ``log`` is imported by value into every module that logs,
so a test capturing output patches it in each module it expects output from.

Core tier. ``run`` is the only action that can reach the knowledge layer, and it
does so by spawning a subprocess, so nothing here imports ``contextlake.kb`` at
all. That is what lets `schedule recommend` and `schedule status` work on an
install without the ``[kb]`` extra.
"""
from __future__ import annotations

from ..logging_setup import log
from .actions import cmd_install, cmd_reset, cmd_uninstall  # noqa: F401
from .adhoc import cmd_interval  # noqa: F401
from .report import cmd_list, cmd_recommend, cmd_status  # noqa: F401
from .runner import cmd_run  # noqa: F401
from .settings import (  # noqa: F401
    _duration_or,
    _float_or,
    current_recommendation,
    resolve_interval,
    settings_from_config,
)

ACTIONS = ("recommend", "install", "uninstall", "status", "run", "list", "reset", "interval")


def dispatch(args, config) -> int:
    """Route one `schedule` invocation."""
    action = args.action
    if action == "recommend":
        return cmd_recommend(args, config)
    if action == "list":
        return cmd_list(args, config)
    if action == "install":
        return cmd_install(args, config)
    if action == "uninstall":
        return cmd_uninstall(args, config)
    if action == "status":
        return cmd_status(args, config)
    if action == "run":
        return cmd_run(args, config)
    if action == "reset":
        return cmd_reset(args, config)
    if action == "interval":
        return cmd_interval(args, config)
    log(f"`schedule {action}` is not implemented yet.")
    return 1
