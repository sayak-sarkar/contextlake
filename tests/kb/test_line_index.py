"""Tests for kb/_lines.py: offset to line number, equal to counting from the top."""

import pytest

from contextlake.kb import _lines
from contextlake.kb._lines import LineIndex

_TEXTS = [
    "",
    "\n",
    "no newline at all",
    "one\n",
    "one\ntwo",
    "a\n\n\nb\n",
    "crlf\r\nlines\r\nhere\r\n",
    "lone\rcarriage\rreturns\n",
    "\n\nstarts with blanks",
    "tabs\there\n\tindented\n",
    "unicode é中文\nsecond \U0001F600 line\n",
]


@pytest.mark.parametrize("text", _TEXTS)
def test_line_of_equals_count_from_zero_at_every_offset(text):
    index = LineIndex(text)
    # `len(text)` included: a match can end at the end of the file, and a lookup at
    # that offset must not raise or drift.
    for pos in range(len(text) + 1):
        assert index.line_of(pos) == text.count("\n", 0, pos) + 1, (text, pos)


def test_an_offset_on_a_newline_belongs_to_the_line_that_newline_ends():
    text = "ab\ncd\n"
    index = LineIndex(text)
    assert index.line_of(2) == 1      # the first "\n" itself
    assert index.line_of(3) == 2      # "c"
    assert index.line_of(5) == 2      # the second "\n"
    assert index.line_of(6) == 3      # one past the end


def test_lookups_in_any_order_agree():
    text = "".join(f"line {i}\n" for i in range(200))
    index = LineIndex(text)
    positions = [len(text) - 1, 0, 57, 3, 190, 57, 1000, 12]
    for pos in positions:
        assert index.line_of(pos) == text.count("\n", 0, pos) + 1


def test_the_table_is_built_once_and_only_when_asked(monkeypatch):
    built = []
    real = _lines._newline_offsets

    def spy(text):
        built.append(len(text))
        return real(text)

    monkeypatch.setattr(_lines, "_newline_offsets", spy)
    index = LineIndex("a\nb\nc\n")
    assert built == []                 # a file with no matches pays nothing
    for pos in range(6):
        index.line_of(pos)
    assert built == [6]                # one build serves every lookup
