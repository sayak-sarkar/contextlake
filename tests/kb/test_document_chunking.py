"""An ingested document is several vectors now, and still exactly one node.

It used to be one vector over the whole text. For a 14 KB page that vector is an average of
everything the page discusses, so a question about one paragraph in it matched poorly.
Measured on 29 real documents with 53 position-selected queries: hit rate 71.7% -> 94.3%,
MRR 45.2% -> 80.4%.

The load-bearing property is that **chunking is invisible above the vector store**. The
chunk index lives inside the stored KEY, and `search()` collapses keys back to node ids, so
all four callers of `search()` keep receiving node ids and never learn chunks exist. If that
ever stops holding, the symptom is the one this codebase already documents: unresolvable ids
are dropped by the retrieval tools, which then return a shorter, plausible, non-empty answer.
"""

from __future__ import annotations

import pytest

from contextlake.kb.embeddings.chunk import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP,
    split_document,
)
from contextlake.kb.embeddings.index import EMBED_CONTENT_VERSION
from contextlake.kb.embeddings.store import (
    CHUNK_SEP,
    VectorStore,
    _collapse_chunks,
    base_node_id,
    chunk_key,
)


def _doc(paragraphs: int, words: int = 40) -> str:
    return "\n\n".join(" ".join(f"w{i}p{p}" for i in range(words))
                       for p in range(paragraphs))


# --- splitting ---------------------------------------------------------------------------

def test_a_short_document_is_one_chunk_exactly_as_before():
    """The whole point of a floor: short documents must be byte-for-byte unaffected, or
    this change silently rewrites what every small page embeds."""
    text = "One short paragraph.\n\nAnd a second one."
    assert split_document(text) == [text]


def test_a_long_document_becomes_several_chunks():
    chunks = split_document(_doc(40))
    assert len(chunks) > 1


def test_no_chunk_greatly_exceeds_the_budget():
    """Packing whole paragraphs means a chunk can overshoot slightly; it must not overshoot
    without limit, or one enormous paragraph reintroduces the averaging being removed."""
    for chunk in split_document(_doc(60, words=60)):
        assert len(chunk) <= DEFAULT_MAX_CHARS + DEFAULT_OVERLAP + 200


def test_consecutive_chunks_overlap():
    """A point spanning a boundary must still sit whole in one chunk. Without overlap the
    passages most likely to be cut in half are the ones a specific question asks about."""
    chunks = split_document(_doc(40))
    assert len(chunks) >= 2
    tail = chunks[0][-DEFAULT_OVERLAP:]
    assert any(word and word in chunks[1] for word in tail.split()[-3:])


def test_the_last_paragraph_is_never_dropped():
    """A splitter that discards a short remainder loses the conclusion -- often the one
    paragraph that says what the page decided."""
    text = _doc(30) + "\n\nTHE FINAL SENTENCE OF THE WHOLE PAGE."
    assert "THE FINAL SENTENCE OF THE WHOLE PAGE." in split_document(text)[-1]


def test_blank_input_produces_no_chunks():
    """Not one empty chunk. An embedded empty string is a vector that matches everything
    weakly, which is worse than having no vector for that document at all."""
    assert split_document("") == []
    assert split_document("   \n\n  \t ") == []


def test_one_paragraph_larger_than_the_budget_is_cut_rather_than_emitted_whole():
    giant = " ".join(f"word{i}" for i in range(4000))
    chunks = split_document(giant)
    assert len(chunks) > 1
    assert all(len(c) <= DEFAULT_MAX_CHARS + 50 for c in chunks)


def test_an_overlap_at_least_as_large_as_the_chunk_cannot_hang():
    """Clamped, because a chunk that carries forward everything it just emitted makes no
    progress and the loop never ends. A hang is the worst failure shape here: no output,
    no error, and a run that looks merely slow."""
    chunks = split_document(_doc(20), max_chars=200, overlap=500)
    assert len(chunks) > 1
    assert all(len(c) <= 400 for c in chunks)


# --- keys --------------------------------------------------------------------------------

def test_a_chunk_key_round_trips_to_its_node_id():
    node = "@ingest:cli:docs/usage.md"
    assert base_node_id(chunk_key(node, 0)) == node
    assert base_node_id(chunk_key(node, 17)) == node


def test_an_unchunked_key_passes_through_unchanged():
    """Code-node vectors are still written under a bare node id. They must survive the same
    collapse path untouched, or every code symbol disappears from semantic search."""
    assert base_node_id("demo_app_catalogservice") == "demo_app_catalogservice"


def test_the_separator_cannot_occur_in_a_document_id():
    """A printable separator would not be safe: a document id is a relative path, and a
    filename may legally contain `#` or `:`. A control character cannot appear in either a
    normalized node id or a real path."""
    assert CHUNK_SEP == "\x1f"
    assert not CHUNK_SEP.isprintable()
    node = "@ingest:cli:docs/a#b:c.md"
    assert base_node_id(chunk_key(node, 3)) == node


# --- collapsing --------------------------------------------------------------------------

def test_collapse_keeps_the_best_scoring_chunk_per_node():
    scored = [(chunk_key("a", 0), 0.2), (chunk_key("a", 1), 0.9), (chunk_key("b", 0), 0.5)]
    assert _collapse_chunks(scored, 10) == [("a", 0.9), ("b", 0.5)]


def test_collapse_ranks_by_score_and_trims_to_k():
    scored = [(chunk_key(n, 0), s) for n, s in (("a", 0.1), ("b", 0.9), ("c", 0.5))]
    assert [n for n, _ in _collapse_chunks(scored, 2)] == ["b", "c"]


def test_collapse_happens_before_trimming():
    """Trimming first would drop a document whose only strong chunk sat behind several
    chunks of a different one -- exactly the case chunking creates."""
    scored = [(chunk_key("a", i), 0.9 - i * 0.01) for i in range(10)]
    scored.append((chunk_key("b", 0), 0.5))
    assert [n for n, _ in _collapse_chunks(scored, 2)] == ["a", "b"]


# --- the store's contract ----------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    vs = VectorStore(tmp_path / "vec.sqlite")
    yield vs
    vs.close()


def test_search_returns_node_ids_never_chunk_keys(store):
    """The property everything above the store depends on. A chunk key leaking out reaches
    a caller that cannot resolve it, and the retrieval tools drop unresolvable hits -- a
    shorter, plausible, non-empty answer, which is the failure this project treats as worst."""
    store.upsert([(chunk_key("doc/a", 0), "r", [1.0, 0.0]),
                  (chunk_key("doc/a", 1), "r", [0.9, 0.1]),
                  (chunk_key("doc/b", 0), "r", [0.0, 1.0])])
    hits = store.search([1.0, 0.0], k=5)
    assert [nid for nid, _ in hits] == ["doc/a", "doc/b"]
    assert all(CHUNK_SEP not in nid for nid, _ in hits)


def test_one_document_cannot_crowd_the_whole_result(store):
    """Ten chunks of one document must not fill a k=2 result and hide every other document.
    This is what the over-fetch exists for."""
    store.upsert([(chunk_key("hot", i), "r", [1.0, 0.0]) for i in range(10)])
    store.upsert([(chunk_key("other", 0), "r", [0.95, 0.05])])
    assert [nid for nid, _ in store.search([1.0, 0.0], k=2)] == ["hot", "other"]


def test_a_document_scores_by_its_best_chunk(store):
    """A page with one highly relevant paragraph should beat a page that is vaguely on topic
    throughout. Averaging the chunks would throw away the reason chunking works."""
    store.upsert([(chunk_key("sharp", 0), "r", [1.0, 0.0]),
                  (chunk_key("sharp", 1), "r", [0.0, 1.0]),
                  (chunk_key("vague", 0), "r", [0.7, 0.7])])
    assert store.search([1.0, 0.0], k=1)[0][0] == "sharp"


# --- the version gate --------------------------------------------------------------------

def test_the_embed_content_version_was_bumped():
    """Both halves of the staleness rule changed at once: the text a vector is built from,
    and the shape of every stored key. A store left at the old version holds one averaged
    vector per document under a key nothing writes any more."""
    assert EMBED_CONTENT_VERSION >= 5


# --- the splitter must always make progress ------------------------------------------------

def test_a_long_run_with_no_late_whitespace_does_not_hang():
    """The real hang, found by running it rather than reading it.

    A paragraph over the budget is cut at the last space before `max_chars`. When that space
    sits INSIDE the overlap window -- one word, then a long unbroken run, which is what an
    embedded base64 image or a minified line looks like -- the next buffer started at
    `cut - overlap`, clamped to 0, so it was the buffer that had just been emitted. The loop
    never terminated and `out` grew without bound: no error, no output, and a machine that
    slowly runs out of memory. `pytest-timeout` is not a dependency here, so the guard is
    that this test returns at all; unfixed it never does.
    """
    text = "a " + "x" * 5000
    chunks = split_document(text)
    assert len(chunks) > 1
    assert all(len(c) <= DEFAULT_MAX_CHARS for c in chunks)


def test_an_overlap_over_half_the_budget_cannot_crawl():
    """The QUIET half of the same defect: progress that is technically non-zero.

    Progress per cut is `max_chars - overlap`. Clamping overlap only to `max_chars - 1` --
    enough to stop the loop hanging -- leaves it free to advance ONE character per cut, so
    a single long paragraph emits thousands of chunks that are each nearly all of the
    previous one. That is not a hang, it is the same runaway allocation arriving slowly,
    and it is invisible to a test that only asks whether the call returns.

    Stated as a bound on the count rather than on the clamped value, so it survives a
    change to how the clamp is spelled. Unclamped this text yields ~1441 chunks; with
    overlap capped at half the budget it yields ~19.
    """
    text = "word " * 400
    budget = 200
    chunks = split_document(text, max_chars=budget, overlap=500)
    assert len(chunks) > 1
    # Every cut must advance by at least half the budget, so the count is bounded by the
    # text length divided by that, plus the trailing remainder.
    assert len(chunks) <= len(text) // (budget // 2) + 2


def test_no_whitespace_at_all_still_terminates():
    """The same defect with the space removed entirely: `rfind` returns -1, the cut falls
    back to `max_chars`, and progress has to come from the fallback rather than from luck."""
    chunks = split_document("x" * 5000)
    assert len(chunks) > 1


def _counter(n: int) -> str:
    """`n` characters with no whitespace and no repeating window.

    Repetition is not a detail here. An earlier version of the coverage check below ran on
    `"x" * 5000`, and every 1200-character window of that string is identical, so `str.find`
    matched the FIRST occurrence rather than the one the chunk came from and reported text as
    lost that was present. The fixture, not the splitter, was wrong. A six-digit counter has
    a unique window everywhere, which is what makes the check able to distinguish a real gap.
    """
    return "".join(f"{i:06d}" for i in range(n // 6 + 1))[:n]


@pytest.mark.parametrize("text", [
    "a " + _counter(5000),                                   # cut lands inside the overlap
    _counter(5000),                                          # no whitespace anywhere
    _doc(40),                                                # ordinary paragraphs
    # A giant paragraph between normal ones. The tail paragraphs are re-lettered because
    # `_doc(3)` twice would put IDENTICAL text at both ends, and the cursor below would
    # match a leading chunk against the trailing copy and skip everything in between --
    # the same self-similarity trap `_counter` exists to avoid.
    _doc(3) + "\n\n" + _counter(4000) + "\n\n" + _doc(3).replace("w", "z"),
], ids=["space-inside-overlap", "no-whitespace", "paragraphs", "giant-in-the-middle"])
def test_no_text_is_lost_between_chunks(text):
    """Content loss is the QUIET sibling of the hang, and the fallback branch added to fix
    the hang is exactly where it would live: give up the overlap and it is one off-by-one
    from skipping the characters between two cuts.

    Stated as interval coverage rather than as a forward cursor, deliberately. Overlap makes
    the chunks non-monotonic -- a chunk can begin before the previous one ended -- and a
    cursor that assumes otherwise reports text as lost that is present, which two earlier
    versions of this test did. Each chunk is located in the document (exactly once, or the
    fixture is too self-similar to prove anything), and the union of those intervals must be
    the whole document. Whitespace is ignored because the splitter re-joins paragraphs with
    a blank line and strips at the cuts.
    """
    def squeeze(s: str) -> str:
        return "".join(s.split())

    want = squeeze(text)
    covered: list[tuple[int, int]] = []
    for chunk in split_document(text):
        piece = squeeze(chunk)
        assert piece, "an empty chunk is a wasted vector"
        hits = []
        at = want.find(piece)
        while at != -1:
            hits.append(at)
            at = want.find(piece, at + 1)
        assert hits, "a chunk holds text that is not in the document"
        assert len(hits) == 1, (
            "this chunk occurs more than once in the fixture, so the check below cannot "
            "tell coverage from a coincidence -- give the fixture unique content")
        covered.append((hits[0], hits[0] + len(piece)))

    covered.sort()
    reach = 0
    for lo, hi in covered:
        assert lo <= reach, f"characters {reach}..{lo} of the document are in no chunk"
        reach = max(reach, hi)
    assert reach == len(want), f"the last {len(want) - reach} characters are in no chunk"
