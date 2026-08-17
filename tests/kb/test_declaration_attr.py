"""A constant records what it is SET TO, not merely that it exists.

Until this, the graph knew a constant's name, kind and location and nothing about its value,
so "what is the retry limit" was unanswerable from the graph even though the answer sits on
the same line as the name. `attrs["declaration"]` closes that.

Every expectation below was measured against the real grammars before the extractor was
written, and the cases exist because the obvious implementation fails them:

**The enclosing statement is shared.** `var a = 1, b = 2;` yields two names whose container is
the same statement, so storing the container would report `b` as declared `var a = 1, b = 2`.
True of the statement, misleading about `b`. The extractor takes the narrowest per-name node.

**A declarator is not always the parent.** `const char *NAME = "svc";` nests a
`pointer_declarator` between the identifier and the `init_declarator`, so a parent check misses
it and silently falls back to the shared statement. The walk has to go up until it finds one.

**Some kinds have no declarator, correctly.** A `#define` names exactly one macro, so its
container already is per-name and the fallback is the right answer rather than a miss.

The field is called `declaration` and never `value`: it is the text as written, and no value has
been parsed out of it.
"""

from __future__ import annotations

import pytest

from contextlake.kb.parse import (
    DECLARED_VALUE_KINDS,
    MAX_DECLARATION_CHARS,
    _declaration_text,
    _member_symbols,
    _parser,
)


def _declarations(lang: str, source: bytes) -> dict[str, str]:
    """`{name: declaration}` for every member symbol the parser finds."""
    tree = _parser(lang).parse(source)
    return {nn.text.decode(): _declaration_text(nn, cont)
            for _kind, nn, cont in _member_symbols(tree, lang)}


@pytest.mark.parametrize("lang,source,expected", [
    ("python", b"MAX_RETRY = 3\n", {"MAX_RETRY": "MAX_RETRY = 3"}),
    ("python", b'NAME: str = "svc"\n', {"NAME": 'NAME: str = "svc"'}),
    ("javascript", b"const MAX_RETRY = 3;\n", {"MAX_RETRY": "MAX_RETRY = 3"}),
    ("typescript", b"export const T: number = 3;\n", {"T": "T: number = 3"}),
    ("cpp", b"static const int MAX = 3;\n", {"MAX": "MAX = 3"}),
    ("cpp", b"enum M { FAST = 1, SLOW = 2 };\n",
     {"FAST": "FAST = 1", "SLOW": "SLOW = 2"}),
])
def test_the_declaration_carries_the_value(lang, source, expected):
    got = _declarations(lang, source)
    for name, want in expected.items():
        assert got.get(name) == want, f"{name}: {got.get(name)!r} != {want!r}"


def test_a_shared_statement_does_not_leak_between_names():
    """The case that makes the narrowing necessary rather than tidy.

    Both names come back with the same container, so a container-based implementation gives
    them identical declarations and reports each as the other's.
    """
    got = _declarations("javascript", b"var a = 1, b = 2;\n")
    assert got == {"a": "a = 1", "b": "b = 2"}
    # Stated as its own assertion because it is the actual defect: not "the text is wrong"
    # but "the two names are indistinguishable".
    assert got["a"] != got["b"]


def test_a_pointer_declarator_between_name_and_declarator_is_still_found():
    """`const char *NAME` needs an upward WALK; the declarator is a grandparent here.

    The leading `*` is kept deliberately. It is the declarator exactly as written, and
    trimming it to look tidier would be this module inventing a cleaner source line than the
    one in the file. A reader who wants the type opens the cited file and line.
    """
    got = _declarations("c", b'const char *NAME = "svc";\n')
    assert got["NAME"] == '*NAME = "svc"'
    # The failure mode being excluded: falling back to the whole statement.
    assert "const char" not in got["NAME"]


def test_a_macro_falls_back_to_its_container_and_that_is_correct():
    """No declarator node exists, and none is needed: one `#define`, one name."""
    got = _declarations("cpp", b"#define TIMEOUT 30\n")
    assert got["TIMEOUT"] == "#define TIMEOUT 30"


def test_a_long_declaration_is_truncated_and_says_so():
    """A generated table is one enormous declaration; no page is improved by all of it."""
    got = _declarations("python", b"BIG = [" + b"1, " * 200 + b"]\n")["BIG"]
    assert got.startswith("BIG = [1,")
    assert got.endswith("[truncated]")
    assert len(got) <= MAX_DECLARATION_CHARS + len(" [truncated]")


def test_a_short_declaration_is_not_marked_truncated():
    """The marker is a claim about this line, so it must not appear on an untouched one.

    Without this, an off-by-one at the boundary would label every declaration truncated and
    the test above would still pass.
    """
    exact = "X = " + "9" * (MAX_DECLARATION_CHARS - 4)
    got = _declarations("python", exact.encode() + b"\n")["X"]
    assert got == exact
    assert "truncated" not in got


def test_multiline_declarations_are_collapsed_to_one_line():
    """A declaration spanning lines would otherwise break every table it is rendered into."""
    got = _declarations("python", b'MULTI = {\n  "a": 1,\n  "b": 2,\n}\n')["MULTI"]
    assert "\n" not in got
    assert got == 'MULTI = { "a": 1, "b": 2, }'


def test_only_the_kinds_whose_meaning_is_their_value_carry_a_declaration():
    """A function is described by its signature, which `_doc_sig` already supplies.

    Storing a declaration for every kind would duplicate that field and grow every shard for
    nothing, so the set is explicit rather than "whatever `_member_symbols` returns".
    """
    assert DECLARED_VALUE_KINDS == {"global_variable", "enum_constant", "macro", "field"}
    assert "function" not in DECLARED_VALUE_KINDS
    assert "class" not in DECLARED_VALUE_KINDS
