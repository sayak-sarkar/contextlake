"""Console output survives a stream that cannot encode its glyphs.

A pipe on Windows defaults to cp1252, which has none of the status glyphs (✓ ⚠ ✗) nor the
arrows the messages use. Every such line raised UnicodeEncodeError inside logging: the line was
lost and a "--- Logging error ---" traceback printed in its place. Found by the stability v2
Windows cell, in `contextlake init` output.
"""

from __future__ import annotations

import io
import logging

from contextlake import style
from contextlake.logging_setup import _ConsoleHandler


def _cp1252():
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp1252", newline="\n", write_through=True)


def test_a_glyph_line_reaches_a_cp1252_console_in_ascii(capsys):
    raw, stream = _cp1252()
    logger = logging.getLogger("contextlake.test_console_encoding")
    logger.propagate = False
    handler = _ConsoleHandler(stream)
    logger.addHandler(handler)
    try:
        logger.warning("%s Wrote the config -> kb.toml", style.ok())
        logger.warning("%s nothing to do", style.warn())
    finally:
        logger.removeHandler(handler)
    out = raw.getvalue().decode("cp1252")
    assert "OK Wrote the config -> kb.toml" in out
    assert "! nothing to do" in out
    assert "Logging error" not in capsys.readouterr().err


def test_an_unmapped_character_becomes_a_question_mark():
    raw, stream = _cp1252()
    style.write_safely(stream, "repo 漢 indexed\n")
    assert raw.getvalue().decode("cp1252") == "repo ? indexed\n"


def test_a_utf8_stream_keeps_every_glyph():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8", newline="\n", write_through=True)
    style.write_safely(stream, "✓ done → x\n")
    assert raw.getvalue().decode("utf-8") == "✓ done → x\n"
