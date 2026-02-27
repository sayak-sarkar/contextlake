"""Tiny, dependency-free terminal styling: colours, status glyphs, a progress bar.

Honours ``NO_COLOR`` / ``FORCE_COLOR`` and falls back to plain text when output is
not a TTY, so piped, redirected, and cron output stays clean. No third-party deps.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata

_CODES = {
    "reset": "0", "bold": "1", "dim": "2",
    "red": "31", "green": "32", "yellow": "33",
    "blue": "34", "magenta": "35", "cyan": "36", "gray": "90",
}

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR (colour) escape sequences from ``text``."""
    return _ANSI_RE.sub("", text)


def visible_width(text: str) -> int:
    """Number of terminal columns ``text`` occupies once printed.

    ANSI colour codes are ignored, zero-width/combining marks count as 0, and
    East-Asian wide/fullwidth characters count as 2 -- so right-alignment lines
    up identically regardless of colour or the characters in a repo path.
    """
    width = 0
    for ch in strip_ansi(text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def terminal_width(stream=None, default: int = 80) -> int:
    """Best-effort current terminal width (columns).

    Honours an explicit ``COLUMNS`` (useful in CI / for pinning), then the
    stream's own size, then a sane default -- never raises.
    """
    env = os.environ.get("COLUMNS")
    if env and env.strip().isdigit():
        return int(env)
    stream = stream if stream is not None else sys.stdout
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except Exception:  # noqa: BLE001 - not a real terminal; fall back
        try:
            import shutil

            return shutil.get_terminal_size((default, 24)).columns
        except Exception:  # noqa: BLE001
            return default


def align_right(left: str, right: str, width: int, min_gap: int = 2) -> str:
    """Lay ``left`` out with ``right`` flush against column ``width``.

    Returns ``left`` unchanged when there is not at least ``min_gap`` spaces of
    room for ``right`` -- so a long line degrades to just the message instead of
    wrapping or misaligning. Width is measured visibly (ANSI/wide-char aware).
    """
    pad = width - visible_width(left) - visible_width(right)
    if pad < min_gap:
        return left
    return f"{left}{' ' * pad}{right}"


def supports_color(stream=None) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 - a stream without isatty is treated as non-tty
        return False


def style(text: str, *names: str, stream=None) -> str:
    """Wrap ``text`` in ANSI codes for the named styles, or return it unchanged
    when colour is unsupported/disabled."""
    if not names or not supports_color(stream):
        return text
    codes = ";".join(_CODES[n] for n in names if n in _CODES)
    return f"\033[{codes}m{text}\033[0m" if codes else text


def bold(text: str, **kw) -> str:
    return style(text, "bold", **kw)


def dim(text: str, **kw) -> str:
    return style(text, "dim", **kw)


def green(text: str, **kw) -> str:
    return style(text, "green", **kw)


def red(text: str, **kw) -> str:
    return style(text, "red", **kw)


def yellow(text: str, **kw) -> str:
    return style(text, "yellow", **kw)


def cyan(text: str, **kw) -> str:
    return style(text, "cyan", **kw)


# --- status glyphs (pre-coloured) -----------------------------------------

def ok(label: str = "", **kw) -> str:
    return f"{green('✓', **kw)} {label}".rstrip()


def fail(label: str = "", **kw) -> str:
    return f"{red('✗', **kw)} {label}".rstrip()


def warn(label: str = "", **kw) -> str:
    return f"{yellow('⚠', **kw)} {label}".rstrip()


def skip(label: str = "", **kw) -> str:
    return f"{dim('⊘', **kw)} {label}".rstrip()


def bar(done: int, total: int, width: int = 24) -> str:
    """A textual progress bar, e.g. ``[████████░░░░░░] 8/16``."""
    total = max(0, total)
    if total == 0:
        return f"[{'─' * width}] 0/0"
    done = max(0, min(done, total))
    filled = round(width * done / total)
    return f"[{'█' * filled}{'░' * (width - filled)}] {done}/{total}"
