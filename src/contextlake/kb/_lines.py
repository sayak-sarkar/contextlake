"""Character offset to line number, without counting from the top of the file each time.

``text.count("\\n", 0, pos)`` reads ``pos`` characters. Called once per match it costs
(matches x file size): a 2.2 MB DDL file with 20,000 tables took 8.7 s in ``kb/sql.py``,
and the same shape sat in every extractor under ``kb/flow/``. :mod:`.xml_cfg` fixed it with
a running counter, which only works when positions move forward. The extractors here run
several ``finditer`` passes over one text, so positions restart. One table of newline
offsets plus a binary search serves any order of lookups.
"""

from __future__ import annotations

from bisect import bisect_left


def _newline_offsets(text: str) -> list[int]:
    out: list[int] = []
    find = text.find
    i = find("\n")
    while i != -1:
        out.append(i)
        i = find("\n", i + 1)
    return out


class LineIndex:
    """Maps an offset in ``text`` to its 1-based line number.

    ``line_of(pos)`` equals ``text.count("\\n", 0, pos) + 1`` for every ``pos >= 0``,
    including a ``pos`` that sits on a ``\\n`` (that character belongs to the line it
    ends). Build it from the exact string the offsets index into: the masked copy, not the
    raw text, when a comment-blanking pass runs first.

    The table is built on the first lookup, so a file with no matches pays nothing.
    """

    __slots__ = ("_text", "_newlines")

    def __init__(self, text: str) -> None:
        self._text = text
        self._newlines: list[int] | None = None

    def line_of(self, pos: int) -> int:
        newlines = self._newlines
        if newlines is None:
            newlines = self._newlines = _newline_offsets(self._text)
        return bisect_left(newlines, pos) + 1
