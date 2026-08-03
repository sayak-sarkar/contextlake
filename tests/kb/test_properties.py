"""Property-based tests for functions whose contracts are documented but only
example-tested elsewhere (``test_kb_ids_security.py``, ``test_kb_store.py``).

Repository content driving these functions (symbol names, file paths, search
queries) is untrusted -- it can be arbitrary bytes decoded as text, not just
the handful of hand-picked examples the unit tests cover. Hypothesis explores
that space; ``max_examples`` is kept modest (50-150) per test so the whole
file stays a small fraction of the ~65s full-suite budget.

``hypothesis`` is a ``dev``-extra-only dependency, not installed in every
environment that runs the suite (see pyproject.toml). ``importorskip`` turns
a missing install into a clean module-level SKIP instead of a collection
ERROR that would abort the whole run -- see RC-P1-6 report for exactly how to
install it and run this file locally.
"""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, example, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from contextlake.kb.ids import make_id, normalize_id
from contextlake.kb.security import MAX_LABEL_LEN, sanitize_label
from contextlake.kb.store.sqlite_store import SqliteStore, _fts_query

# Deadline=None: these functions are pure CPU work with no I/O, but the CI
# runner's per-example wall clock is noisy enough (shared cores) that
# hypothesis's default 200ms deadline produces flaky DeadlineExceeded
# failures unrelated to the property under test.
_SLOW = settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# ---------------------------------------------------------------------------
# normalize_id -- documented idempotent: normalize_id(normalize_id(s)) == normalize_id(s)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=False,
    reason=(
        "REAL BUG (found by this property test, not fixed -- see task RC-P1-6 "
        "report): normalize_id is NOT always idempotent, contradicting its own "
        "docstring. Root cause: the \\w-based punctuation strip runs BEFORE "
        "casefold(), so a casefold() that *expands* a character into a base "
        "letter plus a combining mark never gets a second punctuation-strip "
        "pass within one call. U+0130 LATIN CAPITAL LETTER I WITH DOT ABOVE "
        "('İ') full-casefolds to 'i' + U+0307 COMBINING DOT ABOVE (two "
        "codepoints) -- but U+0307 alone is not matched by \\w, so a *second* "
        "normalize_id call strips it as punctuation and then strips the now-"
        "trailing '_' too: normalize_id('İ') == 'i\\u0307' but "
        "normalize_id(normalize_id('İ')) == 'i'. The @example below pins this "
        "down deterministically; without it hypothesis's random search only "
        "sometimes lands on this character class within max_examples=100."
    ),
)
@_SLOW
@example("İ")  # U+0130, the confirmed counterexample; see reason= above
@given(st.text())
def test_normalize_id_is_idempotent(s):
    once = normalize_id(s)
    twice = normalize_id(once)
    assert twice == once


@_SLOW
@example("İ")  # same U+0130 class as the idempotence counterexample above --
# pinned deterministically to prove this assertion (unlike idempotence) holds
# for it: `out == out.casefold()` is guaranteed by construction (out IS the
# result of a .casefold() call, and Unicode case folding is itself idempotent
# per spec), so it can never be the multi-codepoint-expansion footgun that
# broke idempotence. Confirmed empirically rather than assumed.
@given(st.text())
def test_normalize_id_is_casefolded_and_ascii_underscore_only(s):
    """Every character class the recipe promises: casefold, and the only
    surviving separator is ``_`` (never raw punctuation/whitespace)."""
    out = normalize_id(s)
    assert out == out.casefold()
    if out:
        assert not out.startswith("_")
        assert not out.endswith("_")
    assert "__" not in out


# ---------------------------------------------------------------------------
# make_id -- empty parts dropped consistently; result matches normalize_id of
# the "_".join of the (separator-stripped) non-empty parts.
# ---------------------------------------------------------------------------

# Keep part alphabets small and mostly identifier-shaped so drop/join
# semantics dominate the property (arbitrary-unicode robustness is already
# covered by normalize_id's own properties above).
_PART = st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"),
                                        max_codepoint=0x2FFFF) | st.sampled_from("_.- /"),
                max_size=12)


@_SLOW
@given(st.lists(_PART, max_size=6))
def test_make_id_drops_empty_parts(parts):
    non_empty = [p for p in parts if p]
    assert make_id(*parts) == make_id(*non_empty)


@_SLOW
@given(st.lists(_PART, min_size=1, max_size=6))
def test_make_id_matches_normalize_id_of_joined_parts(parts):
    expected = normalize_id("_".join(p.strip("_.") for p in parts if p))
    assert make_id(*parts) == expected


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Inherits the normalize_id idempotence bug (see "
        "test_normalize_id_is_idempotent's reason=): make_id('İ') delegates "
        "straight to normalize_id, so the same U+0130 casefold-expansion "
        "counterexample applies. Pinned via @example below."
    ),
)
@_SLOW
@example(["İ"])
@given(st.lists(_PART, max_size=6))
def test_make_id_is_idempotent_like_normalize_id(parts):
    once = make_id(*parts)
    assert normalize_id(once) == once


# ---------------------------------------------------------------------------
# sanitize_label -- security boundary: defuses ANSI/terminal injection from
# repo-derived text before it reaches an agent. Actual contract (see
# security.py's _CONTROL_CHAR_RE): every C0/C1 control character is stripped
# EXCEPT tab/newline/CR, which are deliberately preserved as "ordinary
# whitespace". Output is capped at max_len and never raises on None.
# ---------------------------------------------------------------------------

_KEPT_CONTROL = {"\t", "\n", "\r"}


@_SLOW
@given(st.text())
def test_sanitize_label_strips_all_control_chars_except_whitespace(s):
    out = sanitize_label(s)
    for ch in out:
        if ch in _KEPT_CONTROL:
            continue
        cp = ord(ch)
        is_control = cp <= 0x1F or 0x7F <= cp <= 0x9F
        assert not is_control, f"control char {cp!r} survived sanitize_label"


@_SLOW
@given(st.text())
def test_sanitize_label_never_exceeds_max_length(s):
    assert len(sanitize_label(s)) <= MAX_LABEL_LEN


@_SLOW
@given(st.integers(min_value=1, max_value=1000))
def test_sanitize_label_respects_custom_max_len(max_len):
    out = sanitize_label("x" * (max_len + 500), max_len=max_len)
    assert len(out) <= max_len


def test_sanitize_label_none_is_safe():
    assert sanitize_label(None) == ""


# ---------------------------------------------------------------------------
# _fts_query -- must never produce a string that raises a FTS5 syntax error
# when executed as a MATCH query, for any input text.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fts_conn():
    # A throwaway in-memory FTS5 table mirrors node_fts's shape closely enough
    # to exercise MATCH parsing without needing a full SqliteStore. Module-
    # scoped (not function-scoped) so hypothesis resolves it once and reuses
    # it across every example of the test below -- safe because the table is
    # never written to, only queried, so there is no cross-example state to
    # reset. A function-scoped fixture here trips hypothesis's
    # function_scoped_fixture health check (it would silently NOT be reset
    # between examples anyway, which is the exact footgun that check exists
    # to catch).
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(name)")
    yield conn
    conn.close()


@_SLOW
@given(st.text(max_size=200))
def test_fts_query_output_never_raises_on_match(fts_conn, s):
    q = _fts_query(s)
    if not q:
        return  # SqliteStore.search() guards this case before executing MATCH
    # Must not raise sqlite3.OperationalError (malformed MATCH expression).
    fts_conn.execute("SELECT * FROM t WHERE t MATCH ?", (q,)).fetchall()


def test_fts_query_smoke_through_real_store(tmp_path):
    """NOT a property assertion -- SqliteStore.search() (sqlite_store.py:238-247)
    deliberately catches sqlite3.OperationalError around the MATCH execute and
    treats an "fts5"/"syntax error"/"malformed match" message as an expected,
    silent no-hits case. So a bug in _fts_query's output would be swallowed
    here, not surfaced. This only smoke-tests that going through the real
    schema/indexes for a handful of sharp edge cases doesn't raise some OTHER
    (unexpected) exception before reaching that guard. The actual property --
    _fts_query's output never causes a MATCH syntax error in the first place
    -- is asserted for real by test_fts_query_output_never_raises_on_match
    above, which executes MATCH directly against a bare table with no
    exception handler in the way."""
    store = SqliteStore(tmp_path / "kb.sqlite")
    for s in ["", " ", '"""', "AND OR NOT NEAR", "a" * 500, "\x00\x01", "日本語 café"]:
        store.search(s)  # must not raise
