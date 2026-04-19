"""Golden-query evaluation harness for retrieval quality.

Define a small labelled set of ``query -> expected nodes`` and run it through any
retriever (FTS search, semantic, hybrid) to get **precision@k / recall@k / MRR**,
so a retrieval change (embed-bodies, reranking, the future ``ask`` router) is
*falsifiable* rather than vibes — a regression shows up as a number dropping.

Stdlib-only; the golden set is plain JSON:

    {"queries": [
      {"query": "order service", "expected": ["demo_app_orderservice"]},
      {"query": "charge a card", "expected": ["charge"], "match": "name", "kind": "function"}
    ]}

``match`` is ``"id"`` (default — compare against node ids) or ``"name"`` (compare
against node names, handy when ids are path-derived and unstable).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .store.base import Store

# A retriever maps (store, query, k, kind, repo) -> a ranked list of node ids.
Retriever = Callable[..., list]


@dataclass
class GoldenQuery:
    query: str
    expected: list  # node ids, or names when match == "name"
    kind: str | None = None
    repo: str | None = None
    match: str = "id"  # "id" | "name"


def load_golden(path) -> list[GoldenQuery]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [GoldenQuery(**q) for q in data["queries"]]


def fts_retriever(store: Store, query: str, k: int,
                  kind: str | None = None, repo: str | None = None) -> list:
    """The always-available baseline: the store's full-text search."""
    return [n.id for n in store.search(query, kind=kind, repo=repo, limit=k)]


def _keys(retrieved: list, gq: GoldenQuery, store: Store) -> list:
    if gq.match == "name":
        return [(n.name if (n := store.get_node(nid)) else nid) for nid in retrieved]
    return list(retrieved)


def precision_at_k(retrieved_keys: list, expected: list, k: int) -> float:
    topk = retrieved_keys[:k]
    if not topk:
        return 0.0
    exp = set(expected)
    return sum(1 for r in topk if r in exp) / len(topk)


def recall_at_k(retrieved_keys: list, expected: list, k: int) -> float:
    if not expected:
        return 0.0
    topk = set(retrieved_keys[:k])
    return sum(1 for e in set(expected) if e in topk) / len(set(expected))


def reciprocal_rank(retrieved_keys: list, expected: list) -> float:
    exp = set(expected)
    for i, r in enumerate(retrieved_keys, 1):
        if r in exp:
            return 1.0 / i
    return 0.0


def evaluate(store: Store, golden: list[GoldenQuery], *, k: int = 10,
             retriever: Retriever = fts_retriever) -> dict:
    """Run every golden query and aggregate precision@k / recall@k / MRR."""
    per = []
    for gq in golden:
        # fetch a few extra so recall isn't capped by k when expected has many ids
        retrieved = retriever(store, gq.query, max(k, len(gq.expected)), gq.kind, gq.repo)
        keys = _keys(retrieved, gq, store)
        per.append({
            "query": gq.query,
            "precision@k": precision_at_k(keys, gq.expected, k),
            "recall@k": recall_at_k(keys, gq.expected, k),
            "rr": reciprocal_rank(keys, gq.expected),
            "hit": reciprocal_rank(keys, gq.expected) > 0,
        })
    n = len(per) or 1
    return {
        "k": k,
        "n": len(per),
        "precision@k": round(sum(p["precision@k"] for p in per) / n, 4),
        "recall@k": round(sum(p["recall@k"] for p in per) / n, 4),
        "mrr": round(sum(p["rr"] for p in per) / n, 4),
        "hit_rate": round(sum(1 for p in per if p["hit"]) / n, 4),
        "per_query": per,
    }
