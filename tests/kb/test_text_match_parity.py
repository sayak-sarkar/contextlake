"""`match_symbol_mentions` tokenises the text once; it must still match what the
per-symbol regex matched (P03).

The old implementation compiled `\\b<name>\\b` for every symbol for every document. That
cost symbols x text length: 20,000 symbols against one 40 KB document took 8.4 s. The new
one tokenises the text once and looks names up in a set. Speed is worthless if the matches
move, so the old body is kept below as the oracle and every test here compares ORDERED
output (ids and order), never a set. Timings are in the report, not asserted: a wall-clock
assertion would be a flaky test on a loaded CI box.

Names with a non-word character (`$ref`, `a.b`, `Foo::bar`, `operator==`) are the risky
shape, because the old regex put `\\b` next to the non-word character, where it means
"a word character must be on the OTHER side". That quirk is kept on purpose and pinned
below, not fixed.
"""

from __future__ import annotations

import random
import re

import pytest

from contextlake.kb.connectors import text_match
from contextlake.kb.connectors.text_match import (
    link_documents_to_symbols,
    match_symbol_mentions,
)
from contextlake.kb.embeddings.index import EMBEDDABLE_KINDS
from contextlake.kb.model import Confidence, Node
from contextlake.kb.store.sqlite_store import SqliteStore


def _oracle(text, symbols, *, min_name_len=3):
    """The implementation this replaced, verbatim."""
    candidates = [
        s for s in symbols
        if s.kind in EMBEDDABLE_KINDS and s.name and len(s.name) >= min_name_len
    ]
    candidates.sort(key=lambda s: len(s.name), reverse=True)
    seen: set[str] = set()
    matches: list[tuple[str, Confidence]] = []
    for sym in candidates:
        if sym.id in seen:
            continue
        pattern = r"\b" + re.escape(sym.name) + r"\b"
        if re.search(pattern, text):
            matches.append((sym.id, Confidence.AMBIGUOUS))
            seen.add(sym.id)
    return matches


def _sym(nid, name, kind="function"):
    return Node(id=nid, repo="app", kind=kind, name=name, file="x.py")


def _is_pure_word(name):
    return re.search(r"\W", name) is None


PURE_WORD_NAMES = [
    "charge", "Charge", "CHARGE", "sample", "sample_grid", "sampleGrid", "foo_", "_foo",
    "1foo", "foo1", "__init__", "café", "café", "Über", "日本語",
    "abc", "ab", "x_1",
]
# Every shape the old pattern had to handle: leading/trailing non-word characters, inner
# ones, metacharacters, whitespace, a newline, and a name with no word character at all.
NON_WORD_NAMES = [
    "$foo", "foo$", "$", "$$$", "a.b", "a.b.c", "Foo::bar", "::bar", "Foo::", "~Foo",
    "operator==", "operator()", "<=>", "a-b", "a b", "a\nb", "x+y", "(", "[a]", "foo?",
    "bar!", "@Entry", "#define", "a|b", "^foo", "foo^", "\\d+", "a*b", "(?i)foo", ".*",
    "foo\\", "...", "a$b", "$a$",
]
# What can sit on either side of a name in a document.
CONTEXTS = [
    "{}", " {} ", "a{}b", "a{} ", " {}b", "_{}_", "1{}1", "({})", "{}\n", "\n{}", "{}.",
    "-{}-", "${}", "{}$", "::{}", "{}::",
]
EXTRA_TEXTS = [
    "", "   ", "$", "$$$", "a$ $a", "charge()", "Charge charge CHARGE",
    "sample_grid sample sampleGrid", "foo_ _foo foo", "1foo foo1", "__init__",
    "café café cafe", "Über über", "日本語です",
    "a.b is used; a.b.c too", "Foo::bar and ::bar", "delete ~Foo;", "x~Foo",
    "operator== defined", "xoperator==y", "a<=>b", "x <=> y", "foo? bar!",
    "pattern \\d+ and (?i)foo and .* and a*b", "email me@example.com", "a$foo b", " $foo ",
]


def _all_symbols():
    names = PURE_WORD_NAMES + NON_WORD_NAMES
    return [_sym(f"n{i}", n) for i, n in enumerate(names)]


def _texts():
    out = list(EXTRA_TEXTS)
    for name in PURE_WORD_NAMES + NON_WORD_NAMES:
        out.extend(ctx.format(name) for ctx in CONTEXTS)
    return out


@pytest.mark.parametrize("min_name_len", [1, 3])
def test_matches_equal_the_per_symbol_regex_over_every_context(min_name_len):
    symbols = _all_symbols()
    by_id = {s.id: s.name for s in symbols}
    pure = nonword = 0
    for text in _texts():
        expected = _oracle(text, symbols, min_name_len=min_name_len)
        got = match_symbol_mentions(text, symbols, min_name_len=min_name_len)
        assert got == expected, repr(text)
        for sid, _conf in expected:
            if _is_pure_word(by_id[sid]):
                pure += 1
            else:
                nonword += 1
    # The comparison is only worth anything if both kinds of name matched.
    assert pure > 100 and nonword > 100, (pure, nonword)


def test_the_non_word_name_quirk_is_kept_not_fixed():
    """`\\b` next to `$` wants a word character on the other side, so `$foo` matches
    in `a$foo` and not in ` $foo`. Parity means keeping that; fixing it would be a
    behaviour change this performance fix has no business making."""
    symbols = [_sym("dollar", "$foo")]
    assert match_symbol_mentions("a$foo", symbols) == [("dollar", Confidence.AMBIGUOUS)]
    assert match_symbol_mentions(" $foo ", symbols) == []
    assert match_symbol_mentions("$foo", symbols) == []
    for text in ("a$foo", " $foo ", "$foo"):
        assert match_symbol_mentions(text, symbols) == _oracle(text, symbols)


def test_a_whole_word_match_does_not_fire_inside_a_longer_word():
    """The reason `\\b` was there: `sample` must not match inside `sample_grid`."""
    symbols = [_sym("s", "sample")]
    assert match_symbol_mentions("sample_grid", symbols) == []
    assert match_symbol_mentions("a sample_grid and a sample", symbols) == \
        [("s", Confidence.AMBIGUOUS)]
    assert match_symbol_mentions("Sample SAMPLE", symbols) == []        # case-sensitive


def test_unicode_word_characters_are_word_characters():
    symbols = [_sym("cafe", "café"), _sym("kanji", "日本語")]
    assert match_symbol_mentions("un café noir", symbols) == [
        ("cafe", Confidence.AMBIGUOUS)]
    assert match_symbol_mentions("cafés", symbols) == []            # a longer word
    assert match_symbol_mentions("x日本語y", symbols) == []


def test_duplicate_ids_keep_the_first_name_that_matches():
    """Two nodes sharing an id with different names: the longer name is tried first, and
    the id is skipped only AFTER one of them matched."""
    symbols = [_sym("dup", "alpha"), _sym("dup", "longer_alpha"), _sym("solo", "beta")]
    for text in ("alpha", "longer_alpha", "alpha longer_alpha beta", "beta", ""):
        assert match_symbol_mentions(text, symbols) == _oracle(text, symbols), text


def test_short_names_and_non_embeddable_kinds_are_skipped_as_before():
    symbols = [_sym("s1", "id"), _sym("s2", "pay.py", kind="file"),
               _sym("s3", "ok_name"), _sym("s4", "doc_name", kind="document")]
    text = "id pay.py ok_name doc_name"
    assert match_symbol_mentions(text, symbols) == _oracle(text, symbols) == \
        [("s3", Confidence.AMBIGUOUS)]


def test_randomised_parity_sweep():
    """Seeded sweep over names and documents built from the awkward alphabet. Texts embed
    the names themselves, so most pairs of symbols and text produce matches."""
    rng = random.Random(20260904)
    alphabet = list("abAB_$.:~-+= \n<>12") + ["é", "́", "日"]
    kinds = ["function"] * 9 + ["file"]

    def rand_name():
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 5)))

    pure = nonword = 0
    for _round in range(6):
        symbols = []
        for i in range(120):
            # ~5% reuse an earlier id, which exercises the `seen` rule.
            nid = f"n{rng.randint(0, i)}" if rng.random() < 0.05 else f"n{i}"
            symbols.append(_sym(nid, rand_name(), kind=rng.choice(kinds)))
        names = [s.name for s in symbols]
        for _ in range(60):
            parts = []
            for _ in range(rng.randint(0, 12)):
                parts.append(rng.choice(names) if rng.random() < 0.4
                             else "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4))))
            text = rng.choice(["", " ", "a", "_", "$"]).join(parts)
            for min_len in (1, 3):
                expected = _oracle(text, symbols, min_name_len=min_len)
                got = match_symbol_mentions(text, symbols, min_name_len=min_len)
                assert got == expected, (min_len, text)
                by_id = {s.id: s.name for s in symbols}
                for sid, _conf in expected:
                    if _is_pure_word(by_id[sid]):
                        pure += 1
                    else:
                        nonword += 1
    # Both branches of the new code were exercised against real matches.
    assert pure > 100 and nonword > 100, (pure, nonword)


def test_link_documents_to_symbols_matches_what_the_oracle_would_link():
    store = SqliteStore(":memory:")
    try:
        symbols = [_sym(f"s{i}", n) for i, n in enumerate(
            ["charge", "Payer", "$ref", "a.b", "sample", "sample_grid", "operator=="])]
        store.upsert_nodes("app", symbols)
        texts = ["charge() throws when Payer is null", "see a$ref and a.b", "sample_grid only",
                 "", "nothing here", "operator== and sample"]
        docs = [Node(id=f"doc{i}", repo="@ingest:t", kind="document", name=f"d{i}")
                for i in range(len(texts))]
        edges = link_documents_to_symbols(store, "app", docs, texts, "documented_by",
                                          "ingest", repo_fallback=False)
        linked: dict[str, list[str]] = {}
        for e in edges:
            linked.setdefault(e.dst, []).append(e.src)
        for doc, text in zip(docs, texts, strict=True):
            expected = [sid for sid, _ in _oracle(text, symbols)]
            assert linked.get(doc.id, []) == expected, text
        assert any(linked.values())
    finally:
        store.close()


# --- structure: the property the speed-up rests on, not a stopwatch -------------------

class _ReSearchSpy:
    """Records the patterns handed to `re.search`, then runs the real thing."""

    def __init__(self, real):
        self.real = real
        self.patterns: list[str] = []

    def __call__(self, pattern, string, *a, **k):
        self.patterns.append(pattern)
        return self.real(pattern, string, *a, **k)


@pytest.fixture
def spy(monkeypatch):
    s = _ReSearchSpy(re.search)
    monkeypatch.setattr(re, "search", s)
    return s


def _pattern_for(name):
    return r"\b" + re.escape(name) + r"\b"


def test_a_pure_word_name_never_compiles_or_runs_a_regex(spy):
    symbols = [_sym(f"n{i}", f"Sym{i}Handler") for i in range(2000)]
    text = " ".join(f"Sym{i}Handler" for i in range(0, 2000, 7)) + " other words"
    got = match_symbol_mentions(text, symbols)
    assert len(got) == len(range(0, 2000, 7))          # the matches are real
    assert spy.patterns == []                          # and no regex ran for any of them


def test_the_spy_is_live_a_non_word_name_does_reach_the_regex(spy):
    """Positive control: without this the test above would pass if the spy were inert."""
    symbols = [_sym("d", "$foo")]
    assert match_symbol_mentions("a$foo", symbols) == [("d", Confidence.AMBIGUOUS)]
    assert spy.patterns == [_pattern_for("$foo")]


def test_a_non_word_name_skips_the_regex_when_the_text_lacks_one_of_its_words(spy):
    symbols = [_sym("a", "Foo::bar"), _sym("b", "$ref")]
    assert match_symbol_mentions("nothing relevant here", symbols) == []
    assert spy.patterns == []
    # "Foo" and "bar" are tokens but "ref" is not: still no regex for `$ref`, one for the other.
    match_symbol_mentions("Foo::bar", symbols)
    assert spy.patterns == [_pattern_for("Foo::bar")]


def test_a_name_with_no_word_character_always_reaches_the_regex(spy):
    symbols = [_sym("c", "<=>")]
    assert match_symbol_mentions("a<=>b", symbols) == [("c", Confidence.AMBIGUOUS)]
    assert spy.patterns == [_pattern_for("<=>")]


def test_link_documents_prepares_the_symbols_once_not_once_per_document(monkeypatch):
    calls = []
    real = text_match._prepare_candidates

    def counting(symbols, min_name_len):
        calls.append(len(symbols))
        return real(symbols, min_name_len)

    monkeypatch.setattr(text_match, "_prepare_candidates", counting)
    store = SqliteStore(":memory:")
    try:
        store.upsert_nodes("app", [_sym("s1", "charge"), _sym("s2", "Payer")])
        texts = [f"charge {i}" for i in range(25)]
        docs = [Node(id=f"d{i}", repo="@ingest:t", kind="document", name=f"d{i}")
                for i in range(len(texts))]
        edges = link_documents_to_symbols(store, "app", docs, texts, "documented_by",
                                          "ingest", repo_fallback=False)
        assert len(edges) == 25
    finally:
        store.close()
    assert calls == [2]
