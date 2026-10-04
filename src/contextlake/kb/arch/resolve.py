"""Repo-level architecture resolution from the code knowledge graph.

The **trustworthy** cross-repo signal is the *package two-hop*: repo A
**publishes** a package that repo B **depends_on** → B depends on A. Raw
cross-repo ``imports`` edges are dominated by import-star artifacts (global
``module`` nodes like ``System``/``xunit`` shared across the fleet), so they are
deliberately NOT used here. The result is **inferred** (a manifest-derived,
likely-undercount signal), never presented as ground truth.

Stdlib-only. The joins run in the store (``Store.repo_pairs_via_shared_target`` and
``Store.edges_with_unmatched_target``), not as raw SQL here, so a scoping proxy can
filter the rows: this module used to read ``store.conn`` directly, which is why the
network proxy had to keep forwarding it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..store.base import Store

# dependent_repo --depends_on--> publisher_repo, weighted by shared package count.


def repo_dependency_edges(store: Store) -> list[dict]:
    """Real repo→repo dependencies via the package two-hop (``publishes ⨝ depends_on``).

    Each edge is ``dependent --depends_on--> publisher``, ``weight`` = number of
    shared packages, marked ``INFERRED`` (manifest-derived, a likely undercount —
    not every dependency declares/publishes a package). Far smaller and far more
    trustworthy than the raw cross-repo ``imports`` edges.
    """
    rows = store.repo_pairs_via_shared_target("publishes", "depends_on")
    return [{"src": dep, "dst": pub, "relation": "depends_on",
             "confidence": "INFERRED", "weight": shared}
            for pub, dep, shared in rows]


# caller_repo --flow--> exposer_repo, via a shared HTTP endpoint node. Direction
# follows the request: the repo that CALLS an endpoint flows to the repo that
# EXPOSES it. Weighted by the count of shared endpoints.


def repo_http_flow_edges(store: Store) -> list[dict]:
    """Real repo→repo HTTP flow via the endpoint two-hop (``exposes ⨝ calls_http``).

    Each edge is ``caller --flow--> exposer`` (the direction a request travels),
    ``weight`` = number of shared endpoints, ``context='http'``, marked ``INFERRED``
    (regex-detected + path-matched — a likely undercount, never ground truth).
    """
    rows = store.repo_pairs_via_shared_target("exposes", "calls_http")
    return [{"src": caller, "dst": exposer, "relation": "flow",
             "confidence": "INFERRED", "weight": shared, "context": "http"}
            for exposer, caller, shared in rows]


# publisher_repo --flow--> consumer_repo, via a shared topic node. Direction
# follows the event: the repo that PUBLISHES to a topic flows to the repo that
# CONSUMES it. Weighted by the count of shared topics.


def repo_event_flow_edges(store: Store) -> list[dict]:
    """Real repo→repo event flow via the topic two-hop (``publishes_event ⨝ consumes_event``).

    Each edge is ``publisher --flow--> consumer`` (the direction an event travels),
    ``weight`` = number of shared topics, ``context='event'``, marked ``INFERRED``
    (regex-detected literal topics — a likely undercount that omits config-variable
    topics, never ground truth).
    """
    rows = store.repo_pairs_via_shared_target("publishes_event", "consumes_event")
    return [{"src": publisher, "dst": consumer, "relation": "flow",
             "confidence": "INFERRED", "weight": shared, "context": "event"}
            for publisher, consumer, shared in rows]


# calls_http edges whose endpoint never joins ANY indexed repo's `exposes` edge
# (the join `repo_http_flow_edges` relies on, inverted: NOT IN instead of JOIN) are
# calls that leave the fleet: either genuinely external, or an internal service
# simply not indexed yet (see kb/model.py's SYSTEM_REPO docstring). attrs is
# fetched raw and parsed in Python (not SQLite json_extract) to match the rest
# of the store's JSON-in-a-TEXT-column handling and avoid depending on the
# JSON1 SQLite extension being compiled in.


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
    rows = store.edges_with_unmatched_target("calls_http", "exposes")
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
