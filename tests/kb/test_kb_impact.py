"""Reverse blast-radius / change-impact (`impact` verb + shared blast_radius)."""

import json
from datetime import date

import pytest

from contextlake.cli import main
from contextlake.kb.impact import blast_radius, resolve_target
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))


def _edge(src, dst):
    return Edge(src=src, dst=dst, relation="calls",
                confidence=Confidence.EXTRACTED, provenance=_PROV)


def _store(tmp_path):
    """A -calls-> S, B -calls-> A, C -calls-> B (a 3-deep reverse chain into S)."""
    s = SqliteStore(tmp_path / "index.sqlite")
    s.upsert_nodes("r", [Node(id=i, repo="r", kind="function", name=i)
                         for i in ("S", "A", "B", "C")])
    s.upsert_edges("r", [_edge("A", "S"), _edge("B", "A"), _edge("C", "B")])
    return s


def test_blast_radius_walks_incoming_to_depth(tmp_path):
    s = _store(tmp_path)
    try:
        hits, trunc = blast_radius(s, "S", hops=2)
        assert {h.name for h in hits} == {"A", "B"}     # C is hop 3, beyond hops=2
        assert not trunc
        assert next(h.hop for h in hits if h.name == "A") == 1
        assert next(h.hop for h in hits if h.name == "B") == 2
    finally:
        s.close()


def test_blast_radius_respects_limit_and_reports_truncation(tmp_path):
    s = _store(tmp_path)
    try:
        hits, trunc = blast_radius(s, "S", hops=5, limit=1)
        assert len(hits) == 1 and trunc
    finally:
        s.close()


def test_cmd_impact_cli_lists_dependents(tmp_path, capsys):
    _store(tmp_path).close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path}"\n')
    with pytest.raises(SystemExit) as e:
        main(["kb", "impact", "S", "--config", str(cfg), "--hops", "3"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "Impact of changing" in out and "A" in out


def _collision_store(tmp_path):
    """One repo, two definitions named `close`, and a caller of each. The caller
    edges are AMBIGUOUS and stamped with the candidate count the parser saw."""
    s = SqliteStore(tmp_path / "index.sqlite")
    s.upsert_nodes("r", [
        Node(id="py-close", repo="r", kind="method", name="close",
             file="src/db.py", line_start=40),
        Node(id="js-close", repo="r", kind="function", name="close",
             file="web/ui.js", line_start=113),
        Node(id="caller", repo="r", kind="function", name="shutdown",
             file="src/app.py", line_start=7),
    ])
    s.upsert_edges("r", [Edge(
        src="caller", dst="js-close", relation="calls",
        confidence=Confidence.AMBIGUOUS, context="ambiguous",
        attrs={"name_candidates": 2},
        provenance=Provenance(source_file="src/app.py", source_line=9,
                              verified_at=date(2026, 8, 5)))])
    return s


def _cfg(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path}"\n')
    return cfg


def test_cmd_impact_names_the_seed_it_chose_and_the_alternatives(tmp_path, capsys):
    # A bare name resolved to one of several definitions in silence, so an answer
    # about web/ui.js read exactly like one about the src/db.py the user meant.
    _collision_store(tmp_path).close()
    with pytest.raises(SystemExit) as e:
        main(["kb", "impact", "close", "--config", str(_cfg(tmp_path)), "--hops", "1"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "seed: function web/ui.js:113" in out          # which one it used
    assert "2 matched 'close'; used the first" in out     # that a choice was made
    assert "--node py-close" in out                       # and how to pick the other


def test_cmd_impact_json_distinguishes_hits_and_cites_the_call_site(tmp_path, capsys):
    # --json carried only hop/repo/kind/name/via/confidence, so two hits sharing a
    # name were indistinguishable and none could be opened.
    _collision_store(tmp_path).close()
    with pytest.raises(SystemExit) as e:
        main(["kb", "impact", "close", "--config", str(_cfg(tmp_path)),
              "--hops", "1", "--json"])
    assert e.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"]["file"] == "web/ui.js" and payload["target"]["line"] == 113
    assert payload["ambiguous"] is True
    assert [c["id"] for c in payload["other_definitions"]] == ["py-close"]
    hit = payload["affected"][0]
    assert hit["id"] == "caller"
    assert hit["file"] == "src/app.py" and hit["line"] == 7
    assert hit["via_file"] == "src/app.py" and hit["via_line"] == 9   # the call site
    assert hit["name_candidates"] == 2


def test_cmd_impact_quantifies_what_ambiguous_cost_this_answer(tmp_path, capsys):
    # "ambiguous" read identically on a hand-verified 11/11 answer and on one with
    # 282 false positives; the discriminating fact is how many definitions each
    # name-matched reference could have meant.
    _collision_store(tmp_path).close()
    with pytest.raises(SystemExit) as e:
        main(["kb", "impact", "close", "--config", str(_cfg(tmp_path)), "--hops", "1"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "1 of 2 same-name definitions" in out
    assert "1 of 1 hit(s) came from a reference matched by NAME" in out
    assert "via calls at src/app.py:9" in out


def test_cmd_impact_usage_error_without_target():
    with pytest.raises(SystemExit) as e:
        main(["kb", "impact"])
    assert e.value.code == 2


# -- resolve_target: exact-name-first, repo scoping, disambiguation -----------
def _multi_repo_store(tmp_path):
    """Two repos each defining a class named ``Node``; repo ``a`` also has a file
    named ``Node`` (must lose to the class) and a fuzzy-only symbol ``ForecastService``."""
    s = SqliteStore(tmp_path / "index.sqlite")
    s.upsert_nodes("a", [
        Node(id="a:node-class", repo="a", kind="class", name="Node", file="src/node.py"),
        Node(id="a:node-file", repo="a", kind="file", name="Node", file="src/node.py"),
        Node(id="a:fs", repo="a", kind="class", name="ForecastService", file="src/svc.py"),
    ])
    s.upsert_nodes("b", [
        Node(id="b:node-class", repo="b", kind="class", name="Node", file="lib/node.ts"),
    ])
    return s


def test_resolve_exact_node_id_wins(tmp_path):
    s = _multi_repo_store(tmp_path)
    try:
        node, cands = resolve_target(s, "a:node-class")
        assert node is not None and node.id == "a:node-class" and cands == []
    finally:
        s.close()


def test_resolve_ambiguous_name_returns_candidates(tmp_path):
    s = _multi_repo_store(tmp_path)
    try:
        node, cands = resolve_target(s, "Node")          # defined in repo a AND b
        assert node is None
        assert {c.repo for c in cands} == {"a", "b"}
    finally:
        s.close()


def test_resolve_repo_scope_disambiguates_and_prefers_source(tmp_path):
    s = _multi_repo_store(tmp_path)
    try:
        node, cands = resolve_target(s, "Node", repo="a")
        # repo scope narrows to one repo; the class beats the same-named file
        assert node is not None and node.id == "a:node-class" and cands == []
    finally:
        s.close()


def test_resolve_fuzzy_fallback_for_partial_name(tmp_path):
    s = _multi_repo_store(tmp_path)
    try:
        node, cands = resolve_target(s, "ForecastServ")   # no exact name -> FTS
        assert node is not None and node.name == "ForecastService" and cands == []
    finally:
        s.close()


def test_cmd_impact_ambiguous_name_lists_repos(tmp_path, capsys):
    _multi_repo_store(tmp_path).close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path}"\n')
    with pytest.raises(SystemExit) as e:
        main(["kb", "impact", "Node", "--config", str(cfg)])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "ambiguous" in out and "--repo a" in out and "--repo b" in out


def test_default_relations_includes_references():
    from contextlake.kb.impact import DEFAULT_RELATIONS
    assert "references" in DEFAULT_RELATIONS


def test_default_relations_includes_dataflow_reads_and_writes():
    """A table's blast radius must include the code that reads/writes it by
    default -- the whole point of tracking dataflow is answering "what breaks
    if I change this table" without the caller needing to know to pass
    --relation reads,writes."""
    from contextlake.kb.impact import DEFAULT_RELATIONS
    assert "reads" in DEFAULT_RELATIONS and "writes" in DEFAULT_RELATIONS


def test_blast_radius_finds_code_that_reads_a_table(tmp_path):
    s = SqliteStore(tmp_path / "index.sqlite")
    try:
        s.upsert_nodes("r", [Node(id="orders", repo="r", kind="table", name="orders"),
                             Node(id="dao.py", repo="r", kind="file", name="dao.py")])
        s.upsert_edges("r", [Edge(src="dao.py", dst="orders", relation="reads",
                                  confidence=Confidence.INFERRED, provenance=_PROV)])
        hits, _ = blast_radius(s, "orders", hops=2)
        assert {h.name for h in hits} == {"dao.py"}
        assert hits[0].via == "reads"
    finally:
        s.close()
