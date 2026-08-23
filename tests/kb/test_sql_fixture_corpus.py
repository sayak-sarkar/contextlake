"""Precision/recall for the regex SQL parser (kb/sql.py), quantified.

kb/sql.py is a regex extractor by documented necessity (T-SQL/PL-SQL defeat
tree-sitter -- see the module docstring), and every edge it emits is marked
``INFERRED``. Until now that correctness ceiling was acknowledged in comments
but never measured, so nobody knew what an ``INFERRED`` SQL edge was actually
worth (RC-P2-8 / K-2).

This test runs a small, synthetic, hand-labelled orders/customers/inventory
corpus (tests/kb/fixtures/sql/) through the real ``index_repo_dir`` pipeline --
the same path a real repo takes -- and scores the resolved ``references``
edges against a hand-checked ground truth (expected_edges.json): every FK
relationship a human reading the DDL would call real, including the ones the
parser is documented to miss (self-referencing FKs, FKs attached via a
separate ``ALTER TABLE`` statement).

The thresholds below are the honestly *measured* current numbers, not a
target. If a future change to sql.py's regex moves them, that is a real
signal -- ratchet the floor up (never lower it to make this pass) and update
docs/indexing-the-code-graph.md's published numbers to match. That last step used to
be a comment aimed at a filename that no longer existed, so the page kept
quoting a precision the parser had already beaten;
``test_published_numbers_match_the_docs`` now checks it instead of asking.

Precision was 0.90 on this corpus until sql.py started masking comments before
matching, which removed its one false positive; it is 1.00 now, recall
unchanged at 0.69.
"""

from __future__ import annotations

import json
from pathlib import Path

from contextlake.kb.model import Confidence
from contextlake.kb.parse import index_repo_dir

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sql"
REPO_ROOT = Path(__file__).resolve().parents[2]
# Every page that prints these figures. Both of them drifted once already,
# because the only thing keeping them current was a comment.
PUBLISHING_DOCS = ("docs/indexing-the-code-graph.md", "docs/explained.md")


def _load_ground_truth() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    data = json.loads((FIXTURE_DIR / "expected_edges.json").read_text(encoding="utf-8"))
    all_edges = {(e["src"], e["dst"]) for e in data["edges"]}
    detectable = {(e["src"], e["dst"]) for e in data["edges"] if e["detectable"]}
    return all_edges, detectable


def _emitted_reference_pairs(shard) -> set[tuple[str, str]]:
    name = {n.id: n.name for n in shard.nodes}
    return {(name[e.src], name[e.dst]) for e in shard.edges if e.relation == "references"}


def test_sql_fixture_corpus_precision_recall_floor():
    ground_truth, _detectable = _load_ground_truth()
    shard = index_repo_dir(str(FIXTURE_DIR), "fixtures/sql")
    emitted = _emitted_reference_pairs(shard)

    true_positives = emitted & ground_truth
    false_positives = emitted - ground_truth
    false_negatives = ground_truth - emitted

    precision = len(true_positives) / len(emitted) if emitted else 0.0
    recall = len(true_positives) / len(ground_truth) if ground_truth else 0.0

    # Measured on this corpus: 9 true positives, 0 false positives, 4 false
    # negatives (2 self-referencing FKs + 2 ALTER-TABLE-attached FKs, all
    # documented misses). Precision was 0.90 until sql.py learned to mask
    # comments before matching -- the floor is ratcheted up to the new measured
    # value, per this module's own rule. These are FLOORS to ratchet up as
    # sql.py improves, not targets already met -- do not lower them to make a
    # regression pass.
    assert precision >= 1.0, (
        f"SQL parser precision floor regressed: {precision:.4f} "
        f"(false positives: {sorted(false_positives)})")
    assert recall >= 0.69, (
        f"SQL parser recall floor regressed: {recall:.4f} "
        f"(false negatives: {sorted(false_negatives)})")

    # The specific false positive this corpus was built to catch, now pinned as
    # a negative: a commented-out FK is dead DDL, and must not resolve like a
    # real one just because the table it names exists elsewhere in the repo.
    assert ("orders", "regions") not in emitted

    # The two documented gap classes, by name -- if either starts resolving,
    # that's real parser progress and expected_edges.json's "detectable"
    # flag (plus docs/indexing-the-code-graph.md's published numbers) should be updated.
    assert ("customers", "customers") in false_negatives
    assert ("inventory_categories", "inventory_categories") in false_negatives
    assert ("shipments", "orders") in false_negatives
    assert ("shipments", "warehouses") in false_negatives


def test_published_numbers_match_the_docs():
    """The page that publishes these figures must quote what this corpus measures.

    The numbers below are derived from the same run as the floors above, then
    looked for verbatim in the doc, so the doc cannot quietly keep an older
    figure (which is exactly what happened while the pointer in this module's
    header named a file that had been renamed away).
    """
    ground_truth, _detectable = _load_ground_truth()
    emitted = _emitted_reference_pairs(index_repo_dir(str(FIXTURE_DIR), "fixtures/sql"))
    true_positives = emitted & ground_truth
    precision = len(true_positives) / len(emitted) if emitted else 0.0
    recall = len(true_positives) / len(ground_truth) if ground_truth else 0.0

    for name in PUBLISHING_DOCS:
        doc = REPO_ROOT / name
        assert doc.exists(), (
            f"{name} is one of the pages that publishes these numbers. If it moved, "
            "update this list and the module docstring together, or the published "
            "figures go stale unnoticed again.")
        text = doc.read_text(encoding="utf-8")
        for phrase in (f"precision {precision:.2f}", f"recall {recall:.2f}"):
            assert phrase in text, (
                f"{name} does not publish {phrase!r}. The corpus moved; update the "
                "page to match the measurement.")


def test_sql_fixture_corpus_emitted_edges_are_all_inferred_confidence():
    """docs/indexing-the-code-graph.md's claim ("every SQL edge is INFERRED") is itself
    checked here, not just asserted in prose -- every name in this corpus is
    unique, so nothing should resolve as AMBIGUOUS."""
    shard = index_repo_dir(str(FIXTURE_DIR), "fixtures/sql")
    ref_edges = [e for e in shard.edges if e.relation == "references"]
    assert ref_edges  # the corpus must actually produce edges, or this is vacuous
    assert all(e.confidence == Confidence.INFERRED for e in ref_edges)
