"""Tests for the shared whole-word symbol-mention text matcher."""

from contextlake.kb.connectors.text_match import match_symbol_mentions
from contextlake.kb.model import Confidence, Node


def _sym(nid, name, kind="function"):
    return Node(id=nid, repo="team/api", kind=kind, name=name, file="pay.py")


def test_match_symbol_mentions_finds_whole_word_matches():
    symbols = [_sym("s1", "charge"), _sym("s2", "Payer", kind="class")]
    matches = match_symbol_mentions("charge() is throwing when Payer is null", symbols)
    ids = {m[0] for m in matches}
    assert ids == {"s1", "s2"}
    assert all(conf == Confidence.AMBIGUOUS for _, conf in matches)


def test_match_symbol_mentions_no_partial_word_match():
    symbols = [_sym("s1", "charge")]
    matches = match_symbol_mentions("recharge_order failed", symbols)
    assert matches == []


def test_match_symbol_mentions_skips_short_names_and_non_embeddable_kinds():
    symbols = [_sym("s1", "id"), _sym("s2", "pay.py", kind="file")]
    matches = match_symbol_mentions("id and pay.py are both mentioned here", symbols)
    assert matches == []


def test_match_symbol_mentions_dedupes_repeated_mentions():
    symbols = [_sym("s1", "charge")]
    matches = match_symbol_mentions("charge charge charge", symbols)
    assert len(matches) == 1
