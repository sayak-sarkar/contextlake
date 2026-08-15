"""Every language the parser claims to support must have a grammar it can load.

`_GRAMMARS` replaced a 14-branch if/elif chain of lazy imports. A table makes adding a
language one row, and it makes this test possible: the chain's failure mode was a language
reachable through `LANG_BY_EXT` with no branch to match it, which raised
`ValueError: unsupported language` at parse time on a user's repository rather than here.

These assertions are cheap and they are the ones that go wrong when a language is added in
a hurry: an extension mapped to a language nobody wrote a grammar row for, or a grammar row
whose package name is right and whose factory name is not.
"""

from __future__ import annotations

import pytest

from contextlake.kb import parse as P

LANGS = sorted(set(P._DEF_TYPES) | set(P.LANG_BY_EXT.values()))


def test_the_fixture_sees_the_languages_it_claims_to():
    """Guards the two collections this file reads. If either is renamed or emptied, every
    parametrised test below silently becomes zero test cases and this file passes while
    checking nothing."""
    assert len(LANGS) >= 14, f"only {len(LANGS)} languages found: {LANGS}"
    for expected in ("python", "javascript", "typescript", "c", "cpp"):
        assert expected in LANGS, f"{expected} vanished from the language set"


@pytest.mark.parametrize("lang", LANGS)
def test_every_supported_language_has_a_grammar_row(lang):
    assert lang in P._GRAMMARS, (
        f"{lang} is reachable through LANG_BY_EXT or _DEF_TYPES but has no _GRAMMARS "
        f"row, so parsing a file of that type raises at index time on a user's repo")


@pytest.mark.parametrize("lang", LANGS)
def test_every_grammar_row_actually_loads(lang):
    """The package name and the factory name are both easy to get subtly wrong, and three
    of them are irregular already (`tree_sitter_c_sharp`, typescript's two entry points,
    php's `language_php`). Loading is the only check that catches a wrong factory."""
    assert P._language(lang) is not None


def test_an_unknown_language_is_refused_by_name():
    with pytest.raises(ValueError, match="unsupported language"):
        P._language("cobol")
