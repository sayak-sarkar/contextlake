"""Tiny, dependency-free terminal styling: colours, status glyphs, a progress bar.

Honours ``NO_COLOR`` / ``FORCE_COLOR`` and falls back to plain text when output is
not a TTY, so piped, redirected, and cron output stays clean. No third-party deps.
"""

from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from collections import deque

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


def nochange(label: str = "", **kw) -> str:
    return f"{dim('=', **kw)} {label}".rstrip()


def switched(label: str = "", **kw) -> str:
    return f"{cyan('↝', **kw)} {label}".rstrip()


def dryrun(label: str = "", **kw) -> str:
    return f"{yellow('~', **kw)} {label}".rstrip()


_STATE_ACCESSORS = {
    "ok": ok,
    "warn": warn,
    "fail": fail,
    "skip": skip,
    "nochange": nochange,
    "switched": switched,
    "dryrun": dryrun,
}


def _state_glyph(state: str, **kw) -> str:
    try:
        accessor = _STATE_ACCESSORS[state]
    except KeyError:
        raise ValueError(f"unknown state: {state!r}") from None
    return accessor(**kw)


def status_line(i, total, state: str, path: str, message: str, *, stream=None) -> str:
    """A coloured per-item progress line: dim counter, state glyph, cyan path.

    Promotes the ``[i/total] glyph path: message`` shape hand-built by callers
    (e.g. ``core.py``'s ``_status``) into a single state-driven helper.
    """
    glyph = _state_glyph(state, stream=stream)
    counter = dim(f"[{i}/{total}]", stream=stream)
    return f"{counter} {glyph} {cyan(path, stream=stream)}: {message}"


def summary_line(state: str, text: str, *, stream=None) -> str:
    """A single glyph-prefixed finale line, e.g. ``✓ Embed complete: ...``."""
    return f"{_state_glyph(state, stream=stream)} {text}"


def header(title: str, *, stream=None) -> str:
    """A bold-cyan phase header: ``▶ Title``.

    Promotes the styling bootstrap's ``_stage`` closure hand-builds today.
    """
    return bold(cyan(f"▶ {title}", stream=stream), stream=stream)


def kv(pairs, *, width=None, stream=None) -> str:
    """Aligned label/value rows, e.g. for a status summary.

    ``pairs`` is a list of ``(label, value)`` tuples; each is rendered as
    ``label`` flush-left and ``value`` flush-right of a shared column, clamped
    to ``terminal_width`` so rows degrade to just the label when the terminal
    is too narrow to fit both (see :func:`align_right`). Returns a multi-line
    string with no trailing newline.
    """
    if not pairs:
        return ""
    if width is None:
        content_width = (
            max(visible_width(str(label)) for label, _ in pairs)
            + 2
            + max(visible_width(str(value)) for _, value in pairs)
        )
        width = min(content_width, terminal_width(stream))
    return "\n".join(
        align_right(str(label), str(value), width) for label, value in pairs
    )


def bar(done: int, total: int, width: int = 24) -> str:
    """A textual progress bar, e.g. ``[████████░░░░░░] 8/16``."""
    total = max(0, total)
    if total == 0:
        return f"[{'─' * width}] 0/0"
    done = max(0, min(done, total))
    filled = round(width * done / total)
    return f"[{'█' * filled}{'░' * (width - filled)}] {done}/{total}"


# --- progress reporting -----------------------------------------------------

def _fmt_hms(seconds: float) -> str:
    """Format ``seconds`` as ``H:MM:SS`` once it reaches an hour, else ``MM:SS``."""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class Progress:
    """Count-based CLI progress reporter: a live bar on a TTY, periodic
    summary lines otherwise.

    Stdlib-only, writes to ``stream`` (default ``sys.stderr``) so it never
    collides with ``log()`` output on stdout. Deliberately count-based (an
    empirical pass showed node-count does not predict wall-clock duration)
    but ``weight``-agnostic: :meth:`advance` accepts an optional weight so a
    future data-backed pass can size-weight progress without changing this
    helper's shape.
    """

    _BAR_WIDTH = 14

    def __init__(
        self,
        total: int | None,
        *,
        label: str = "",
        now=time.monotonic,
        stream=None,
        min_interval: float = 0.5,
        summary_every: int = 25,
        summary_seconds: float = 30.0,
    ) -> None:
        self._total = total
        self._label = label
        self._now = now
        self._stream = stream if stream is not None else sys.stderr
        self._min_interval = min_interval
        self._summary_every = summary_every
        self._summary_seconds = summary_seconds

        self._count = 0
        self._done_weight = 0.0
        # Weight-agnostic bookkeeping for a future size-weighted pass; not
        # used by today's count-based rendering.
        self._total_weight = total if total is not None else None

        self._start = self._now()
        self._last_tick = self._start
        self._recent: deque[float] = deque(maxlen=20)
        self._last_render = self._start
        self._first = True
        try:
            self._tty = bool(self._stream.isatty())
        except Exception:  # noqa: BLE001 - a stream without isatty is non-tty
            self._tty = False

    def advance(self, item_desc: str = "", *, weight: float = 1.0) -> None:
        """Record one completed item and (throttled) re-render."""
        del item_desc  # not part of the rendered line today; kept for callers
        now = self._now()
        dur = now - self._last_tick
        self._last_tick = now
        self._recent.append(dur)
        self._count += 1
        self._done_weight += weight
        self._render(now)

    def done(self, summary: str = "") -> None:
        """Finish the run: clear the live bar (TTY) or print a final line."""
        now = self._now()
        if self._tty:
            width = terminal_width(self._stream)
            self._stream.write("\r")
            self._stream.write(" " * width)
            self._stream.write("\r")
            if summary:
                self._stream.write(summary + "\n")
        else:
            self._stream.write((summary or self._line(now)) + "\n")
        self._stream.flush()

    # -- internal ------------------------------------------------------

    def _render(self, now: float) -> None:
        if self._tty:
            if self._first or (now - self._last_render) >= self._min_interval:
                self._write_tty_frame(now)
                self._first = False
                self._last_render = now
        else:
            due_count = self._summary_every > 0 and self._count % self._summary_every == 0
            due_time = (now - self._last_render) >= self._summary_seconds
            if due_count or due_time:
                self._stream.write(self._line(now) + "\n")
                self._last_render = now
                self._stream.flush()

    def _write_tty_frame(self, now: float) -> None:
        line = self._line(now)
        width = terminal_width(self._stream)
        pad = width - visible_width(line)
        if pad > 0:
            line = line + (" " * pad)
        self._stream.write("\r" + line)
        # stderr is line-buffered; a \r-terminated frame has no trailing "\n"
        # to trigger a flush on its own, so the live bar would never actually
        # appear on screen without an explicit flush here.
        self._stream.flush()

    def _line(self, now: float) -> str:
        elapsed_seconds = now - self._start
        elapsed = _fmt_hms(elapsed_seconds)
        recent = self._recent
        mean = (sum(recent) / len(recent)) if recent else 0.0
        if recent and mean > 0:
            rate = 60.0 / mean
        else:
            rate = 60.0 * self._count / max(elapsed_seconds, 1e-9)

        if self._total is None:
            head = f"{self._count} done"
            tail = f"{elapsed} elapsed · {rate:.1f}/min"
        else:
            total = self._total
            pct = round(100 * self._count / total) if total else 0
            bar_str = bar(self._count, total, self._BAR_WIDTH)
            remaining = max(total - self._count, 0)
            eta = _fmt_hms(remaining * mean if recent else 0.0)
            head = f"{bar_str} ({pct}%)"
            tail = f"{elapsed} elapsed · ~{eta} left · {rate:.1f}/min"

        prefix = f"{self._label} " if self._label else ""
        plain = f"{prefix}{head} · {tail}"

        width = terminal_width(self._stream)
        if visible_width(plain) > width:
            return plain[:width]

        colored_tail = dim(tail, stream=self._stream)
        return f"{prefix}{head} · {colored_tail}"
