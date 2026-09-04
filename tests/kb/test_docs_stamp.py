"""Which commit a generated document describes, and why an absent one is not a blank.

A generated page is a claim about source at a moment. Strip the moment and a page describing
code that changed months ago is indistinguishable from a current one, while reading exactly
as authoritative. The wiki has carried its commit for this reason since it was built; the API
reference and the design notes did not, which was survivable while only a human opened them
and stops being survivable the moment anything else can read them.

The failure this guards is quiet in both directions: a consumer that cannot find a stamp must
treat the page as stale, and a consumer that finds `unknown` must not read it as fresh.
"""

from __future__ import annotations

import pytest

from contextlake.kb.docs.api import render_api_reference
from contextlake.kb.docs.design import render_design_document
from contextlake.kb.docs.stamp import UNKNOWN, read_stamp, stamp
from contextlake.kb.store.shards import GraphShard

REPO = "acme/stations"
SHA = "d318b683471101618febed18996405ad26462110"


def _text(kind, repo, commit):
    return "\n".join(stamp(kind, repo, commit))


def test_a_stamp_round_trips():
    assert read_stamp(_text("api", REPO, SHA)) == ("api", REPO, SHA)


def test_an_unstamped_page_reads_as_none_not_as_a_guess():
    """`None` is the signal a consumer turns into "stale".

    A page written before stamping existed, or by something else entirely, has no commit to
    report. Returning a plausible default here would make every such page look current.
    """
    assert read_stamp("# A page\n\nSome prose about code.\n") is None
    assert read_stamp("") is None


def test_an_absent_commit_is_recorded_as_unknown_rather_than_omitted():
    """The difference between "nothing to report" and "checked, could not determine".

    Omitting the field would make the marker unparseable and send a consumer down the
    same path as an unstamped page. Writing `unknown` keeps the page machine-readable and
    says the thing that is true, and the human sentence says it too rather than quietly
    printing a blank where a commit belongs.
    """
    text = _text("design", REPO, None)
    assert read_stamp(text) == ("design", REPO, UNKNOWN)
    assert "unknown commit" in text
    assert "no way to tell whether this still describes the code" in text


@pytest.mark.parametrize("hostile", [
    "acme/stations commit=deadbeef",   # a space plus a second commit= would shift the capture
    "acme/stations>",                  # closing the comment early
    "acme/<script>",
    "  ",                            # nothing but whitespace
])
def test_a_repo_id_cannot_rewrite_the_marker_grammar(hostile):
    """The id lands in an attribute-like position, so its own characters are the risk.

    A space in a repo id would let a crafted id inject a second `commit=` and have the
    parser read THAT instead of the real one: a page reporting a commit its generator never
    wrote. Neutralised at write time rather than trusted, and the parse still has to agree
    with the real commit afterwards.
    """
    got = read_stamp(_text("api", hostile, SHA))
    assert got is not None, "the marker stopped parsing entirely"
    kind, repo, commit = got
    assert commit == SHA, "a crafted repo id displaced the real commit"
    assert " " not in repo and ">" not in repo and "<" not in repo


def _shard(commit):
    return GraphShard(repo=REPO, head_commit=commit, parser_version="8", nodes=[], edges=[])


@pytest.mark.parametrize("render,kind", [
    (render_api_reference, "api"),
    (render_design_document, "design"),
])
def test_both_generated_documents_carry_their_commit(render, kind):
    """Neither document is served honestly without this, so both are checked, not one.

    Adding a third document type and forgetting its stamp would be invisible: the page would
    render, look complete, and report as current forever.
    """
    page = render(_shard(SHA), repo_id=REPO)
    assert read_stamp(page) == (kind, REPO, SHA)
    assert f"commit `{SHA}`" in page


def test_the_design_page_two_markers_do_not_confuse_each_other():
    """The design notes carry a status marker AND a provenance marker.

    They are different facts with a similar shape, and a reader looking for one must not
    match the other. The status marker sits first in the document, so a loose pattern would
    find it and return nothing useful.
    """
    page = render_design_document(_shard(SHA), repo_id=REPO)
    assert "status=proposed-never-ratified" in page
    assert read_stamp(page) == ("design", REPO, SHA)
