"""`--platform`'s help must list every adapter that exists.

It said "(systemd | cron)" while five more were registered. A hand-written list
in help text has no gate on it: nothing fails when an adapter is added and the
sentence is not, and the only symptom is a user who cannot discover the backend
they need.
"""
from __future__ import annotations

from contextlake import cli
from contextlake.schedule.platform import base


def _platform_help() -> str:
    parser = cli.build_parser()
    for action in parser._actions:            # noqa: SLF001 - argparse has no public reader
        if getattr(action, "dest", None) == "command":
            for name, sub in action.choices.items():
                if name != "schedule":
                    continue
                for sub_action in sub._actions:   # noqa: SLF001
                    if sub_action.dest == "platform":
                        return sub_action.help or ""
    raise AssertionError("could not find `schedule --platform` in the parser")


def test_every_registered_adapter_appears_in_the_platform_help():
    text = _platform_help()
    missing = [name for name in base.registered() if name not in text]
    assert not missing, (
        f"these adapters are registered but absent from `--platform`'s help, so "
        f"nothing tells a user they exist: {missing}")


def test_the_help_says_cloud_adapters_are_not_auto_detected():
    """detect() deliberately skips them, so naming one is the ONLY way to
    reach it. Help that lists them without saying that reads as though they
    are picked automatically."""
    assert "never auto-detected" in _platform_help()
