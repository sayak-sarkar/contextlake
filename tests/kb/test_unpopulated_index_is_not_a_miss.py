"""An empty index answering `[]` must not read as "nothing matched".

A nearest-neighbour search over an empty table returns the same value as one over a
populated table that found nothing: an empty list. The two mean opposite things. The
first is "this search never ran"; the second is "the graph holds nothing like your
query". `contextlake init` writes `[embeddings] enabled = true` while no vectors exist
until `kb embed` runs, so the first case is the state of every workspace on its first
day -- which made "No matches" the default answer to a working semantic query.
"""

from __future__ import annotations

from contextlake.kb.embeddings.store import VectorStore, unpopulated_reason


def _store(tmp_path, name="v.sqlite"):
    return VectorStore(tmp_path / name)


def test_an_empty_store_says_it_has_nothing_to_search(tmp_path):
    reason = unpopulated_reason(_store(tmp_path))
    assert reason is not None
    assert "kb embed" in reason, "the reason must carry the remedy, not just the fact"


def test_a_populated_store_reports_nothing(tmp_path):
    vs = _store(tmp_path)
    vs.upsert([("svc-billing::charge_card", "svc-billing", [0.1, 0.2, 0.3])])
    assert unpopulated_reason(vs) is None


def test_a_repo_with_no_vectors_is_told_apart_from_an_empty_store(tmp_path):
    """"None here, some elsewhere" and "none anywhere" need different fixes, so the two
    messages must differ and the scoped one must name the repo and its own remedy."""
    vs = _store(tmp_path)
    vs.upsert([("svc-billing::charge_card", "svc-billing", [0.1, 0.2, 0.3])])
    scoped = unpopulated_reason(vs, "svc-orders")
    empty = unpopulated_reason(_store(tmp_path, "other.sqlite"))
    assert scoped is not None and empty is not None
    assert scoped != empty
    assert "svc-orders" in scoped
    assert "kb embed svc-orders" in scoped
    assert "svc-orders" not in empty


def test_a_repo_that_has_vectors_reports_nothing(tmp_path):
    vs = _store(tmp_path)
    vs.upsert([("svc-billing::charge_card", "svc-billing", [0.1, 0.2, 0.3])])
    assert unpopulated_reason(vs, "svc-billing") is None


def test_vectors_in_a_linked_partition_still_count_as_present(tmp_path):
    """The count must use the SAME scope expansion `search` uses.

    `search(repo=...)` widens to the repo's connector and enrichment partitions. A count
    that matched only the literal repo id would report "no vectors for this repo" while
    the very next search over the same filter returned hits -- a false alarm produced by
    a count and a query disagreeing about what the filter means.
    """
    from contextlake.kb.connectors.enrich import enrich_partition

    vs = _store(tmp_path)
    vs.upsert([("doc-1", enrich_partition("svc-billing"), [0.1, 0.2, 0.3])])
    assert unpopulated_reason(vs, "svc-billing") is None
    assert vs.search([0.1, 0.2, 0.3], k=5, repo="svc-billing"), (
        "guard is only meaningful if search really does reach that partition")


# --- the three MCP surfaces ------------------------------------------------------
#
# `semantic_search`, `hybrid_search` and `ask` each read the vector store directly.
# Fixing the CLI alone would leave an agent, which cannot see the store at all, with an
# empty result and nothing to explain it -- it would report back that the codebase has
# no such concept.


def test_the_server_note_says_the_index_is_unpopulated(tmp_path):
    from contextlake.kb.server import _unpopulated_note

    note = _unpopulated_note(_store(tmp_path), None)
    assert note is not None
    assert "UNPOPULATED" in note and "not because nothing matched" in note


def test_the_server_note_is_absent_once_vectors_exist(tmp_path):
    from contextlake.kb.server import _unpopulated_note

    vs = _store(tmp_path)
    vs.upsert([("svc-billing::charge_card", "svc-billing", [0.1, 0.2, 0.3])])
    assert _unpopulated_note(vs, None) is None


def test_the_bare_reason_is_available_for_a_caller_that_frames_it(tmp_path):
    """`ask` folds the reason into the sentence naming which search really ran, so it
    needs the reason without the note's framing wrapped around it."""
    from contextlake.kb.server import _unpopulated_reason

    reason = _unpopulated_reason(_store(tmp_path), None)
    assert reason is not None
    assert "UNPOPULATED" not in reason
    assert "kb embed" in reason


def test_a_broken_vector_store_never_fails_the_tool_call():
    """A disclosure is not worth failing a tool call for."""
    from contextlake.kb.server import _unpopulated_note, _unpopulated_reason

    class Exploding:
        def count(self):
            raise RuntimeError("db is gone")

    assert _unpopulated_note(Exploding(), None) is None
    assert _unpopulated_reason(Exploding(), None) is None


def test_the_check_is_a_probe_not_a_count(tmp_path):
    """It runs before every semantic query, so it must not scan the store.

    Asserted on the interface rather than on timing: `has_any` is what the reason
    consults, and a `count()` that raised would go unnoticed if the reason still
    called it.
    """
    vs = _store(tmp_path)
    vs.upsert([("svc-billing::charge_card", "svc-billing", [0.1, 0.2, 0.3])])

    class NoCounting:
        def __init__(self, inner):
            self._inner = inner

        def has_any(self, repo_ids=None):
            return self._inner.has_any(repo_ids)

        def count(self):
            raise AssertionError("unpopulated_reason must probe, not count")

        def count_repo(self, repo_id):
            raise AssertionError("unpopulated_reason must probe, not count")

    guarded = NoCounting(vs)
    assert unpopulated_reason(guarded) is None
    assert unpopulated_reason(guarded, "svc-billing") is None
    assert unpopulated_reason(guarded, "svc-orders") is not None
