"""A retrieved node is only useful if its citation is real.

Every retrieval metric this project had answered one question: did the right node
come back? None of them asked whether the `file:line` that node carries still points
at that symbol -- and the citation is the whole product. An agent is not shown a node
id, it is told to go read `src/thing.cpp:412`. A citation that is wrong looks exactly
like an answer, which makes it worse than a miss.

The three-outcome split is the load-bearing part of these tests. "Unverifiable" (the
repository has no local checkout, so nothing could be opened) must not collapse into
either verified or broken: as broken it invents defects on any machine without the
mirror, as verified it reports a clean bill of health for checks that never ran. That
is this project's signature failure mode -- a number that looks measured and is not --
so it gets a test of its own rather than a comment.
"""

from contextlake.kb.eval import (
    GoldenQuery,
    citation_summary,
    evaluate,
    verify_citations,
)
from contextlake.kb.model import Node, Repo
from contextlake.kb.store.sqlite_store import SqliteStore

SRC = """\
#include <string>

// a comment line
void
Widget::Draw(int n)
{
    return;
}
"""


def _store(tmp_path, *, checkout=True):
    """A one-file repo whose node lines are deliberately varied: one exact hit, one
    that needs the window, and several that cannot resolve.

    ``checkout=False`` records a path that is not on this machine, which is the real
    shape of the unverifiable case -- `Repo.path` is a required string, so a store
    always claims a clone location; whether it exists locally is the variable."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "widget.cpp").write_text(SRC, encoding="utf-8")
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="demo",
                           path=str(root if checkout else tmp_path / "not-cloned-here"),
                           host=None, default_branch="main", head_commit="abc123"))
    nodes = [
        # name sits on line 5, and line_start points at the return type on line 4:
        # the real shape of an out-of-line C++ definition.
        Node(id="n_draw", repo="demo", kind="method", name="Draw",
             file="widget.cpp", line_start=4, line_end=8, lang="cpp"),
        Node(id="n_include", repo="demo", kind="function", name="include",
             file="widget.cpp", line_start=1, line_end=1, lang="cpp"),
        # the file is 8 lines long
        Node(id="n_past_eof", repo="demo", kind="function", name="Draw",
             file="widget.cpp", line_start=900, line_end=901, lang="cpp"),
        Node(id="n_wrong_line", repo="demo", kind="function", name="Draw",
             file="widget.cpp", line_start=1, line_end=1, lang="cpp"),
        Node(id="n_gone", repo="demo", kind="function", name="Draw",
             file="deleted.cpp", line_start=1, line_end=2, lang="cpp"),
        Node(id="n_file", repo="demo", kind="file", name="widget.cpp",
             file="widget.cpp", line_start=None, line_end=None, lang="cpp"),
        Node(id="n_nofile", repo="demo", kind="class", name="Widget",
             file=None, line_start=None, line_end=None, lang="cpp"),
    ]
    store.upsert_nodes("demo", nodes)
    return store


def _by_id(checks):
    return {c.node_id: c for c in checks}


def test_a_name_a_couple_of_lines_below_line_start_still_verifies(tmp_path):
    """The window exists for a measured reason, not as slack. `line_start` is the
    start of the *construct*: a C++ return type on its own line, or a Python
    decorator, puts the name one or two lines later. Without the window every
    out-of-line C++ definition would read as a broken citation, and a checker whose
    false positives outnumber its findings gets switched off."""
    store = _store(tmp_path)
    got = _by_id(verify_citations(store, ["n_draw"]))
    assert got["n_draw"].status == "verified"
    assert got["n_draw"].cite == "widget.cpp:4"


def test_the_window_does_not_stretch_to_swallow_a_wrong_line(tmp_path):
    """`n_wrong_line` claims line 1 for a symbol defined on line 5. Four lines away
    with a window of two: this is the case that separates a check from a rubber
    stamp."""
    store = _store(tmp_path)
    got = _by_id(verify_citations(store, ["n_wrong_line"]))
    assert got["n_wrong_line"].status == "broken"
    assert got["n_wrong_line"].reason == "name_absent"


def test_each_way_a_citation_can_fail_is_named(tmp_path):
    """One reason per failure mode, because "broken: 4" tells you nothing about
    whether the index is stale, the parser is wrong, or the file was deleted."""
    store = _store(tmp_path)
    got = _by_id(verify_citations(
        store, ["n_past_eof", "n_gone", "n_nofile", "n_missing_entirely"]))
    assert got["n_past_eof"].reason == "line_out_of_range"
    assert got["n_gone"].reason == "file_missing"
    assert got["n_nofile"].reason == "no_citation"
    assert got["n_missing_entirely"].reason == "node_missing"
    assert all(c.status == "broken" for c in got.values())


def test_a_file_node_is_verified_by_the_path_since_it_has_no_name_to_find(tmp_path):
    store = _store(tmp_path)
    got = _by_id(verify_citations(store, ["n_file"]))
    assert got["n_file"].status == "verified"


def test_no_checkout_is_unverifiable_and_never_counted_as_either(tmp_path):
    """The test that keeps the harness honest on a machine without the mirror."""
    store = _store(tmp_path, checkout=False)
    checks = verify_citations(store, ["n_draw", "n_wrong_line", "n_file"])
    assert {c.status for c in checks} == {"unverifiable"}
    assert {c.reason for c in checks} == {"checkout_missing"}
    s = citation_summary(checks)
    assert s["unverifiable"] == 3
    assert s["verified"] == 0 and s["broken"] == 0
    # None, not 0.0 and not 1.0: nothing was checked, so there is no rate to report.
    assert s["verified_rate"] is None


def test_the_rate_is_over_checkable_nodes_only(tmp_path):
    """A repo without a checkout must not drag the rate down, or up."""
    store = _store(tmp_path)
    store.upsert_repo(Repo(id="nowhere", path=str(tmp_path / "absent"),
                           host=None, default_branch="main", head_commit="def456"))
    store.upsert_nodes("nowhere", [
        Node(id="n_elsewhere", repo="nowhere", kind="function", name="f",
             file="a.py", line_start=1, line_end=1, lang="python")])
    s = citation_summary(verify_citations(
        store, ["n_draw", "n_wrong_line", "n_elsewhere"]))
    assert (s["verified"], s["broken"], s["unverifiable"]) == (1, 1, 1)
    assert s["verified_rate"] == 0.5


def test_evaluate_counts_a_node_once_however_many_queries_returned_it(tmp_path):
    """Otherwise one popular symbol cited by every query decides the whole rate."""
    store = _store(tmp_path)

    def retriever(query, k, kind=None, repo=None):
        return ["n_draw", "n_wrong_line"]

    golden = [GoldenQuery(query="draw", expected=["Draw"], match="name"),
              GoldenQuery(query="draw again", expected=["Draw"], match="name")]
    res = evaluate(store, golden, k=10, retriever=retriever, verify=True)
    assert res["citations"]["checked"] == 2      # not 4
    assert res["citations"]["verified_rate"] == 0.5
    # the per-query view keeps its own copy, so a single bad query is still findable
    assert res["per_query"][0]["citations"]["broken"] == 1


def test_verification_is_off_unless_asked_for(tmp_path):
    """It does filesystem work per result and needs the mirror; it cannot be the
    default, and its absence must be visible rather than silently reported as 1.0."""
    store = _store(tmp_path)

    def retriever(query, k, kind=None, repo=None):
        return ["n_draw"]

    res = evaluate(store, [GoldenQuery(query="draw", expected=["Draw"], match="name")],
                   retriever=retriever)
    assert "citations" not in res
    assert "citations" not in res["per_query"][0]


def test_a_huge_file_is_not_read_past_the_cited_line(tmp_path, monkeypatch):
    """A generated header or god-file in a legacy tree runs to tens of thousands of
    lines. Verifying line 3 must cost three lines, or the checker becomes the slowest
    thing in the run and stops being used."""
    from contextlake.kb import eval as kb_eval

    root = tmp_path / "big"
    root.mkdir()
    (root / "gen.h").write_text("\n".join(f"#define M{i} {i}" for i in range(50_000)),
                                encoding="utf-8")
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="big", path=str(root), host=None,
                           default_branch="main", head_commit="a1"))
    store.upsert_nodes("big", [Node(id="n_m3", repo="big", kind="macro", name="M3",
                                    file="gen.h", line_start=4, line_end=4, lang="cpp")])

    seen = {}
    real = kb_eval._read_upto

    def spy(path, upto):
        seen["upto"] = upto
        return real(path, upto)

    monkeypatch.setattr(kb_eval, "_read_upto", spy)
    assert verify_citations(store, ["n_m3"])[0].status == "verified"
    assert seen["upto"] == 6      # line_start + window, not 50,000
