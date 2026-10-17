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
    content_terms,
    extract_target,
)

# (question, expected_route, expected_target-or-None-to-skip-target-check)
_CASES = [
    ("where is ForecastService defined", DEFINITION, "ForecastService"),
    ("definition of load_reading", DEFINITION, "load_reading"),
    ("who calls load_reading", CALLERS, "load_reading"),
    ("what calls StationClient", CALLERS, "StationClient"),
    ("callers of validate_token", CALLERS, "validate_token"),
    ("who uses the station-registry", CALLERS, "station-registry"),
    ("what depends on requests", DEPENDENTS, "requests"),
    ("which repos use shared-core", DEPENDENTS, "shared-core"),
    ("dependents of the readings package", DEPENDENTS, "readings"),
    ("who depends on payments-api", DEPENDENTS, "payments-api"),
    ("what does payments-api depend on", SEARCH, "payments-api"),
    ("what extends BaseController", SUBCLASSES, "BaseController"),
    ("subclasses of Embedder", SUBCLASSES, "Embedder"),
    ("who implements Store", SUBCLASSES, "Store"),
    ("implementations of SensorGateway", SUBCLASSES, "SensorGateway"),
    ("classes that extend BaseView", SUBCLASSES, "BaseView"),
    ("what breaks if I change ForecastService", IMPACT, "ForecastService"),
    ("blast radius of load_reading", IMPACT, "load_reading"),
    ("is it safe to remove LegacyAdapter", IMPACT, "LegacyAdapter"),
    ("impact of modifying the alerts module", IMPACT, "alerts"),
    ("who owns the forecast-api", OWNERS, "forecast-api"),
    ("who knows about sensor-gateway", OWNERS, "sensor-gateway"),
    ("who is the SME for alerts-api", OWNERS, "alerts-api"),
    ("explain the ingest-service architecture", EXPLAIN, "ingest-service"),
    ("how does ForecastService work", EXPLAIN, "ForecastService"),
    ("what is the alerts-service", EXPLAIN, "alerts-service"),
    ("give me an overview of station-registry", EXPLAIN, "station-registry"),
    ("where do we validate the tenant header", SEARCH, None),
    ("find the code that parses ISO timestamps", SEARCH, None),
    ("logic for backfilling a reading", SEARCH, None),
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


# "depends on" runs in two directions. The bare `depends on` in the DEPENDENTS rule sent
# "What does X depend on?" to find_dependents, which answers the OPPOSITE question (what
# depends on X) and labelled it answered. Outgoing questions have no graph route in `ask`
# and go to SEARCH; `repo_dependencies(direction="out")` is the tool that answers them.
_OUTGOING_DEPENDENCY_QUESTIONS = [
    "What does payments-api depend on?",
    "what does the billing service depend on",
    "Which packages does checkout-ui depend on?",
    "what else does payments-api depend on",
    "what do we depend on",
    "what packages do all services depend on",
    "how many packages does checkout-ui depend on?",
    "Who does payments-api depend on?",
    "What does payments-api depend on in production?",
    "What does payments-api depend on? Name the packages.",
]


@pytest.mark.parametrize("question", _OUTGOING_DEPENDENCY_QUESTIONS)
def test_what_x_depends_on_is_not_routed_to_dependents(question):
    route, _target = classify(question)
    assert route == SEARCH, f"{question!r} routed to {route}, want {SEARCH}"


# The positive control: the same words in the incoming direction keep their route. A rule
# that sent every "depend on" question to SEARCH would pass the cases above.
_INCOMING_DEPENDENCY_QUESTIONS = [
    ("who depends on payments-api", "payments-api"),
    ("Who depends on payments-api?", "payments-api"),
    ("which repos depend on payments-api", "payments-api"),
    ("which packages depend on requests", "requests"),
    ("what services depend on payments-api?", "payments-api"),
    ("what else depends on payments-api", "payments-api"),
    ("does anything depend on payments-api?", "payments-api"),
    ("do any repos depend on payments-api", "payments-api"),
    ("which repos do not depend on payments-api", "payments-api"),
    ("which of our repos still depend on requests", "requests"),
    ("Which repos depend on payments-api, and what does it do?", "payments-api"),
    ("what does payments-api do, and which repos depend on it", "payments-api"),
    ("which repos do we have that depend on payments-api", "payments-api"),
    ("which repos does the team own that depend on payments-api", "payments-api"),
]


@pytest.mark.parametrize("question,target", _INCOMING_DEPENDENCY_QUESTIONS)
def test_who_depends_on_x_still_routes_to_dependents(question, target):
    route, found = classify(question)
    assert route == DEPENDENTS, f"{question!r} routed to {route}, want {DEPENDENTS}"
    assert found == target, f"{question!r} target {found!r}, want {target!r}"


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
    ("Where is ForecastService defined? Give the file.", DEFINITION, "ForecastService"),
    ("What breaks if I change load_reading? Be thorough.", IMPACT, "load_reading"),
    ("Who owns forecast-api? Name the team.", OWNERS, "forecast-api"),
]


@pytest.mark.parametrize("question,route,target", _TRAILING_SENTENCE_CASES)
def test_a_second_sentence_does_not_steal_the_target(question, route, target):
    r, t = classify(question)
    assert r == route, f"{question!r} routed to {r}, want {route}"
    assert t == target, f"{question!r} target {t!r}, want {target!r}"


def test_a_trailing_full_stop_is_not_part_of_the_symbol():
    assert extract_target("who calls load_reading.") == "load_reading"


def test_a_dotted_path_still_survives_sentence_splitting():
    # the sentence splitter must require whitespace after the terminator, or it
    # would cut "Store.close" in half
    assert extract_target("where is Store.close defined") == "Store.close"
    assert classify("Who calls Store.close? Be brief.")[1] == "Store.close"


def test_content_terms_keeps_the_words_worth_probing():
    assert content_terms("Which repository implements the SAML SSO flow?") == [
        "SAML", "SSO", "flow"]
    assert content_terms("Summarise the readings service.") == ["readings"]


def test_content_terms_keeps_domain_words_that_stop_would_have_dropped():
    """_STOP drops impact / blast / radius / package because they are route
    keywords. Reusing it here left "blast radius impact analysis" with a single
    probe term, and refused a question the store could answer."""
    assert content_terms("blast radius impact analysis") == [
        "blast", "radius", "impact", "analysis"]


def test_content_terms_deduplicates_case_insensitively_and_keeps_order():
    assert content_terms("load_reading and Load_Reading") == ["load_reading"]
    assert content_terms("beta alpha beta") == ["beta", "alpha"]
