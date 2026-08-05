"""Logging setup for contextlake.

A single named logger ("contextlake") backs the ``log()`` helper used throughout
the package, so call sites stay simple while output routing (console verbosity,
an optional rotating audit file, the human/JSON line format, and redaction) is
configured once in ``setup_logging()``.

Two audiences, one logger. The default human format is unchanged and is what a
person at a terminal reads. ``--log-format json`` swaps in one JSON object per
line -- timestamp, level, message, run id, command, plus whatever structured
fields the call site attached -- for the unattended case (the systemd unit in
``examples/``), where the reader is a log shipper rather than a person.
"""

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

from . import observability, style

LOGGER_NAME = "contextlake"
_FORMAT = "[%(asctime)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_JSON_DATEFMT = "%Y-%m-%dT%H:%M:%S"
_CLOCKFMT = "%H:%M:%S"  # terminal-only short clock, shown dim on the right edge

TEXT, JSON = "text", "json"
LOG_FORMATS = (TEXT, JSON)


class _ConsoleFormatter(logging.Formatter):
    """Render log lines for an interactive terminal.

    On a TTY the message sits on the left and a dim ``HH:MM:SS`` clock is flushed
    to the right edge, re-flowed to the live terminal width and dropped when the
    line is too long to fit (never wraps). When the stream is *not* a TTY (pipe,
    redirect, cron) or the message spans multiple lines, it falls back to the
    classic ``[full-timestamp] message`` form so redirected/audit output keeps
    its timestamps. The log *file* always keeps the full prefix via ``_FORMAT``.

    Records marked ``inline`` (via ``log(..., inline=True)``) never get the
    clock either, regardless of how much room they'd have: those are the
    hot per-item loop lines (per-repo mirror/index/embed/wiki progress), and
    whether such a line is short enough to fit a clock varies line to line
    with e.g. path length -- so leaving the width check in play would just
    make the clock flicker on/off across the fleet instead of disappearing.
    A live Progress bar already shows elapsed time while those lines print.
    """

    def __init__(self, handler, *, redact=False):
        super().__init__(_FORMAT, datefmt=_DATEFMT)
        self._handler = handler
        self._redact = redact

    def _is_tty(self):
        stream = getattr(self._handler, "stream", None)
        try:
            return stream, bool(stream and stream.isatty())
        except Exception:  # noqa: BLE001 - stream without isatty -> not a tty
            return stream, False

    def format(self, record):
        message = record.getMessage()
        if self._redact:
            message = observability.redact(message)
        stream, is_tty = self._is_tty()
        if not is_tty:
            return f"[{self.formatTime(record, _DATEFMT)}] {message}"
        if "\n" in message:
            # multi-line block: show it as-is, no per-line timestamp clutter
            return message
        if getattr(record, "inline", False):
            # hot per-item loop line: skip the clock outright (see class docstring)
            return message
        clock = style.dim(self.formatTime(record, _CLOCKFMT), stream=stream)
        return style.align_right(message, clock, style.terminal_width(stream))


class _FileFormatter(logging.Formatter):
    """The audit file's classic ``[full-timestamp] message`` form, optionally
    redacted. Redaction lives here rather than in a ``logging.Filter`` for the
    same reason it does in every formatter below: a filter mutates the shared
    ``LogRecord``, so whether the console also came out scrubbed would depend on
    handler order. A formatter only ever rewrites its own handler's output."""

    def __init__(self, *, redact=False):
        super().__init__(_FORMAT, datefmt=_DATEFMT)
        self._redact = redact

    def format(self, record):
        text = super().format(record)
        return observability.redact(text) if self._redact else text


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for shipping rather than reading.

    Every line carries the run id and command so an interleaved journal (the
    systemd timer's, say) can be split back into runs, and any structured fields
    the call site attached via ``log(..., repo=..., duration_ms=...)`` ride
    alongside in a flat namespace. Fields are kept in their own dict on the
    record, never set as record attributes, so a field called ``message`` or
    ``name`` cannot collide with ``LogRecord``'s own reserved attributes.
    """

    # ``logging.Formatter`` resolves timestamps through ``self.converter``, which
    # defaults to ``time.localtime``. The human format makes no timezone claim,
    # so local is right there -- but this one appends "Z", and a line that says
    # UTC while carrying the host's wall clock silently shifts every timestamp a
    # collector ingests by that host's offset. UTC is also simply the right
    # answer for output meant to be correlated across machines.
    converter = time.gmtime

    def __init__(self, *, redact=False):
        super().__init__(datefmt=_JSON_DATEFMT)
        self._redact = redact

    def _clean(self, value):
        if self._redact and isinstance(value, str):
            return observability.redact(value)
        return value

    def format(self, record):
        payload = {
            "ts": self.formatTime(record, _JSON_DATEFMT) + "Z",
            "level": record.levelname,
            "msg": self._clean(record.getMessage()),
        }
        run = observability.run_id()
        if run:
            payload["run_id"] = run
        cmd = observability.command()
        if cmd:
            payload["command"] = cmd
        for key, value in (getattr(record, "fields", None) or {}).items():
            payload[key] = self._clean(value)
        if record.exc_info and "error_type" not in payload:
            exc = record.exc_info[1]
            payload["error_type"] = type(exc).__name__
            payload["error"] = self._clean(str(exc))
        # default=str so an unexpected value (a Path, a datetime) degrades to its
        # string form instead of taking down the run it was supposed to describe.
        return json.dumps(payload, default=str)


def get_logger():
    """Return the package logger (configured lazily with a console handler)."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        # Ensure log() works even if setup_logging() was never called (e.g. when
        # core functions are used as a library).
        setup_logging()
    return logger


class _ConsoleHandler(logging.StreamHandler):
    """Console handler that yields the terminal line to any live progress bar.

    The bar renders on stderr while these lines go to stdout, but both share one
    terminal cursor: without this, a painted frame stayed on screen and the next
    log line was appended to its right edge. Erasing around the write keeps the
    bar a single live line at the bottom. Also flushes the underlying stream
    before releasing, so the repaint cannot land ahead of the line it follows.
    """

    def emit(self, record):
        with style.suspend_progress():
            super().emit(record)
            try:
                self.flush()
            except Exception:  # noqa: BLE001 - flush failures are never fatal here
                pass


def setup_logging(verbose=False, quiet=False, log_file=None, *,
                  log_format=TEXT, redact=None):
    """Configure the package logger.

    verbose -> DEBUG console output, quiet -> WARNING and above, otherwise INFO.
    When ``log_file`` is given, a rotating file handler captures full DEBUG
    detail regardless of console verbosity.

    ``log_format`` is ``"text"`` (the unchanged human rendering) or ``"json"``.

    ``redact`` decides whether output goes through
    :func:`observability.redact`. ``None`` -- the default -- means *file yes,
    console no*, which follows from what each stream is for: the console is
    yours and you need the real paths on it to act on what you are reading,
    while the file is the artifact that gets attached to a bug report. ``True``
    scrubs both, ``False`` neither. Redaction is a no-op until
    :func:`observability.configure_redaction` has registered what to hide, so
    the file stays fully readable for anything the CLI could not identify as
    workspace- or group-derived.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False

    if verbose:
        console_level = logging.DEBUG
    elif quiet:
        console_level = logging.WARNING
    else:
        console_level = logging.INFO

    global _console_redact
    console_redact = _console_redact = bool(redact)
    file_redact = True if redact is None else bool(redact)

    def formatter(handler, redacting):
        if log_format == JSON:
            return _JsonFormatter(redact=redacting)
        # The console renders the clock on the right edge (TTY) or the classic
        # prefix (pipes/redirects); the file always keeps the full audit prefix.
        if handler is None:
            return _FileFormatter(redact=redacting)
        return _ConsoleFormatter(handler, redact=redacting)

    console = _ConsoleHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(formatter(console, console_redact))
    logger.addHandler(console)

    file_level = logging.DEBUG
    if log_file:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter(None, file_redact))
        logger.addHandler(file_handler)

    # Logger threshold must be the most permissive of its handlers.
    logger.setLevel(min(console_level, file_level if log_file else console_level))
    return logger


# Whether the console is scrubbing, as decided by the last setup_logging call.
# Exposed for the one command that legitimately writes to stdout itself.
_console_redact = False


def console_redacting() -> bool:
    """Is console output being scrubbed right now?

    ``doctor`` renders an aligned report with ``print`` rather than through the
    logger, on purpose: the console formatter appends a right-edge clock to every
    single-line record, which is right for a progress stream and wrong for a
    report you read as a block. That choice also put doctor outside redaction
    entirely, so ``--redact doctor`` printed the store path in full -- on the one
    command whose whole output is what you paste into a bug report.

    Rather than give doctor a second opinion about the flag, it asks the module
    that already decided.
    """
    return _console_redact


def report_line(text: str = "") -> None:
    """Emit one line of a command's own rendered report: to the console as the
    command composed it, and to the audit file alongside every other log line.

    ``doctor`` renders an aligned block with ``print`` rather than through
    :func:`log`, for the reason above. The cost was that its entire output
    missed ``--log-file``: measured at zero lines, on the one command whose
    output is what you attach to a bug report.

    So the two halves are split rather than merged. The console half stays the
    caller's own rendering, with the console's redaction decision applied here
    so no caller has to remember it. The file half goes straight to the file
    handler, which formats and redacts it exactly like every other line in that
    file. Handing the line to the *logger* instead would print it twice, and
    would let ``--quiet`` silence the whole output of a command whose only job
    is to produce it.
    """
    print(observability.redact(text) if _console_redact else text)
    # No get_logger() here: that would configure a console handler on a path
    # that has already done its own printing. With no setup_logging call there
    # is no file handler to write to, and nothing is owed.
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.handle(logging.getLogger(LOGGER_NAME).makeRecord(
                LOGGER_NAME, logging.INFO, "(report)", 0, text, None, None))


def use_stderr():
    """Route console logging to stderr instead of stdout.

    Used when stdout is a machine-readable channel (e.g. the MCP stdio transport's
    JSON-RPC stream), so human-facing log lines never corrupt the protocol.
    """
    logger = get_logger()
    for handler in logger.handlers:
        if type(handler) is _ConsoleHandler:  # the console handler, not the file one
            handler.setStream(sys.stderr)


def log(message, level=logging.INFO, *, inline=False, exc_info=False, **fields):
    """Emit a timestamped message through the package logger.

    ``inline=True`` marks a high-frequency per-item detail line (per-repo
    mirror/index/embed/wiki loop output) so the TTY console formatter skips
    its right-aligned clock -- see ``_ConsoleFormatter`` for why. Low-frequency
    section/summary lines should leave this at the default ``False``.

    Any keyword arguments beyond those are **structured fields** (``repo=``,
    ``duration_ms=``, ``error_type=``, ...). They are carried on the record for
    the JSON formatter to emit and are deliberately invisible in the human
    format: the human line is composed for reading and already says what it
    needs to, so a call site can add machine-readable detail without changing a
    single character of what a person sees.
    """
    get_logger().log(level, message, exc_info=exc_info,
                     extra={"inline": inline, "fields": fields})
