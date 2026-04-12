"""Repo-level architecture resolution from the code knowledge graph.

The **trustworthy** cross-repo signal is the *package two-hop*: repo A
**publishes** a package that repo B **depends_on** → B depends on A. Raw
cross-repo ``imports`` edges are dominated by import-star artifacts (global
``module`` nodes like ``System``/``xunit`` shared across the fleet), so they are
deliberately NOT used here. The result is **inferred** (a manifest-derived,
likely-undercount signal), never presented as ground truth.

Stdlib-only; one SQL query against the shared store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..store.base import Store

# dependent_repo --depends_on--> publisher_repo, weighted by shared package count.
_TWO_HOP = """
SELECT dep.dep_repo, pub.pub_repo, COUNT(DISTINCT pub.pkg) AS shared
FROM (SELECT np.repo_id AS pub_repo, e.dst AS pkg FROM edges e
        JOIN nodes np ON np.node_id = e.src WHERE e.relation = 'publishes') pub
JOIN (SELECT nd.repo_id AS dep_repo, e.dst AS pkg FROM edges e
        JOIN nodes nd ON nd.node_id = e.src WHERE e.relation = 'depends_on') dep
  ON pub.pkg = dep.pkg
WHERE pub.pub_repo != dep.dep_repo
GROUP BY dep.dep_repo, pub.pub_repo
"""


def repo_dependency_edges(store: Store) -> list[dict]:
    """Real repo→repo dependencies via the package two-hop (``publishes ⨝ depends_on``).

    Each edge is ``dependent --depends_on--> publisher``, ``weight`` = number of
    shared packages, marked ``INFERRED`` (manifest-derived, a likely undercount —
    not every dependency declares/publishes a package). Far smaller and far more
    trustworthy than the raw cross-repo ``imports`` edges.
    """
    rows = store.conn.execute(_TWO_HOP).fetchall()
    return [{"src": dep, "dst": pub, "relation": "depends_on",
             "confidence": "INFERRED", "weight": shared}
            for dep, pub, shared in rows]


# caller_repo --flow--> exposer_repo, via a shared HTTP endpoint node. Direction
# follows the request: the repo that CALLS an endpoint flows to the repo that
# EXPOSES it. Weighted by the count of shared endpoints.
_HTTP_FLOW = """
SELECT cl.repo AS caller, ex.repo AS exposer, COUNT(DISTINCT ex.ep) AS shared
FROM (SELECT ne.repo_id AS repo, e.dst AS ep FROM edges e
        JOIN nodes ne ON ne.node_id = e.src WHERE e.relation = 'exposes') ex
JOIN (SELECT nc.repo_id AS repo, e.dst AS ep FROM edges e
        JOIN nodes nc ON nc.node_id = e.src WHERE e.relation = 'calls_http') cl
  ON ex.ep = cl.ep
WHERE ex.repo != cl.repo
GROUP BY cl.repo, ex.repo
"""


def repo_http_flow_edges(store: Store) -> list[dict]:
    """Real repo→repo HTTP flow via the endpoint two-hop (``exposes ⨝ calls_http``).

    Each edge is ``caller --flow--> exposer`` (the direction a request travels),
    ``weight`` = number of shared endpoints, ``context='http'``, marked ``INFERRED``
    (regex-detected + path-matched — a likely undercount, never ground truth).
    """
    rows = store.conn.execute(_HTTP_FLOW).fetchall()
    return [{"src": caller, "dst": exposer, "relation": "flow",
             "confidence": "INFERRED", "weight": shared, "context": "http"}
            for caller, exposer, shared in rows]
