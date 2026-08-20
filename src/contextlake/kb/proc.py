"""Pro*C (``.pc``) support: keep the embedded SQL out of the C parse, and only there.

A Pro*C file is C with ``EXEC SQL`` statements written directly into the source, ahead of a
precompiler that turns them into library calls. Routed at the C grammar as-is it parses, but
it parses the SQL too, and tree-sitter reads those statements as declarations. Measured on a
short realistic file: ``EXEC SQL INCLUDE SQLCA;`` and ``EXEC SQL BEGIN DECLARE SECTION;``
produced ``global_variable`` nodes named ``SQLCA``, ``SQL`` and ``SECTION`` -- names of
things that do not exist, in a kind a bare identifier elsewhere in the repository resolves
``uses`` edges onto. Confidently wrong, which is the one outcome this project does not ship.

So the C parse gets the statements blanked out, and **only the C parse**. The dataflow pass
reads the original bytes, because the SQL is the entire point of the file: which tables it
reads and writes is the most useful thing a Pro*C file has to say. That pass already
normalises table names through :func:`..sql._norm_name`, which is the same recipe
:mod:`.sql` gives its ``table`` nodes, so an ``EXEC SQL`` reference and the ``CREATE TABLE``
in another file land on one node without a second copy of the rule existing anywhere.
"""

from __future__ import annotations

import re

# `EXEC SQL`, and the two other statement introducers Pro*C accepts. `EXEC ORACLE` carries
# precompiler options and `EXEC TOOLS` the Forms interface; both are as foreign to the C
# grammar as the SQL is.
_EXEC = re.compile(r"\bEXEC\s+(SQL|ORACLE|TOOLS)\b", re.IGNORECASE)

# An embedded PL/SQL block is introduced by `EXEC SQL EXECUTE` and closed by `END-EXEC;`,
# and its body contains semicolons of its own. Terminating that one at the first `;` would
# leave the block's remaining statements in the C parse, which is the case this exists for.
_EXECUTE = re.compile(r"\bEXEC\s+SQL\s+EXECUTE\b", re.IGNORECASE)
_END_EXEC = re.compile(r"\bEND-EXEC\s*;", re.IGNORECASE)


def mask_embedded_sql(source: bytes) -> bytes:
    """Blank every ``EXEC SQL`` statement, preserving length and every newline.

    Length and newlines are preserved rather than the statements deleted because the C
    parse's line numbers are the provenance every node it produces will cite; a delete
    would shift every function below the first statement in the file.
    """
    text = source.decode("utf-8", "replace") if isinstance(source, (bytes, bytearray)) \
        else source
    out = list(text)
    pos = 0
    while True:
        m = _EXEC.search(text, pos)
        if not m:
            break
        block = _EXECUTE.match(text, m.start())
        if block:
            closer = _END_EXEC.search(text, m.end())
            end = closer.end() if closer else len(text)
        else:
            semi = text.find(";", m.end())
            # An unterminated statement runs to end of file. That is malformed input, and
            # blanking the remainder is the safe reading: the alternative feeds a partial
            # SQL statement to the C grammar, which is what this function exists to prevent.
            end = len(text) if semi < 0 else semi + 1
        for i in range(m.start(), end):
            if out[i] != "\n":
                out[i] = " "
        pos = end
    return "".join(out).encode("utf-8")
