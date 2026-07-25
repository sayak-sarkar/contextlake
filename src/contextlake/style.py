"""Tiny, dependency-free terminal styling: colours, status glyphs, a progress bar.

Honours ``NO_COLOR`` / ``FORCE_COLOR`` and falls back to plain text when output is
not a TTY, so piped, redirected, and cron output stays clean. No third-party deps.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import threading
import time
import unicodedata
import weakref
from collections import deque

_CODES = {
    "reset": "0", "bold": "1", "dim": "2",
    "red": "31", "green": "32", "yellow": "33",
    "blue": "34", "magenta": "35", "cyan": "36", "gray": "90",
}

# Any CSI sequence, not just SGR colour: the live progress bar also emits an
# erase-in-line (``\033[K``), and width maths must treat that as zero columns.
_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI escape sequences (colour, erase, cursor moves) from ``text``."""
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


def elide(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` columns, dropping from the middle.

    Middle-elision (not a tail cut) because the informative parts of a repo id
    are at both ends: the leading namespace and the trailing repo name.
    """
    if limit <= 0:
        return ""
    if visible_width(text) <= limit:
        return text
    if limit <= 3:
        return "." * limit
    keep = limit - 3
    head = (keep + 1) // 2
    return f"{text[:head]}...{text[len(text) - (keep - head):]}"


def status_line(i, total, state: str, path: str, message: str, *, stream=None) -> str:
    """A coloured per-item progress line: dim counter, state glyph, cyan path.

    Promotes the ``[i/total] glyph path: message`` shape hand-built by callers
    (e.g. ``core.py``'s ``_status``) into a single state-driven helper.

    Clamped to one terminal row: deeply nested repo ids plus a git message used to
    wrap onto a second line, which tore through the live progress bar below it.
    The path is elided before the message is trimmed, so the reason stays legible.
    """
    glyph = _state_glyph(state, stream=stream)
    counter = dim(f"[{i}/{total}]", stream=stream)
    message = " ".join((message or "").split())  # never let a newline through

    out = stream if stream is not None else sys.stdout
    # Clamp only for a live terminal. Wrapping matters because it tears through
    # the progress bar on the row below; in a pipe or a log file there is no bar,
    # and a truncated repo id or reason is strictly worse than a long line.
    try:
        is_tty = bool(out.isatty())
    except Exception:  # noqa: BLE001 - a stream without isatty is not a terminal
        is_tty = False

    if is_tty:
        # "[i/total] G " + path + ": " + message, measured without colour codes.
        budget = terminal_width(out) - (visible_width(f"[{i}/{total}] x ") + 2)
        if budget > 0 and visible_width(path) + visible_width(message) > budget:
            # The reason is short and bounded, the path is what runs long, so
            # spend the overflow on the path and keep the message readable.
            path = elide(path, max(20, budget - visible_width(message)))
            message = elide(message, max(0, budget - visible_width(path)))

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


# Rewind to column 0 and erase to end of line. Replaces padding the frame out to
# the full terminal width: same visual erase, no flood of trailing spaces (which
# also left the cursor at the right margin, so the next stdout line started there).
_ERASE_LINE = "\r\033[K"

# An ETA needs enough completions (or enough wall-clock) to mean anything.
_ETA_MIN_ITEMS = 10
_ETA_MIN_SECONDS = 5.0

# The bar (stderr) and log lines (stdout) share one terminal cursor, so a frame
# left on screen gets welded to whatever prints next. Any live bar registers here
# so the logging handler can erase it, let the line through, and repaint after.
#
# A WeakSet, so a bar abandoned without done() (an exception mid-run) is dropped
# when it is collected instead of being repainted by every later log line for the
# rest of the process.
_active_lock = threading.RLock()
_active: weakref.WeakSet[Progress] = weakref.WeakSet()


@contextlib.contextmanager
def suspend_progress():
    """Erase any live progress bar for the duration of the block, then repaint.

    Used by the console log handler: per-repo status lines are written by worker
    threads while the main thread renders the bar, so clearing at the call site
    is not possible. Reentrant and a no-op when no bar is live.
    """
    with _active_lock:
        bars = list(_active)
        for p in bars:
            p.clear()
        try:
            yield
        finally:
            for p in bars:
                p.repaint()


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
        self._live = False  # is a frame currently painted on the terminal?
        try:
            self._tty = bool(self._stream.isatty())
        except Exception:  # noqa: BLE001 - a stream without isatty is non-tty
            self._tty = False
        if self._tty:
            with _active_lock:
                _active.add(self)

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
            with _active_lock:
                _active.discard(self)
            self.clear()
            if summary:
                self._stream.write(summary + "\n")
        else:
            self._stream.write((summary or self._line(now)) + "\n")
        self._stream.flush()

    def clear(self) -> None:
        """Erase the painted frame so another writer can use the line (TTY only)."""
        if not (self._tty and self._live):
            return
        self._write(_ERASE_LINE)
        self._live = False

    def repaint(self) -> None:
        """Redraw the frame erased by :meth:`clear` (TTY only, no-op if never painted)."""
        if not self._tty or self._live or self._first:
            return
        self._write_tty_frame(self._now())

    # -- internal ------------------------------------------------------

    def _write(self, text: str) -> None:
        """Write to the bar's stream, tolerating a stream that has gone away.

        The logging handler erases/repaints every live bar around each log line,
        so a stale or closed stream here must never take down the whole command.
        """
        try:
            self._stream.write(text)
            self._stream.flush()
        except Exception:  # noqa: BLE001 - a dead progress stream is never fatal
            self._tty = False

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
        # Erase-to-end-of-line rather than padding with spaces out to the terminal
        # width: padding left the cursor at the right margin, so the next stdout
        # line (per-repo status) started there and every frame stayed in scrollback.
        # _write flushes; a \r-terminated frame has no newline to flush on its own.
        self._write(_ERASE_LINE + self._line(now))
        self._live = True

    def _line(self, now: float) -> str:
        elapsed_seconds = now - self._start
        elapsed = _fmt_hms(elapsed_seconds)
        # Cumulative mean, not a trailing window: with a worker pool the gaps
        # between completions are extremely spiky (a burst of cached repos then a
        # slow fetch), and a short window turned that into an ETA swinging between
        # seconds and half an hour. The cumulative figure is self-smoothing and
        # converges monotonically.
        mean = (elapsed_seconds / self._count) if self._count else 0.0
        rate = (60.0 * self._count / elapsed_seconds) if elapsed_seconds > 0 else 0.0

        if self._total is None:
            head = f"{self._count} done"
            tail = f"{elapsed} elapsed · {rate:.1f}/min"
        else:
            total = self._total
            pct = round(100 * self._count / total) if total else 0
            bar_str = bar(self._count, total, self._BAR_WIDTH)
            remaining = max(total - self._count, 0)
            head = f"{bar_str} ({pct}%)"
            # One early sample is not an estimate: quoting an ETA off the first
            # completion produced confidently wrong numbers ("~36s left" for a
            # 10-minute run). Stay silent until there is enough signal.
            warm = self._count >= _ETA_MIN_ITEMS or elapsed_seconds >= _ETA_MIN_SECONDS
            if warm and mean > 0:
                tail = (f"{elapsed} elapsed · ~{_fmt_hms(remaining * mean)} left "
                        f"· {rate:.1f}/min")
            else:
                tail = f"{elapsed} elapsed · {rate:.1f}/min"

        prefix = f"{self._label} " if self._label else ""
        plain = f"{prefix}{head} · {tail}"

        width = terminal_width(self._stream)
        if visible_width(plain) > width:
            return plain[:width]

        colored_tail = dim(tail, stream=self._stream)
        return f"{prefix}{head} · {colored_tail}"
