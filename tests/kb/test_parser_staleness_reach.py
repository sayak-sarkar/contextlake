"""A parser bump must invalidate every derived artefact, not just the graph.

`kb index` has always been parser-aware. The artefacts built ON TOP of the graph were
keyed on the repo's commit alone, so a parser bump refreshed the graph while embeddings,
the wiki page and cluster pages kept reporting themselves fresh. That is the project's
worst failure shape: not a crash, a confident answer built from a graph that no longer
exists.

The vectors are the sharpest case, because they are keyed by node id. When ids change,
stale rows name nodes the graph no longer holds, those hits get dropped at query time,
and the caller receives a shorter and entirely plausible answer.
"""

import re

from contextlake.kb.wiki.cluster import cluster_fingerprint
from contextlake.kb.wiki.generate import provenance_footer

# The exact expression four separate readers use to pull the commit out of a page:
# the MCP server, the HTML renderer, the dashboard mutations, and the wiki command's
# own freshness skip. Pinned here because the parser stamp was added to this same
# footer, and putting it inside the backticks would have silently changed all four.
_COMMIT_RE = re.compile(r"at commit `([^`]+)`")


def _brief(**over):
    base = {"repo": "team/svc", "head": "abc123", "parser_version": "4",
            "files": [], "grounded_count": None, "coverage_total": 0}
    base.update(over)
    return base


def test_footer_records_the_parser_that_built_the_graph():
    footer = provenance_footer(_brief())
    assert "(parser 4)" in footer


def test_footer_stamp_does_not_disturb_the_commit_readers():
    footer = provenance_footer(_brief())
    assert _COMMIT_RE.search(footer).group(1) == "abc123"


def test_footer_omits_the_stamp_when_the_shard_has_none():
    """An unstamped shard must not render `(parser None)` into a user-visible page."""
    footer = provenance_footer(_brief(parser_version=None))
    assert "parser" not in footer
    assert _COMMIT_RE.search(footer).group(1) == "abc123"


def test_wiki_skip_regex_reads_the_stamp_back():
    """The skip in `cmds/wiki.py` matches on this shape; a page written before the
    stamp existed yields no match, which reads as unknown and therefore as stale."""
    stamped = provenance_footer(_brief())
    m = re.search(r"at commit `[^`]+` \(parser ([^)]+)\)", stamped)
    assert m and m.group(1) == "4"

    unstamped = provenance_footer(_brief(parser_version=None))
    assert re.search(r"at commit `[^`]+` \(parser ([^)]+)\)", unstamped) is None


def test_an_unstamped_shard_falls_back_to_the_commit_question():
    """The regression guard for a loop this change nearly shipped.

    When the SHARD carries no parser version, nothing can be established about which
    parser built it. Demanding a match there would regenerate that page on EVERY run
    forever instead of once, because the page it writes cannot carry a stamp either.
    So an unstamped shard asks the commit-only question it always asked.

    This mirrors the expression in `cmds/wiki.py`; six suite tests failed on the first
    version of it, which is how the loop was caught before it shipped.
    """
    page_without_stamp = provenance_footer(_brief(parser_version=None))
    pm = re.search(r"at commit `[^`]+` \(parser ([^)]+)\)", page_without_stamp)

    def skips(shard_parser):
        return shard_parser is None or (pm is not None and pm.group(1) == shard_parser)

    assert skips(None) is True, "unstamped shard must not loop"
    assert skips("4") is False, "a stamped shard regenerates the unstamped page once"


def test_cluster_fingerprint_moves_when_only_the_parser_moves():
    """No member commit changes here. Only the parser does, and the page must rebuild."""
    heads = {"team/a": "aaa", "team/b": "bbb"}
    before = cluster_fingerprint({"heads": heads, "parsers": {"team/a": "3", "team/b": "3"}})
    after = cluster_fingerprint({"heads": heads, "parsers": {"team/a": "4", "team/b": "4"}})
    assert before != after


def test_cluster_fingerprint_still_moves_when_a_commit_moves():
    """The original signal must survive the addition -- this is the regression guard
    for keying on the wrong field instead of on both."""
    parsers = {"team/a": "4", "team/b": "4"}
    before = cluster_fingerprint({"heads": {"team/a": "aaa", "team/b": "bbb"}, "parsers": parsers})
    after = cluster_fingerprint({"heads": {"team/a": "aaa", "team/b": "ccc"}, "parsers": parsers})
    assert before != after


def test_embedded_parser_version_round_trips(tmp_path):
    from contextlake.kb.embeddings.store import (
        VectorStore,
        get_embedded_parser_version,
        set_embedded_parser_version,
    )
    vs = VectorStore(tmp_path / "vec.sqlite")
    assert get_embedded_parser_version(vs, "team/svc") is None, "never embedded is unknown"
    set_embedded_parser_version(vs, "team/svc", "4")
    assert get_embedded_parser_version(vs, "team/svc") == "4"
    # Unknown must never compare equal to a real version, or the incremental skip
    # would treat pre-stamp vectors as current and never re-embed them.
    assert get_embedded_parser_version(vs, "other/repo") != "4"
