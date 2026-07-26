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

import json
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


# publisher_repo --flow--> consumer_repo, via a shared topic node. Direction
# follows the event: the repo that PUBLISHES to a topic flows to the repo that
# CONSUMES it. Weighted by the count of shared topics.
_EVENT_FLOW = """
SELECT pub.repo AS publisher, con.repo AS consumer, COUNT(DISTINCT pub.topic) AS shared
FROM (SELECT np.repo_id AS repo, e.dst AS topic FROM edges e
        JOIN nodes np ON np.node_id = e.src WHERE e.relation = 'publishes_event') pub
JOIN (SELECT nc.repo_id AS repo, e.dst AS topic FROM edges e
        JOIN nodes nc ON nc.node_id = e.src WHERE e.relation = 'consumes_event') con
  ON pub.topic = con.topic
WHERE pub.repo != con.repo
GROUP BY pub.repo, con.repo
"""


def repo_event_flow_edges(store: Store) -> list[dict]:
    """Real repo→repo event flow via the topic two-hop (``publishes_event ⨝ consumes_event``).

    Each edge is ``publisher --flow--> consumer`` (the direction an event travels),
    ``weight`` = number of shared topics, ``context='event'``, marked ``INFERRED``
    (regex-detected literal topics — a likely undercount that omits config-variable
    topics, never ground truth).
    """
    rows = store.conn.execute(_EVENT_FLOW).fetchall()
    return [{"src": publisher, "dst": consumer, "relation": "flow",
             "confidence": "INFERRED", "weight": shared, "context": "event"}
            for publisher, consumer, shared in rows]


# calls_http edges whose endpoint never joins ANY indexed repo's `exposes` edge
# (the same join `_HTTP_FLOW` makes, inverted -- NOT IN instead of the JOIN) are
# calls that leave the fleet: either genuinely external, or an internal service
# simply not indexed yet (see kb/model.py's SYSTEM_REPO docstring). attrs is
# fetched raw and parsed in Python (not SQLite json_extract) to match the rest
# of the store's JSON-in-a-TEXT-column handling and avoid depending on the
# JSON1 SQLite extension being compiled in.
_UNRESOLVED_CALLS = """
SELECT nc.repo_id AS caller, e.attrs AS attrs
FROM edges e JOIN nodes nc ON nc.node_id = e.src
WHERE e.relation = 'calls_http'
  AND e.dst NOT IN (SELECT dst FROM edges WHERE relation = 'exposes')
"""


def repo_external_system_edges(store: Store) -> list[dict]:
    """Repo → external-system edges: HTTP calls that never resolve to any
    indexed repo's exposed route, grouped by the raw host the call named.

    Each edge is ``caller --calls_external--> system`` (``system`` is the raw
    host, e.g. ``api.stripe.com``, not a repo id), ``weight`` = number of
    distinct calls to that host, marked ``INFERRED``. A call site with no
    visible host (a relative path against a base-url client -- see
    ``kb/flow/http.py:raw_host``) contributes nothing here: there is no
    system to name, only an unresolved path, which is already visible as an
    endpoint node with no ``exposes`` edge.

    Deliberately unclassified: nothing here distinguishes a genuine
    third-party dependency from an internal service this fleet simply hasn't
    indexed yet (see ``kb/c4.py``'s C1 layer, the renderer for this).
    """
    rows = store.conn.execute(_UNRESOLVED_CALLS).fetchall()
    counts: dict[tuple[str, str], int] = {}
    for caller, raw_attrs in rows:
        if not raw_attrs:
            continue
        host = json.loads(raw_attrs).get("raw_host")
        if not host:
            continue
        key = (caller, host)
        counts[key] = counts.get(key, 0) + 1
    return [{"src": caller, "system": host, "relation": "calls_external",
             "confidence": "INFERRED", "weight": n}
            for (caller, host), n in counts.items()]
