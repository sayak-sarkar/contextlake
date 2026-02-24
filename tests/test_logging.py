"""Tests for logging routing (stdout vs stderr)."""

import logging
import sys

from contextlake import logging_setup


def _console_handler():
    logger = logging.getLogger(logging_setup.LOGGER_NAME)
    return next(h for h in logger.handlers if type(h) is logging.StreamHandler)


def test_use_stderr_routes_console_off_stdout():
    try:
        logging_setup.setup_logging()
        assert _console_handler().stream is sys.stdout  # default: human output on stdout

        logging_setup.use_stderr()
        assert _console_handler().stream is sys.stderr  # protocol-safe: logs off stdout
    finally:
        logging_setup.setup_logging()  # restore default for other tests
