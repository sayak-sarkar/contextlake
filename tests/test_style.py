"""Tests for the dependency-free terminal styling helpers."""

import io

from gitlab_sync import style


class _Tty(io.StringIO):
    def isatty(self):
        return True


class _NotTty(io.StringIO):
    def isatty(self):
        return False


def test_no_color_env_disables(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.green("hi", stream=_Tty()) == "hi"


def test_force_color_overrides_non_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert style.green("hi", stream=_NotTty()) == "\033[32mhi\033[0m"


def test_plain_when_not_a_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert style.bold("hi", stream=_NotTty()) == "hi"


def test_color_on_a_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert style.red("x", stream=_Tty()) == "\033[31mx\033[0m"


def test_glyphs_plain_without_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.ok("done", stream=_Tty()) == "✓ done"
    assert style.fail(stream=_Tty()) == "✗"


def test_bar():
    assert style.bar(0, 0) == "[" + "─" * 24 + "] 0/0"
    assert style.bar(4, 8, width=8) == "[████░░░░] 4/8"
    assert style.bar(99, 8, width=8) == "[████████] 8/8"  # clamps overflow
