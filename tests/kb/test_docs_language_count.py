"""The language count in the docs must equal the language count in the code.

Every page quoting "N languages" is a claim about what the parser does, and the parser is
the only thing that knows. This number has been written by hand in three places and has now
been wrong once: adding five grammars moved it from 14 to 19, and nothing but a reader would
have noticed.

The count is derived here from `ALL_LANGS` rather than restated, so this file cannot drift
in the same direction as the pages it checks. `docs/style-guide-reference.md` names the exact
phrasing to use, and is checked too, because that page is what the others copy.

`ALL_LANGS`, not `LANG_BY_EXT`: a language whose files are found by NAME has no extension
entry, so counting the extension table alone under-reports the total while looking exactly
like a complete count.

Lives under `tests/kb/` because deriving the count means importing the parser, and the core
CI job runs `pytest --ignore=tests/kb` against an install without it. Placed at the top level
first, and the core-tier guard caught it, which is the second time that trap has been walked
into in one session.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from contextlake.kb.parse import _GRAMMARS, ALL_LANGS

REPO = Path(__file__).resolve().parents[2]

LANGUAGES = len(ALL_LANGS)
# TypeScript and TSX share one grammar package, so grammars are counted by module.
GRAMMARS = len({module for module, _factory in _GRAMMARS.values()})

# Each page and the phrase it must contain.
CLAIMS = [
    ("README.md", f"**{LANGUAGES} languages**"),
    ("docs/indexing-the-code-graph.md", f"**{LANGUAGES} languages across {GRAMMARS} grammars**"),
    ("docs/style-guide-reference.md",
     f'"{LANGUAGES} languages across {GRAMMARS} tree-sitter grammars"'),
]


def test_the_counts_are_plausible():
    """Guards the two derived numbers. If `ALL_LANGS` were renamed or emptied, every
    assertion below would compare against 0 and could pass against a doc that says
    nothing, which is the vacuous-pass shape these tests exist to avoid."""
    assert LANGUAGES >= 14, f"only {LANGUAGES} languages derived; the source moved"
    assert GRAMMARS >= 13, f"only {GRAMMARS} grammars derived; the source moved"
    assert GRAMMARS < LANGUAGES, (
        "grammars should be fewer than languages, since TypeScript and TSX share one")


@pytest.mark.parametrize("relpath,phrase", CLAIMS)
def test_each_page_states_the_derived_count(relpath, phrase):
    text = (REPO / relpath).read_text(encoding="utf-8")
    assert phrase in text, (
        f"{relpath} does not contain {phrase!r}. The parser now supports {LANGUAGES} "
        f"languages across {GRAMMARS} grammars; update the page, and check "
        f"docs/style-guide-reference.md, which is the phrasing every other page copies.")


@pytest.mark.parametrize("relpath,_phrase", CLAIMS)
def test_no_page_still_carries_the_previous_count(relpath, _phrase):
    """A page can gain the new sentence and keep the old one, which reads as a
    contradiction to anybody who scrolls.

    ANCHORED, and the first draft was not: a plain `"9 languages" in text` matches inside
    "19 languages" and failed against a page that was correct. A substring test on a number
    is the same hole as a substring test on a name.
    """
    text = (REPO / relpath).read_text(encoding="utf-8")
    for stale in range(1, LANGUAGES):
        assert not re.search(rf"(?<!\d){stale} languages\b", text), (
            f"{relpath} still says '{stale} languages' somewhere alongside the current "
            f"count of {LANGUAGES}")


# --- the OTHER count on the same page, which had already drifted --------------------

def test_the_vocabulary_diagram_alt_text_states_the_real_counts():
    """The graph-vocabulary image's alt text names how many kinds and bands it shows.

    It said "40 node kinds in 9 bands" against a registry holding 48 in 10, having gone
    stale several releases before anyone read it. Alt text is the version of that image a
    screen-reader user gets, so a wrong number there is not cosmetic: it is the only
    description they receive, and it was describing a different diagram.

    Derived from the registry for the same reason the language count is: a number written
    by hand beside a table that grows is a number that will be wrong.
    """
    from contextlake.kb.kinds import KIND_GROUP_ORDER, KIND_REGISTRY

    text = (REPO / "docs/indexing-the-code-graph.md").read_text(encoding="utf-8")
    phrase = f"all {len(KIND_REGISTRY)} node kinds in {len(KIND_GROUP_ORDER)} bands"
    assert phrase in text, (
        f"docs/indexing-the-code-graph.md does not contain {phrase!r}. The vocabulary diagram's "
        f"alt text must state the counts the registry actually holds; regenerate the "
        f"diagram and update the alt text together.")


def test_that_alt_text_check_is_not_vacuous():
    """Both halves must be plausible, or the assertion above could pass against a page
    that says "all 0 node kinds in 0 bands"."""
    from contextlake.kb.kinds import KIND_GROUP_ORDER, KIND_REGISTRY

    assert len(KIND_REGISTRY) >= 40, "the registry shrank unexpectedly; check the source"
    assert 5 <= len(KIND_GROUP_ORDER) <= 30
    assert len(KIND_GROUP_ORDER) < len(KIND_REGISTRY)
