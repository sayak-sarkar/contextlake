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


def _record(msg, inline=False):
    record = logging.LogRecord("contextlake", logging.INFO, "", 0, msg, None, None)
    record.inline = inline
    return record


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


def test_console_inline_line_has_no_clock(monkeypatch):
    # A short per-item detail line still fits a clock -- but ``inline=True``
    # (the hot per-repo/per-item loops) must suppress it outright so the
    # clock column never flickers on/off across hundreds of such lines.
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("NO_COLOR", "1")
    handler = logging.StreamHandler(_Tty())
    fmt = logging_setup._ConsoleFormatter(handler)
    msg = "[1/500] * repo: ok"
    assert fmt.format(_record(msg, inline=True)) == msg


def test_console_non_inline_short_line_still_gets_clock(monkeypatch):
    # Sanity check alongside the above: the same-length line without the
    # ``inline`` marker keeps getting the right-aligned clock (section/
    # summary lines are low-frequency, so the clock doesn't flicker there).
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("NO_COLOR", "1")
    handler = logging.StreamHandler(_Tty())
    fmt = logging_setup._ConsoleFormatter(handler)
    msg = "[1/500] * repo: ok"
    out = fmt.format(_record(msg, inline=False))
    assert out != msg
    assert out.startswith(msg)
    assert style.visible_width(out) == 40


def test_log_helper_marks_record_inline(gls_logs):
    # Look up records by message rather than fixed indices: gls_logs attaches
    # its handler directly to the "contextlake" logger, and until setup_logging()
    # has run at least once in this process the logger's default propagate=True
    # also lets pytest's own root-level capture see the same records, so exact
    # position/count isn't guaranteed -- content per message is.
    logging_setup.log("detail line", inline=True)
    logging_setup.log("section line")
    detail = next(r for r in gls_logs.records if r.getMessage() == "detail line")
    section = next(r for r in gls_logs.records if r.getMessage() == "section line")
    assert detail.inline is True
    assert getattr(section, "inline", False) is False


def test_use_stderr_routes_console_off_stdout():
    try:
        logging_setup.setup_logging()
        assert _console_handler().stream is sys.stdout  # default: human output on stdout

        logging_setup.use_stderr()
        assert _console_handler().stream is sys.stderr  # protocol-safe: logs off stdout
    finally:
        logging_setup.setup_logging()  # restore default for other tests
