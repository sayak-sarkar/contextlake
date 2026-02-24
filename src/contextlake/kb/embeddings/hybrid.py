"""HippoRAG-style hybrid retrieval: embedding seeds + Personalized PageRank.

Semantic search alone returns lexically/semantically similar nodes; it misses
structurally-related ones (a function's caller, a config's consumer). Hybrid
retrieval seeds Personalized PageRank with the embedding hits (weighted by
similarity) and propagates that relevance across the graph, so structurally
central neighbours surface even when their text doesn't match the query.

The PPR runs over a bounded subgraph (BFS-expanded from the seeds) to stay
tractable across a large workspace.
"""

from __future__ import annotations


def _expand(store, seeds, hops):
    """BFS an undirected subgraph out from ``seeds`` up to ``hops``."""
    visited = set(seeds)
    adjacency: dict[str, set[str]] = {}
    frontier = list(seeds)
    for _ in range(max(0, hops)):
        nxt = []
        for n in frontier:
            for e in store.neighbors(n, direction="both"):
                m = e.dst if e.src == n else e.src
                adjacency.setdefault(n, set()).add(m)
                adjacency.setdefault(m, set()).add(n)
                if m not in visited:
                    visited.add(m)
                    nxt.append(m)
        frontier = nxt
        if not frontier:
            break
    return visited, adjacency


def _normalize(personalization, nodes):
    total = sum(max(0.0, personalization.get(n, 0.0)) for n in nodes)
    if total <= 0:
        u = 1.0 / len(nodes)
        return {n: u for n in nodes}
    return {n: max(0.0, personalization.get(n, 0.0)) / total for n in nodes}


def _ppr(nodes, adjacency, personalization, damping=0.5, iters=20):
    """Personalized PageRank via power iteration over the (undirected) subgraph."""
    nodes = list(nodes)
    p = _normalize(personalization, nodes)
    r = dict(p)
    for _ in range(max(1, iters)):
        new = {n: (1.0 - damping) * p[n] for n in nodes}
        dangling = 0.0
        for n in nodes:
            nbrs = adjacency.get(n)
            if nbrs:
                share = damping * r[n] / len(nbrs)
                for m in nbrs:
                    new[m] += share
            else:
                dangling += r[n]
        if dangling:
            for n in nodes:
                new[n] += damping * dangling * p[n]
        r = new
    return r


def hybrid_search(store, vector_store, embedder, query, *, k=10, seeds=20, hops=2,
                  damping=0.5, iters=20, repo=None):
    """Return ``[(node_id, ppr_score)]`` ranked by hybrid (semantic + graph) relevance."""
    qvec = embedder.embed([query])[0]
    seed_hits = vector_store.search(qvec, k=seeds, repo=repo)
    if not seed_hits:
        return []
    nodes, adjacency = _expand(store, [nid for nid, _ in seed_hits], hops)
    personalization = {nid: sim for nid, sim in seed_hits if nid in nodes}
    scores = _ppr(nodes, adjacency, personalization, damping=damping, iters=iters)
    ranked = sorted(scores.items(), key=lambda t: t[1], reverse=True)
    return ranked[:k]
