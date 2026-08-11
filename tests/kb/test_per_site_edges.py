"""Per-site ``calls`` edges, and the degree consumers that must not be fooled by them.

``calls`` stores one edge per occurrence in source, so "where is this called" can be
answered exhaustively instead of with one representative site. Two things then have to
hold, and neither is obvious from reading the parser alone:

1. **Only ``calls`` behaves this way.** The resolver is shared by six streams; retaining
   every mention of a base class or every reference to a table is a different question.
2. **Degree consumers count distinct pairs.** A raw row count answers "how many call
   sites" and then gets rendered beside a node as "N caller(s)" -- a number that is
   confidently wrong rather than merely missing.

The last test is the one that matters most: it is built so that row-counting and
distinct-counting produce *different orderings*, so it cannot pass by accident.
"""

from datetime import date

from contextlake.kb.model import (
    PER_SITE_RELATIONS,
    Confidence,
    Edge,
    Node,
    Provenance,
)
from contextlake.kb.parse import _resolve_name_refs, index_repo_dir
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.visualize.payload import repo_subgraph
from contextlake.kb.wiki.generate import repo_brief

# One caller, one callee, three invocations on three known lines. Distinct numbers so a
# failure says which site survived rather than just "wrong count".
_SOURCE = """\
int helper(int v) { return v + 1; }

int base_only(int v) { return v; }

int driver(int v) {
    int a = helper(v);
    int b = helper(a);
    int c = helper(b);
    return base_only(c);
}
"""
_HELPER_CALL_LINES = [6, 7, 8]


def _prov(line, file="a.cpp"):
    return Provenance(source_file=file, source_line=line, verified_at=date(2026, 8, 11))


def test_calls_keep_every_site(tmp_path):
    """The shipped path, not ``parse_source``: reference resolution is repo-wide and
    does not run in the per-file parser, so testing the wrong entry point here would
    report success for a change that does nothing."""
    (tmp_path / "a.cpp").write_text(_SOURCE)
    shard = index_repo_dir(str(tmp_path), "r")
    by_id = {n.id: n for n in shard.nodes}

    helper_lines = sorted(
        e.provenance.source_line for e in shard.edges
        if e.relation == "calls" and by_id[e.dst].name == "helper"
    )
    assert helper_lines == _HELPER_CALL_LINES, (
        "each invocation must contribute its own edge citing its own line")


def test_only_calls_is_per_site():
    """The resolver is shared. A stream that is not per-site must still collapse a
    repeated reference to one edge, keeping the lowest line."""
    nodes = {"base": Node(id="base", repo="r", kind="class", name="Base", file="b.h")}
    refs = [("child", "Base", "a.cpp", 30), ("child", "Base", "a.cpp", 10)]

    collapsed = _resolve_name_refs(
        refs, nodes, relation="inherits", target_kinds={"class"}, per_site=False)
    assert len(collapsed) == 1
    assert collapsed[0].provenance.source_line == 10, "keeps the earliest reference"

    retained = _resolve_name_refs(
        refs, nodes, relation="calls", target_kinds={"class"}, per_site=True)
    assert [e.provenance.source_line for e in retained] == [10, 30]

    assert "calls" in PER_SITE_RELATIONS


def test_tied_lines_are_ordered_deterministically():
    """Removing the pair de-duplication made tie order observable in shard output.

    Two references on the SAME line previously could not both survive, so their order
    never mattered; tree-sitter's capture order is not guaranteed, so sorting by line
    alone would make the shard non-deterministic -- an invariant this project has
    already had to repair once.
    """
    nodes = {
        "f": Node(id="f", repo="r", kind="function", name="alpha", file="x.cpp"),
        "g": Node(id="g", repo="r", kind="function", name="beta", file="x.cpp"),
    }
    one = [("caller", "alpha", "x.cpp", 5), ("caller", "beta", "x.cpp", 5)]
    other = list(reversed(one))

    def resolve(refs):
        return [(e.dst, e.provenance.source_line) for e in _resolve_name_refs(
            refs, nodes, relation="calls", target_kinds={"function"}, per_site=True)]

    assert resolve(one) == resolve(other), "input order must not reach the output"


def test_hub_count_reports_callers_not_call_sites(tmp_path):
    """A hub row renders as "N caller(s)". Three invocations from ONE caller is one
    caller, and reporting three would overstate the fan-in threefold."""
    nodes = [
        Node(id="target", repo="r", kind="function", name="Encode", file="codec.cpp"),
        Node(id="caller", repo="r", kind="function", name="Driver", file="a.cpp"),
    ]
    edges = [Edge(src="caller", dst="target", relation="calls",
                  confidence=Confidence.INFERRED, provenance=_prov(line))
             for line in _HELPER_CALL_LINES]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h", nodes=nodes, edges=edges))

    brief = repo_brief(tmp_path, "r")
    counts = {row["name"]: row["count"] for row in brief["hubs"]}
    assert counts["Encode"] == 1, "three call sites from one caller is one caller"


def test_subgraph_ranking_counts_distinct_callers(tmp_path):
    """Built so the two counting rules disagree on the ORDER, not merely the number.

    ``popular`` is called once each by three different callers (3 distinct, 3 rows).
    ``repeated`` is called five times by a single caller (1 distinct, 5 rows).
    Counting rows ranks ``repeated`` first; counting distinct pairs ranks ``popular``
    first, which is the honest answer to "what does this repo depend on most".
    """
    store = SqliteStore(tmp_path / "index.sqlite")
    nodes = [Node(id="popular", repo="r", kind="function", name="Popular", file="p.cpp"),
             Node(id="repeated", repo="r", kind="function", name="Repeated", file="q.cpp")]
    nodes += [Node(id=f"c{i}", repo="r", kind="function", name=f"C{i}", file=f"c{i}.cpp")
              for i in range(3)]
    store.upsert_nodes("r", nodes)

    edges = [Edge(src=f"c{i}", dst="popular", relation="calls",
                  confidence=Confidence.INFERRED, provenance=_prov(10 + i))
             for i in range(3)]
    edges += [Edge(src="c0", dst="repeated", relation="calls",
                   confidence=Confidence.INFERRED, provenance=_prov(50 + i))
              for i in range(5)]
    store.upsert_edges("r", edges)

    got, _ = repo_subgraph(store, "r", max_nodes=2)
    assert [n.id for n in got] == ["popular", "c0"], (
        "ranking must count distinct callers; counting rows would put 'repeated' first")
