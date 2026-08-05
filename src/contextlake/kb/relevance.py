"""The relevance floor: does this store contain anything the query is about?

A nearest-neighbour index has no concept of "no match". Asked about a technology
that appears nowhere in the fleet, it still returns its k nearest vectors, and
every one of them is a real node with a real file and line, so the answer reads
as cited and checkable while being about nothing the question asked.

The floor is lexical rather than a cosine threshold, and the measurement behind
that choice is recorded at the SEARCH branch in ``server.ask``: over 22 questions
whose answer IS in the store and 10 whose answer is not, no cosine statistic
separated the two classes, while term presence separated them cleanly. It is also
embedder-independent, which a tuned constant is not: contextlake ships four
embedding providers, and a constant fitted to one is silently wrong for the other
three.

This lives in its own module because it is a property of the *knowledge base*,
not of one transport. Written as a closure inside the MCP server, it bound
``semantic_search`` and ``hybrid_search`` and left ``kb query --retriever
semantic`` -- the same store, the same retriever, over the CLI -- returning k
confident unrelated hits for the query the MCP tools refused. One knowledge base
answering the same question two ways, depending on which surface asked, is the
defect this module exists to prevent recurring.
"""

from __future__ import annotations

__all__ = ["term_anchors", "below_floor"]


def term_anchors(store, query: str) -> tuple[list[str], bool]:
    """``(terms the index has never seen, whether any term IS indexed)``.

    Known limitation, measured rather than assumed: the probe is the FTS index,
    which covers name/qualified_name/file but NOT docstrings, while the embedded
    text does include docstrings. A question whose ONLY terms are rare prose words
    is therefore refused although the vectors could have answered it. Two things
    keep that acceptable: the refusal names the exact terms, so it is checkable
    and retryable rather than silent, and one indexed term anywhere in the query
    is enough to let the hits through.
    """
    from .router import content_terms

    terms = content_terms(query)
    unmatched = [t for t in terms if not store.search(t, limit=1)]
    return unmatched, len(terms) > len(unmatched)


def below_floor(store, query: str) -> bool:
    """True when not one content term in ``query`` is indexed anywhere."""
    unmatched, anchored = term_anchors(store, query)
    return bool(unmatched) and not anchored
