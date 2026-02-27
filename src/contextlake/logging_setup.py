"""Logging setup for contextlake.

A single named logger ("contextlake") backs the ``log()`` helper used throughout
the package, so call sites stay simple while output routing (console verbosity
and an optional rotating audit file) is configured once in ``setup_logging()``.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import style

LOGGER_NAME = "contextlake"
_FORMAT = "[%(asctime)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_CLOCKFMT = "%H:%M:%S"  # terminal-only short clock, shown dim on the right edge


class _ConsoleFormatter(logging.Formatter):
    """Render log lines for an interactive terminal.

    On a TTY the message sits on the left and a dim ``HH:MM:SS`` clock is flushed
    to the right edge, re-flowed to the live terminal width and dropped when the
    line is too long to fit (never wraps). When the stream is *not* a TTY (pipe,
    redirect, cron) or the message spans multiple lines, it falls back to the
    classic ``[full-timestamp] message`` form so redirected/audit output keeps
    its timestamps. The log *file* always keeps the full prefix via ``_FORMAT``.
    """

    def __init__(self, handler):
        super().__init__(_FORMAT, datefmt=_DATEFMT)
        self._handler = handler

    def _is_tty(self):
        stream = getattr(self._handler, "stream", None)
        try:
            return stream, bool(stream and stream.isatty())
        except Exception:  # noqa: BLE001 - stream without isatty -> not a tty
            return stream, False

    def format(self, record):
        message = record.getMessage()
        stream, is_tty = self._is_tty()
        if not is_tty:
            return f"[{self.formatTime(record, _DATEFMT)}] {message}"
        if "\n" in message:
            # multi-line block: show it as-is, no per-line timestamp clutter
            return message
        clock = style.dim(self.formatTime(record, _CLOCKFMT), stream=stream)
        return style.align_right(message, clock, style.terminal_width(stream))


def get_logger():
    """Return the package logger (configured lazily with a console handler)."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        # Ensure log() works even if setup_logging() was never called (e.g. when
        # core functions are used as a library).
        setup_logging()
    return logger


def setup_logging(verbose=False, quiet=False, log_file=None):
    """Configure the package logger.

    verbose -> DEBUG console output, quiet -> WARNING and above, otherwise INFO.
    When ``log_file`` is given, a rotating file handler captures full DEBUG
    detail regardless of console verbosity (the real audit trail).
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

    # The console renders the clock on the right edge (TTY) or the classic prefix
    # (pipes/redirects); the file always keeps the full audit prefix.
    file_formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(_ConsoleFormatter(console))
    logger.addHandler(console)

    file_level = logging.DEBUG
    if log_file:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    # Logger threshold must be the most permissive of its handlers.
    logger.setLevel(min(console_level, file_level if log_file else console_level))
    return logger


def use_stderr():
    """Route console logging to stderr instead of stdout.

    Used when stdout is a machine-readable channel (e.g. the MCP stdio transport's
    JSON-RPC stream), so human-facing log lines never corrupt the protocol.
    """
    logger = get_logger()
    for handler in logger.handlers:
        if type(handler) is logging.StreamHandler:  # the console handler, not the file one
            handler.setStream(sys.stderr)


def log(message, level=logging.INFO):
    """Emit a timestamped message through the package logger."""
    get_logger().log(level, message)
