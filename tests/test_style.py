"""Tests for the dependency-free terminal styling helpers."""

import io

from contextlake import style


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


def test_strip_ansi():
    assert style.strip_ansi("\033[32mhi\033[0m") == "hi"
    assert style.strip_ansi("plain") == "plain"


def test_visible_width_ignores_ansi_and_counts_wide():
    assert style.visible_width("hello") == 5
    assert style.visible_width("\033[31m✓ ok\033[0m") == 4  # ANSI ignored, glyph=1
    assert style.visible_width("日本") == 4  # wide chars count as 2 each
    assert style.visible_width("é") == 1  # combining accent counts as 0


def test_align_right_places_text_at_width():
    out = style.align_right("left", "RR", 12)
    assert out == "left" + " " * 6 + "RR"
    assert style.visible_width(out) == 12


def test_align_right_drops_when_no_room():
    # not enough columns for the min gap + right text -> left returned unchanged
    assert style.align_right("x" * 18, "RR", 20) == "x" * 18


def test_align_right_measures_visibly():
    # ANSI on either side must not affect where the right text lands
    out = style.align_right("\033[36mleft\033[0m", "\033[2mRR\033[0m", 12)
    assert style.visible_width(out) == 12


def test_terminal_width_honours_columns_env(monkeypatch):
    monkeypatch.setenv("COLUMNS", "123")
    assert style.terminal_width(_NotTty()) == 123


def test_terminal_width_falls_back(monkeypatch):
    monkeypatch.delenv("COLUMNS", raising=False)
    # a StringIO has no real fileno terminal -> default
    assert style.terminal_width(_NotTty(), default=77) == 77
