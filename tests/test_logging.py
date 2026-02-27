"""Tests for logging routing (stdout vs stderr) and console formatting."""

import io
import logging
import sys

from contextlake import logging_setup, style


def _console_handler():
    logger = logging.getLogger(logging_setup.LOGGER_NAME)
    return next(h for h in logger.handlers if type(h) is logging.StreamHandler)


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _record(msg):
    return logging.LogRecord("contextlake", logging.INFO, "", 0, msg, None, None)


def test_console_tty_right_aligns_clock(monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("NO_COLOR", "1")  # keep the clock plain so we can assert columns
    handler = logging.StreamHandler(_Tty())
    fmt = logging_setup._ConsoleFormatter(handler)
    out = fmt.format(_record("hello"))
    # message on the left, clock flush to the right edge (col 40)
    assert out.startswith("hello")
    assert style.visible_width(out) == 40
    assert out[-8:].count(":") == 2  # ends with HH:MM:SS


def test_console_drops_clock_when_line_too_long(monkeypatch):
    monkeypatch.setenv("COLUMNS", "20")
    monkeypatch.setenv("NO_COLOR", "1")
    handler = logging.StreamHandler(_Tty())
    fmt = logging_setup._ConsoleFormatter(handler)
    msg = "a-very-long-message-that-cannot-fit-a-clock"
    assert fmt.format(_record(msg)) == msg  # unchanged, no wrap


def test_console_pipe_falls_back_to_prefix(monkeypatch):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    handler = logging.StreamHandler(io.StringIO())  # not a tty
    fmt = logging_setup._ConsoleFormatter(handler)
    out = fmt.format(_record("piped"))
    assert out.endswith("] piped") and out.startswith("[")  # classic audit prefix


def test_console_multiline_has_no_timestamp(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    handler = logging.StreamHandler(_Tty())
    fmt = logging_setup._ConsoleFormatter(handler)
    msg = "line one\nline two"
    assert fmt.format(_record(msg)) == msg


def test_use_stderr_routes_console_off_stdout():
    try:
        logging_setup.setup_logging()
        assert _console_handler().stream is sys.stdout  # default: human output on stdout

        logging_setup.use_stderr()
        assert _console_handler().stream is sys.stderr  # protocol-safe: logs off stdout
    finally:
        logging_setup.setup_logging()  # restore default for other tests
