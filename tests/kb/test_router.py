"""The ask-router classifier (kb/router.py) — the eval gate for query routing.

A labelled question set graded on route AND extracted target. A misroute or a
missed symbol shows up as a failing case, so routing quality is falsifiable.
"""

import pytest

from contextlake.kb.router import (
    CALLERS,
    DEFINITION,
    DEPENDENTS,
    EXPLAIN,
    IMPACT,
    OWNERS,
    SEARCH,
    SUBCLASSES,
    classify,
    extract_target,
)

# (question, expected_route, expected_target-or-None-to-skip-target-check)
_CASES = [
    ("where is CatalogService defined", DEFINITION, "CatalogService"),
    ("definition of charge_order", DEFINITION, "charge_order"),
    ("who calls charge_order", CALLERS, "charge_order"),
    ("what calls PaymentClient", CALLERS, "PaymentClient"),
    ("callers of validate_token", CALLERS, "validate_token"),
    ("who uses the auth-service", CALLERS, "auth-service"),
    ("what depends on requests", DEPENDENTS, "requests"),
    ("which repos use shared-core", DEPENDENTS, "shared-core"),
    ("dependents of the payments package", DEPENDENTS, "payments"),
    ("what extends BaseController", SUBCLASSES, "BaseController"),
    ("subclasses of Embedder", SUBCLASSES, "Embedder"),
    ("who implements Store", SUBCLASSES, "Store"),
    ("implementations of PaymentGateway", SUBCLASSES, "PaymentGateway"),
    ("classes that extend BaseView", SUBCLASSES, "BaseView"),
    ("what breaks if I change CatalogService", IMPACT, "CatalogService"),
    ("blast radius of charge_order", IMPACT, "charge_order"),
    ("is it safe to remove LegacyAdapter", IMPACT, "LegacyAdapter"),
    ("impact of modifying the billing module", IMPACT, "billing"),
    ("who owns the catalog-api", OWNERS, "catalog-api"),
    ("who knows about payment-gateway", OWNERS, "payment-gateway"),
    ("who is the SME for billing-api", OWNERS, "billing-api"),
    ("explain the order-service architecture", EXPLAIN, "order-service"),
    ("how does CatalogService work", EXPLAIN, "CatalogService"),
    ("what is the billing-service", EXPLAIN, "billing-service"),
    ("give me an overview of auth-service", EXPLAIN, "auth-service"),
    ("where do we validate the tenant header", SEARCH, None),
    ("find the code that parses ISO timestamps", SEARCH, None),
    ("logic for refunding a payment", SEARCH, None),
]


@pytest.mark.parametrize("question,route,target", _CASES)
def test_route_and_target(question, route, target):
    r, t = classify(question)
    assert r == route, f"{question!r} routed to {r}, want {route}"
    if target is not None:
        assert t == target, f"{question!r} target {t!r}, want {target!r}"


def test_classifier_route_accuracy_is_perfect_on_the_golden_set():
    # A single aggregate gate: routing regressions surface as this number dropping.
    hits = sum(1 for q, r, _ in _CASES if classify(q)[0] == r)
    assert hits == len(_CASES), f"routing {hits}/{len(_CASES)}"


def test_backticked_span_wins_as_target():
    assert extract_target("what about `Foo.bar.baz` then") == "Foo.bar.baz"


def test_unmatched_question_falls_back_to_search():
    assert classify("thanks, that's all")[0] == SEARCH


def test_target_none_when_no_symbol_present():
    assert extract_target("who calls it") is None


# A question with an ordinary second sentence. _IDENT accepts '.', so the final
# word plus its full stop ("line.", "caller.") looked more like a dotted symbol
# than the real subject did, and last-wins then picked it out of the wrong
# sentence entirely. Both halves are pinned here: the punctuation, and which
# sentence the subject is taken from.
_TRAILING_SENTENCE_CASES = [
    ("Where is mark_indexed defined? Give the file and line.", DEFINITION, "mark_indexed"),
    ("Who calls classify? List every caller.", CALLERS, "classify"),
    ("Where is CatalogService defined? Give the file.", DEFINITION, "CatalogService"),
    ("What breaks if I change charge_order? Be thorough.", IMPACT, "charge_order"),
    ("Who owns catalog-api? Name the team.", OWNERS, "catalog-api"),
]


@pytest.mark.parametrize("question,route,target", _TRAILING_SENTENCE_CASES)
def test_a_second_sentence_does_not_steal_the_target(question, route, target):
    r, t = classify(question)
    assert r == route, f"{question!r} routed to {r}, want {route}"
    assert t == target, f"{question!r} target {t!r}, want {target!r}"


def test_a_trailing_full_stop_is_not_part_of_the_symbol():
    assert extract_target("who calls charge_order.") == "charge_order"


def test_a_dotted_path_still_survives_sentence_splitting():
    # the sentence splitter must require whitespace after the terminator, or it
    # would cut "Store.close" in half
    assert extract_target("where is Store.close defined") == "Store.close"
    assert classify("Who calls Store.close? Be brief.")[1] == "Store.close"
