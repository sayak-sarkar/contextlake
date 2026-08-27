"""`dispatch()` routes eight actions to eight handlers; before this file only
one (`interval`, via test_schedule_adhoc.py) had any coverage of the routing
itself. Each handler is monkeypatched to a recorder rather than executed, so
this stays a routing test and never a behaviour test for the handler it
reaches.
"""
from __future__ import annotations

import argparse

import pytest

from contextlake.schedule import cmds

# Every action in cmds.ACTIONS mapped to the handler dispatch() must reach.
# Kept as its own dict, not derived from ACTIONS by a naming convention, so a
# handler renamed out of step with its action fails test_every_action below
# instead of both sides silently agreeing on the wrong name.
_HANDLERS = {
    "recommend": "cmd_recommend",
    "list": "cmd_list",
    "install": "cmd_install",
    "uninstall": "cmd_uninstall",
    "status": "cmd_status",
    "run": "cmd_run",
    "reset": "cmd_reset",
    "interval": "cmd_interval",
}


def test_every_action_has_a_handler_mapped_in_this_test():
    """cmds.ACTIONS and this file's map must name the same actions, or an
    action added to one and not the other would leave dispatch's routing for
    it silently unexercised below."""
    assert set(cmds.ACTIONS) == set(_HANDLERS)


@pytest.mark.parametrize("action", sorted(cmds.ACTIONS))
def test_dispatch_routes_each_action_to_its_own_handler_only(action, monkeypatch):
    called = []
    for handler_name in _HANDLERS.values():
        monkeypatch.setattr(
            cmds, handler_name,
            lambda args, config, _name=handler_name: called.append(_name) or 0)

    rc = cmds.dispatch(argparse.Namespace(action=action), {})

    assert called == [_HANDLERS[action]], (
        f"`schedule {action}` must reach {_HANDLERS[action]} and nothing else")
    assert rc == 0
