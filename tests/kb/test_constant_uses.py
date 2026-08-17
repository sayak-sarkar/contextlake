"""Where a constant is READ, one edge per occurrence.

The graph recorded that a constant existed and nothing about where its value was used, so
"what breaks if I change this timeout" could not be answered from the graph even though every
use is sitting in the AST. `extract_constant_uses` is the read side.

Every negative case here is one the implementation got wrong first, on a fixture small enough
to check by eye:

- **A declaration is not a use of itself.** The first draft reported every constant as read on
  the line that declares it, inflating each count by one and putting the declaration at the
  top of its own list of uses.
- **The exclusion has to be by FIELD, not by parent type.** Excluding every child of
  `assignment` also dropped the right-hand side, so `TOTAL = MAX_RETRY` stopped counting as a
  read of `MAX_RETRY`. Both errors were live simultaneously.
- **Node identity is `.id`, never `is`.** The tree-sitter bindings return a new Python wrapper
  per access, so the field comparison silently never matched and the whole guard was dead.
- **The declaring node can be a grandparent.** `const char *NAME = "x"` nests a
  `pointer_declarator`, so a one-level lookup misses it.
- **A write is not a read.** `TOTAL += 1` and `global TOTAL` name the binding; listing them
  under "where this value is used" reports a write as a read.

The source is the FILE, not the enclosing function, which is a deliberate design choice
documented on the extractor: a file and a line is the citation this was built to provide, and
reaching the enclosing definition's id would mean either widening `parse_source`'s return
tuple (which a dozen test modules unpack) or duplicating its qualifier logic.
"""

from __future__ import annotations

import pytest

from contextlake.kb.model import PER_SITE_RELATIONS
from contextlake.kb.parse import extract_constant_uses


def _reads(lang: str, source: bytes) -> list[tuple[str, int]]:
    """`[(name, line), ...]` sorted, for readable assertions."""
    return sorted(((name, line)
                   for _fid, name, _path, line in
                   extract_constant_uses("r", "a", source, lang)),
                  key=lambda t: (t[1], t[0]))


def _lines_for(lang: str, source: bytes, name: str) -> list[int]:
    return [line for got, line in _reads(lang, source) if got == name]


PY_MODULE = (
    b"MAX_RETRY = 3\n"            # 1: declaration
    b"TOTAL = MAX_RETRY\n"        # 2: declares TOTAL, READS MAX_RETRY
    b"def f():\n"                 # 3
    b"    return MAX_RETRY\n"     # 4: READS
    b"def g():\n"                 # 5
    b"    global TOTAL\n"         # 6: names a binding, not a read
    b"    TOTAL += 1\n"           # 7: a write
    b"    return TOTAL\n"         # 8: READS
)


def test_a_constant_is_read_where_it_is_used_and_not_where_it_is_declared():
    assert _lines_for("python", PY_MODULE, "MAX_RETRY") == [2, 4]


def test_the_right_hand_side_of_a_declaration_is_still_a_read():
    """`TOTAL = MAX_RETRY` declares one name and reads another, on one line.

    This is the case that fails if the exclusion is written as a parent-type blocklist
    instead of a field check, and it fails silently: the count simply comes out lower.
    """
    assert 2 in _lines_for("python", PY_MODULE, "MAX_RETRY")
    assert 2 not in _lines_for("python", PY_MODULE, "TOTAL")


def test_a_write_is_not_reported_as_a_read():
    """`global TOTAL` and `TOTAL += 1` name the binding. Only line 8 reads its value."""
    assert _lines_for("python", PY_MODULE, "TOTAL") == [8]


@pytest.mark.parametrize("lang,source,name,expected", [
    # The declared name is a grandchild of the declaring node, via a pointer declarator.
    ("c", b'const char *NAME = "x";\nint f(void) { return NAME[0]; }\n', "NAME", [2]),
    ("cpp", b"static const int MAX = 3;\nint g() { return MAX; }\n", "MAX", [2]),
    ("cpp", b"enum M { FAST = 1 };\nint h() { return FAST; }\n", "FAST", [2]),
    ("javascript", b"const A = 1;\nfunction f() { return A; }\n", "A", [2]),
    ("javascript", b"var p = 1, q = 2;\nfunction f() { return p + q; }\n", "p", [2]),
    ("typescript", b"const T: number = 3;\nexport function f() { return T; }\n", "T", [2]),
])
def test_declarations_are_excluded_across_grammars(lang, source, name, expected):
    assert _lines_for(lang, source, name) == expected


def test_every_occurrence_earns_its_own_entry():
    """Per-site, so "used in three places" is answerable rather than "used somewhere"."""
    source = (b"LIMIT = 5\n"
              b"def f():\n"
              b"    return LIMIT\n"
              b"def g():\n"
              b"    return LIMIT + LIMIT\n")
    assert _lines_for("python", source, "LIMIT") == [3, 5, 5]


def test_uses_is_registered_as_a_per_site_relation():
    """The storage rule lives in one shared constant, so producer and consumers agree.

    Without `uses` in this set, eleven reads in one file collapse to a single edge carrying
    one arbitrary line, and "where is this read" becomes unanswerable again.
    """
    assert "uses" in PER_SITE_RELATIONS
    # And the pre-existing member is untouched: widening this set is what would silently
    # turn every SQL and stylesheet reference into per-mention storage.
    assert "calls" in PER_SITE_RELATIONS
    assert "references" not in PER_SITE_RELATIONS


def test_an_attribute_is_not_a_bare_name_read():
    """`cfg.MAX_RETRY` reads an attribute of `cfg`, not a file-scope constant."""
    source = b"MAX_RETRY = 3\ndef f(cfg):\n    return cfg.MAX_RETRY\n"
    assert _lines_for("python", source, "MAX_RETRY") == []


def test_an_import_is_not_a_read():
    source = b"from mod import MAX_RETRY\ndef f():\n    return MAX_RETRY\n"
    assert _lines_for("python", source, "MAX_RETRY") == [3]


def test_an_unparseable_or_unknown_language_yields_nothing_rather_than_raising():
    """One bad file must not stop the walk over a repository."""
    assert extract_constant_uses("r", "a.xyz", b"\x00\xff not source", "nosuchlang") == []


def test_a_function_like_macro_is_not_a_use_of_itself():
    """`#define NAME(a, b) ...` is a DIFFERENT node type from `#define NAME 5`.

    Object-like macros are `preproc_def`; function-like ones are `preproc_function_def`, and
    handling only the first counted every function-like macro's definition as a read of itself.
    Caught on a real C++ tree, where the impact walk for a test-assertion macro listed the
    header that defines it, citing the `#define` line as the use.

    The parameter names are not reads either: `a` and `b` name parameters, and they were being
    emitted as reads of whatever else in the repository happens to be called `a` or `b`.
    """
    source = (b"#define CMP(a, b) ((a) == (b))\n"
              b"#define LIMIT 5\n"
              b"int f() { return CMP(LIMIT, 5); }\n")
    assert _lines_for("cpp", source, "CMP") == [3]
    assert _lines_for("cpp", source, "LIMIT") == [3]
    assert _lines_for("cpp", source, "a") == []
    assert _lines_for("cpp", source, "b") == []
