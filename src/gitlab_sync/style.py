"""Tiny, dependency-free terminal styling: colours, status glyphs, a progress bar.

Honours ``NO_COLOR`` / ``FORCE_COLOR`` and falls back to plain text when output is
not a TTY, so piped, redirected, and cron output stays clean. No third-party deps.
"""

from __future__ import annotations

import os
import sys

_CODES = {
    "reset": "0", "bold": "1", "dim": "2",
    "red": "31", "green": "32", "yellow": "33",
    "blue": "34", "magenta": "35", "cyan": "36", "gray": "90",
}


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
