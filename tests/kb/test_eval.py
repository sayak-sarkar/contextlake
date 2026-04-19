import json

import pytest

from contextlake.kb.eval import (
    GoldenQuery, evaluate, fts_retriever, load_golden,
    precision_at_k, recall_at_k, reciprocal_rank,
)
from contextlake.kb.model import Node
from contextlake.kb.store.sqlite_store import SqliteStore


def test_metric_primitives():
    assert precision_at_k(["a", "b", "c"], ["a", "c"], 3) == pytest.approx(2 / 3)
    assert recall_at_k(["a", "b"], ["a", "c"], 2) == 0.5
    assert reciprocal_rank(["x", "a"], ["a"]) == 0.5
    assert reciprocal_rank(["x", "y"], ["a"]) == 0.0
    assert precision_at_k([], ["a"], 3) == 0.0          # no results
    assert recall_at_k(["a"], [], 1) == 0.0             # no expected


def test_evaluate_aggregates_with_a_stub_retriever():
    golden = [GoldenQuery("q1", ["a"]), GoldenQuery("q2", ["z"])]

    def stub(store, query, k, kind=None, repo=None):
        return {"q1": ["a", "b"], "q2": ["b", "c"]}[query]  # q1 hits @1, q2 misses

    r = evaluate(None, golden, k=2, retriever=stub)
    assert r["n"] == 2 and r["k"] == 2
    assert r["mrr"] == pytest.approx(0.5)               # (1.0 + 0.0) / 2
    assert r["hit_rate"] == 0.5
    assert r["precision@k"] == pytest.approx(0.25)      # (0.5 + 0.0) / 2
    assert r["recall@k"] == 0.5                         # (1.0 + 0.0) / 2


def test_load_golden(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"queries": [
        {"query": "x", "expected": ["a"]},
        {"query": "y", "expected": ["b"], "match": "name", "kind": "function"},
    ]}))
    g = load_golden(p)
    assert len(g) == 2
    assert g[0].query == "x" and g[0].match == "id"
    assert g[1].match == "name" and g[1].kind == "function"


def test_fts_retriever_scores_real_search(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_nodes("r", [
        Node(id="os", repo="r", kind="class", name="OrderService"),
        Node(id="bh", repo="r", kind="class", name="BaggageHandler"),
    ])
    r = evaluate(s, [GoldenQuery("OrderService", ["os"])], k=5, retriever=fts_retriever)
    assert r["hit_rate"] == 1.0          # search finds the node we asked for
    assert r["mrr"] > 0.0
    s.close()


def test_match_by_name(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_nodes("r", [Node(id="n1", repo="r", kind="function", name="charge")])
    # expected by NAME ("charge"), not id — harness resolves retrieved ids -> names
    r = evaluate(s, [GoldenQuery("charge", ["charge"], match="name")], k=5,
                 retriever=fts_retriever)
    assert r["hit_rate"] == 1.0
    s.close()
