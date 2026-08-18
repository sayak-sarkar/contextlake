"""A malformed golden set must fail loudly, not score zero.

`kb eval --json` exists to gate CI on a metric. So a typo in the golden file that produces
`hit_rate: 0.0` is worse than a crash: it reads as "retrieval regressed to nothing", blocks a
release, and the numbers look measured rather than meaningless.

Two shapes did exactly that:

- an unrecognised `match` mode fell through every comparison;
- `expected` written as a bare string is iterable, so the scorer compared the retrieved ids
  against its individual CHARACTERS and naturally matched none.

An external reviewer reported this as "`kb eval` always reports 0.0 on every metric, the
feature is non-functional". That was not right -- the correct form scores `hit_rate: 1.0` --
but the complaint underneath it was, and this is the part that was real: nothing told the
reviewer their nine attempted spellings were being rejected rather than scored.
"""

from __future__ import annotations

import json

import pytest

from contextlake.kb.eval import MATCH_MODES, GoldenQuery, load_golden


def _write(tmp_path, payload):
    p = tmp_path / "golden.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_a_well_formed_golden_set_loads(tmp_path):
    """The guard must not reject what it is supposed to accept."""
    got = load_golden(_write(tmp_path, {"queries": [
        {"query": "Calculator", "expected": ["Calculator"], "match": "name"},
        {"query": "add", "expected": ["app.py::Calculator.add"]},
    ]}))
    assert [q.query for q in got] == ["Calculator", "add"]
    assert got[1].match == "id", "the default match mode changed"


@pytest.mark.parametrize("bad_match", ["contains", "exact", "NAME", "", "fuzzy"])
def test_an_unrecognised_match_mode_is_rejected(tmp_path, bad_match):
    """Every one of these was tried by a reviewer and silently scored zero.

    `NAME` is in the list deliberately: a case-only difference is the spelling somebody
    reaches for first, and it is exactly as unscorable as a wholly invented one.
    """
    with pytest.raises(ValueError, match="is not one of"):
        load_golden(_write(tmp_path, {"queries": [
            {"query": "q", "expected": ["x"], "match": bad_match}]}))


def test_expected_as_a_string_is_rejected_with_the_fix_in_the_message(tmp_path):
    """A string is iterable, which is why this scored zero instead of raising.

    The message names the correction, because the reader's next move is to edit the file and
    "expected must be a list" alone does not say what to write.
    """
    with pytest.raises(ValueError) as exc:
        load_golden(_write(tmp_path, {"queries": [
            {"query": "q", "expected": "Calculator", "match": "name"}]}))
    assert '["Calculator"]' in str(exc.value)


def test_an_empty_expected_list_is_rejected(tmp_path):
    """Counting it as a miss would drag the whole set's metrics down for an incomplete file."""
    with pytest.raises(ValueError, match="non-empty"):
        load_golden(_write(tmp_path, {"queries": [{"query": "q", "expected": []}]}))


def test_a_file_without_a_queries_key_says_what_the_shape_is(tmp_path):
    """A bare list is the obvious wrong guess, and the old error was a TypeError about
    list indices that told the reader nothing about the schema."""
    with pytest.raises(ValueError, match="queries"):
        load_golden(_write(tmp_path, [{"query": "q", "expected": ["x"]}]))


def test_the_match_modes_are_named_in_one_place():
    """The error message and the validation must not be able to disagree."""
    assert MATCH_MODES == ("id", "name")
    assert GoldenQuery(query="q", expected=["x"]).match in MATCH_MODES


# --- the second round of guards -------------------------------------------------
#
# Every one of these was found by an external reviewer reading the FIRST round of
# guards above, which is the point worth keeping: the fix for a defect class is the
# most likely place to find that same class again. Each case below parsed, scored, and
# returned a number that looked measured.


def test_an_empty_queries_list_is_rejected_not_scored_as_zero(tmp_path):
    """`n: 0` with every metric at 0.0 reads exactly like a real run that missed."""
    with pytest.raises(ValueError, match="non-empty list"):
        load_golden(_write(tmp_path, {"queries": []}))


@pytest.mark.parametrize("queries", [{}, "", "abc", 0, None])
def test_a_non_list_queries_value_is_rejected(tmp_path, queries):
    """A dict and a string are both iterable, so both reach the scorer intact."""
    with pytest.raises(ValueError, match="non-empty list"):
        load_golden(_write(tmp_path, {"queries": queries}))


def test_a_query_entry_that_is_not_an_object_names_its_index(tmp_path):
    with pytest.raises(ValueError, match=r"queries\[1\]"):
        load_golden(_write(tmp_path, {"queries": [
            {"query": "q", "expected": ["x"]}, "not-an-object"]}))


@pytest.mark.parametrize("bad", [0, None, ["nested"], 1.5, True])
def test_a_non_string_inside_expected_is_rejected(tmp_path, bad):
    """It can never equal a retrieved id or name, so it is a guaranteed miss."""
    with pytest.raises(ValueError, match="non-empty"):
        load_golden(_write(tmp_path, {"queries": [
            {"query": "q", "expected": [bad]}]}))


def test_a_blank_string_inside_expected_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="non-empty"):
        load_golden(_write(tmp_path, {"queries": [{"query": "q", "expected": ["  "]}]}))


@pytest.mark.parametrize("bad", ["", "   ", 0, None])
def test_an_empty_query_string_is_rejected(tmp_path, bad):
    """No terms retrieves nothing, and nothing scores as a retrieval failure."""
    with pytest.raises(ValueError, match="non-empty string"):
        load_golden(_write(tmp_path, {"queries": [
            {"query": bad, "expected": ["x"]}]}))


@pytest.mark.parametrize("field", ["kind", "repo"])
@pytest.mark.parametrize("bad", [False, [], {}, "", "   ", 0])
def test_a_falsy_filter_is_rejected_because_the_store_would_drop_it(tmp_path, field, bad):
    """`SqliteStore.search` tests `if kind:`, so a falsy filter runs UNFILTERED.

    That is the dangerous direction: the query can then score a HIT on a node the
    filter it appears to carry would have excluded.
    """
    with pytest.raises(ValueError, match="UNFILTERED"):
        load_golden(_write(tmp_path, {"queries": [
            {"query": "q", "expected": ["x"], field: bad}]}))


@pytest.mark.parametrize("field", ["kind", "repo"])
def test_an_omitted_or_null_filter_is_still_fine(tmp_path, field):
    """The guard must reject falsy-but-present, not absent. Absent is the normal case."""
    assert len(load_golden(_write(tmp_path, {"queries": [
        {"query": "q", "expected": ["x"], field: None}]}))) == 1
    assert len(load_golden(_write(tmp_path, {"queries": [
        {"query": "q", "expected": ["x"]}]}))) == 1


def test_a_fully_populated_entry_still_loads():
    """Every guard added above must let a legitimate, fully-specified query through."""
    q = GoldenQuery(query="Calculator", expected=["Calculator"],
                    kind="class", repo="svc", match="name")
    assert q.kind == "class" and q.repo == "svc" and q.match == "name"
