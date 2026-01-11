"""Logging setup for gitlab_sync.

A single named logger ("gitlab_sync") backs the ``log()`` helper used throughout
the package, so call sites stay simple while output routing (console verbosity
and an optional rotating audit file) is configured once in ``setup_logging()``.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "gitlab_sync"
_FORMAT = "[%(asctime)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


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

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_level = logging.DEBUG
    if log_file:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # Logger threshold must be the most permissive of its handlers.
    logger.setLevel(min(console_level, file_level if log_file else console_level))
    return logger


def log(message, level=logging.INFO):
    """Emit a timestamped message through the package logger."""
    get_logger().log(level, message)
