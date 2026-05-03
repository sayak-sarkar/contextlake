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

# A retriever maps (query, k, kind, repo) -> a ranked list of node ids. It closes
# over whatever it needs (store, vector store, embedder) — built by the make_*
# factories below — so semantic/hybrid retrievers are scorable, not just FTS.
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


def make_fts_retriever(store: Store) -> Retriever:
    """The always-available baseline: the store's full-text search."""
    def _retrieve(query, k, kind=None, repo=None):
        return [n.id for n in store.search(query, kind=kind, repo=repo, limit=k)]
    return _retrieve


def make_semantic_retriever(store, vector_store, embedder) -> Retriever:
    """Pure embedding search (kind is ignored — vectors aren't kind-filtered)."""
    def _retrieve(query, k, kind=None, repo=None):
        vec = embedder.embed([query])[0]
        return [nid for nid, _score in vector_store.search(vec, k=k, repo=repo)]
    return _retrieve


def make_hybrid_retriever(store, vector_store, embedder) -> Retriever:
    """Semantic seed + Personalized-PageRank rerank over the graph."""
    from .embeddings.hybrid import hybrid_search

    def _retrieve(query, k, kind=None, repo=None):
        ranked = hybrid_search(store, vector_store, embedder, query, k=k, repo=repo)
        return [nid for nid, _score in ranked]
    return _retrieve


def _est_tokens(node) -> int:
    """Rough token cost (~chars/4) of surfacing one node to an agent's context."""
    parts = [node.kind or "", node.qualified_name or node.name or "", node.file or ""]
    sig = getattr(node, "signature", None)  # present once embed-bodies lands
    if sig:
        parts.append(sig)
    return max(1, len(" ".join(parts)) // 4)


def _result_tokens(store, ids: list) -> int:
    """Estimated token cost of returning these node ids — the price of the answer."""
    total = 0
    for nid in ids:
        n = store.get_node(nid)
        if n:
            total += _est_tokens(n)
    return total


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
             retriever: Retriever | None = None) -> dict:
    """Run every golden query and aggregate precision@k / recall@k / MRR — plus a
    **cost** dimension (estimated tokens to return the answer, and precision per
    1k tokens), so "route to the cheapest sufficient source" becomes measurable.

    ``retriever`` defaults to the FTS baseline (``make_fts_retriever(store)``).
    """
    if retriever is None:
        retriever = make_fts_retriever(store)
    per = []
    for gq in golden:
        # fetch a few extra so recall isn't capped by k when expected has many ids
        retrieved = retriever(gq.query, max(k, len(gq.expected)), gq.kind, gq.repo)
        keys = _keys(retrieved, gq, store)
        rr = reciprocal_rank(keys, gq.expected)
        tokens = _result_tokens(store, retrieved[:k]) if store is not None else 0
        per.append({
            "query": gq.query,
            "precision@k": precision_at_k(keys, gq.expected, k),
            "recall@k": recall_at_k(keys, gq.expected, k),
            "rr": rr,
            "hit": rr > 0,
            "est_tokens": tokens,
        })
    n = len(per) or 1
    mean_prec = sum(p["precision@k"] for p in per) / n
    mean_tokens = sum(p["est_tokens"] for p in per) / n
    return {
        "k": k,
        "n": len(per),
        "precision@k": round(mean_prec, 4),
        "recall@k": round(sum(p["recall@k"] for p in per) / n, 4),
        "mrr": round(sum(p["rr"] for p in per) / n, 4),
        "hit_rate": round(sum(1 for p in per if p["hit"]) / n, 4),
        "est_tokens_per_query": round(mean_tokens, 1),
        # precision bought per 1k tokens spent — higher is a cheaper, sharper source
        "precision_per_1k_tokens": (round(mean_prec / (mean_tokens / 1000), 4)
                                    if mean_tokens else 0.0),
        "per_query": per,
    }
