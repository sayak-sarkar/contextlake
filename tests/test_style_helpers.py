"""Tests for the style.py additions: new states, status_line, summary_line,
header, kv."""

import io

from contextlake import style


class _Tty(io.StringIO):
    def isatty(self):
        return True


class _NotTty(io.StringIO):
    def isatty(self):
        return False


# --- new state accessors ----------------------------------------------------

def test_nochange_glyph_and_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert style.nochange("same", stream=_Tty()) == f"{style.dim('=', stream=_Tty())} same"
    assert style.strip_ansi(style.nochange("same", stream=_Tty())) == "= same"


def test_nochange_plain_without_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.nochange("same", stream=_Tty()) == "= same"
    assert style.nochange(stream=_Tty()) == "="


def test_switched_glyph_and_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    out = style.switched("moved", stream=_Tty())
    assert out == "\033[36m↝\033[0m moved"
    assert style.strip_ansi(out) == "↝ moved"


def test_switched_plain_without_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.switched("moved", stream=_Tty()) == "↝ moved"
    assert style.switched(stream=_Tty()) == "↝"


def test_dryrun_glyph_and_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    out = style.dryrun("would sync", stream=_Tty())
    assert out == "\033[33m~\033[0m would sync"
    assert style.strip_ansi(out) == "~ would sync"


def test_dryrun_plain_without_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.dryrun("would sync", stream=_Tty()) == "~ would sync"
    assert style.dryrun(stream=_Tty()) == "~"


# --- status_line --------------------------------------------------------

def test_status_line_ok_contains_counter_glyph_path_message(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = style.status_line(2, 5, "ok", "acme/repo", "done", stream=_Tty())
    assert "[2/5]" in out
    assert "✓" in out
    assert "acme/repo" in out
    assert "done" in out
    assert out == "[2/5] ✓ acme/repo: done"


def test_status_line_glyph_per_state(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    expected = {
        "ok": "✓",
        "warn": "⚠",
        "fail": "✗",
        "skip": "⊘",
        "nochange": "=",
        "switched": "↝",
        "dryrun": "~",
    }
    for state, glyph in expected.items():
        out = style.status_line(1, 1, state, "p", "m", stream=_Tty())
        assert glyph in out, f"state {state!r} missing glyph {glyph!r} in {out!r}"


def test_status_line_unknown_state_raises():
    try:
        style.status_line(1, 1, "bogus", "p", "m", stream=_Tty())
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown state")


def test_status_line_no_color_has_no_ansi(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = style.status_line(2, 5, "ok", "acme/repo", "done", stream=_Tty())
    assert "\033[" not in out


def test_status_line_colored_on_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    out = style.status_line(2, 5, "ok", "acme/repo", "done", stream=_Tty())
    assert "\033[" in out
    assert style.strip_ansi(out) == "[2/5] ✓ acme/repo: done"


# --- summary_line --------------------------------------------------------

def test_summary_line_starts_with_glyph(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = style.summary_line("ok", "Embed complete: 12 files", stream=_Tty())
    assert out == "✓ Embed complete: 12 files"


def test_summary_line_glyph_per_state(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    expected = {
        "ok": "✓",
        "warn": "⚠",
        "fail": "✗",
        "skip": "⊘",
        "nochange": "=",
        "switched": "↝",
        "dryrun": "~",
    }
    for state, glyph in expected.items():
        out = style.summary_line(state, "text", stream=_Tty())
        assert out == f"{glyph} text"


# --- header --------------------------------------------------------------

def test_header_contains_arrow_and_title(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = style.header("Mirror repositories", stream=_Tty())
    assert out == "▶ Mirror repositories"
    assert "▶" in out


def test_header_matches_bootstrap_bold_cyan_styling(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    out = style.header("Mirror repositories", stream=_Tty())
    assert style.strip_ansi(out) == "▶ Mirror repositories"
    # bold-cyan treatment, matching cli.py's `style.bold(style.cyan(f"▶ {title}"))`
    assert out == style.bold(style.cyan("▶ Mirror repositories", stream=_Tty()), stream=_Tty())


def test_header_no_color_has_no_ansi(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = style.header("Mirror repositories", stream=_Tty())
    assert "\033[" not in out


# --- kv --------------------------------------------------------------------

def test_kv_aligns_rows_and_contains_labels_and_values(monkeypatch):
    monkeypatch.delenv("COLUMNS", raising=False)
    out = style.kv([("A", "1"), ("Bee", "22")], stream=_NotTty())
    lines = out.splitlines()
    assert len(lines) == 2
    assert "A" in lines[0] and "1" in lines[0]
    assert "Bee" in lines[1] and "22" in lines[1]
    # both rows share the same rendered width (columns line up)
    assert style.visible_width(lines[0]) == style.visible_width(lines[1])


def test_kv_respects_narrow_columns(monkeypatch):
    monkeypatch.setenv("COLUMNS", "5")
    out = style.kv([("A", "1"), ("Bee", "22")], stream=_NotTty())
    lines = out.splitlines()
    # the wide row has no room for a min_gap-separated value -> degrades to label only
    assert lines[1] == "Bee"
    for line in lines:
        assert style.visible_width(line) <= 5


def test_kv_empty_pairs_returns_empty_string():
    assert style.kv([], stream=_NotTty()) == ""


def test_kv_explicit_width_overrides_terminal_width(monkeypatch):
    monkeypatch.delenv("COLUMNS", raising=False)
    out = style.kv([("A", "1")], width=10, stream=_NotTty())
    assert style.visible_width(out) == 10
