"""A read-filtering proxy over a :class:`~contextlake.kb.store.base.Store`.

NETWORK-ONLY. Constructed in ``build_http_app`` and nowhere else. stdio and the
in-process dashboard chat get the bare store, so the graph traversals stay unwrapped:
``server.py`` records that a traversal is not one query but thousands of small round
trips, and a per-call grant resolution on that path would be paid thousands of times for
a caller that has no key at all.

AN ALLOW-LIST WRAPPER, NOT A ``__getattr__`` PASSTHROUGH. Every forwarded method is
written out. A delegating proxy forwards whatever it is asked for, so the first attribute
nobody thought about is served unfiltered, and the test that would catch it has to
predict the attribute's name.

WHAT IS CLOSED HERE, and how it was closed:

* Filtered: ``get_node``, ``neighbors``, ``search``, ``nodes_by_name``, ``list_repos``,
  ``get_repo``, ``stats``, ``repo_counts``, ``list_partitions``, the two per-repo
  index-metadata reads, and the two repo-pair queries ``arch/resolve.py`` runs
  (``repo_pairs_via_shared_target``, ``edges_with_unmatched_target``).
* ``.conn`` is NOT forwarded. Reading it raises ``AttributeError``. It used to be
  forwarded because three tools (``repo_dependencies``, ``repo_flow``,
  ``repo_event_flow``) ran raw SQL through ``arch/resolve.py``; that SQL now lives in the
  store behind the two methods above, which this proxy filters on BOTH repos of a pair.
  The tool bodies keep their own filter (``server._readable_repo_edges``) as a second
  layer. New code that needs data the protocol does not offer adds a filtered method
  here; it does not reach for ``.conn``. ``tests/kb/test_scope_over_the_wire.py`` drives
  every registered tool with a scoped key, so a tool that does reach for it fails there.

  History: a paragraph here once said the three tools were filtered at their bodies
  when no filter existed, and a key scoped to one glob received every denied
  repository's name and its package, HTTP and event relations.

"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from weakref import WeakKeyDictionary

from .scope import owns_partition, partitions_in_scope

# The per-request memo. One entry per distinct question, discarded when the request
# ends, following `_DRIFT_PROBE`'s shape and lifetime in `server.py` -- set as the
# first statement of the tool wrapper, reset in the same `finally`.
#
# WHY A CONTEXTVAR AND NOT AN INSTANCE ATTRIBUTE. One `ScopedStore` serves every
# request on the server, concurrently. An instance dict would let one principal's
# verdicts answer another principal's question, and the two memos below are keyed by
# partition id and node id, NEITHER of which carries the principal. That is the shape
# where a scoped key reads another key's results and every test still passes.
# The most rows a scoped search materialises while looking for readable ones.
# See `ScopedStore.search`: unbounded, a caller-chosen term drove the store to score
# and load every match. Far above any real top-k (callers ask for 1 to 50).
_SCOPED_SEARCH_CEILING = 10_000

_SCOPE_MEMO: ContextVar[WeakKeyDictionary | None] = ContextVar(
    "contextlake_scope_memo", default=None)


def open_request_scope():
    """Start a memo for one request. Returns the token to reset with.

    Call as the first statement of the request, reset in the same ``finally`` that
    resets the drift probe. Both are per-request caches that are only sound for the
    instant one request is answered.
    """
    return _SCOPE_MEMO.set(WeakKeyDictionary())


def reset_request_scope(token) -> None:
    _SCOPE_MEMO.reset(token)


def _memo(owner) -> dict:
    """This proxy's slice of the current request's memo.

    KEYED BY THE PROXY OBJECT, not shared across the request. There is one
    ``ScopedStore`` per server today, so a single flat dict worked -- until a second
    proxy existed in one request, which read the FIRST one's cached scope and
    answered with it. A test constructing two proxies in one request found it; in
    production the second proxy does not exist yet, so nothing would have.

    THE KEY IS THE OBJECT, NOT ``id(owner)``, and the difference is a real defect
    rather than a style choice. CPython recycles the address of a freed object, so a
    proxy constructed after another is garbage collected can be handed the dead one's
    memo entry. Measured: 1988 collisions in 2000 trials, and the observable symptom
    was a proxy holding NO principal reporting ``is_scoped() == False``, which is the
    verdict deciding whether the fleet-wide readers refuse a caller. It reached the
    suite as a 1-in-8 flake under ``--cov``, because coverage shifts the allocation
    pattern that decides whether the address is reused.

    Keying on the object cannot collide: two live objects are distinct keys, and a
    dead one's entry goes with it. A ``WeakKeyDictionary`` rather than a plain dict
    so the memo never keeps a proxy (and through it a store) alive: a plain dict
    would also be correct here, because ``bounded_tool`` resets the scope in a
    ``finally`` that catches ``BaseException``, but that is a fact about a caller
    two modules away, and a cache should not depend on one.

    A throwaway when there is no request, rather than a shared fallback: a direct
    call outside a request must not write into state a later request reads.
    """
    memo = _SCOPE_MEMO.get()
    if memo is None:
        return {}
    return memo.setdefault(owner, {})


class ScopedStore:
    """Reads filtered to what one principal's key grants, resolved per call.

    One instance for the server's life. The grant is NOT captured at construction:
    ``build_server`` is called once over one store (its ``_tool_slots`` semaphore is
    created there, so one server per key would fragment the global concurrency bound),
    while the principal changes per request. So the scope is resolved on every call
    from the identity ContextVar, through ``scope_source``.
    """

    def __init__(self, store, scope_source) -> None:
        self._store = store
        self._scope_source = scope_source

    # -- the scope ---------------------------------------------------------

    def _scope(self) -> tuple[tuple[str, ...], bool] | None:
        """``(patterns, external)`` for the caller of THIS request, or ``None``.

        THREE STATES, and collapsing any two of them is a security bug:

        * ``None`` -- there is no principal, or its key record is gone. DENY
          EVERYTHING. No principal is unreachable through ``guarded``, which raises
          ``IdentityUnset`` above the anchor. A record removed between the grant
          check and the first store read IS reachable, and
          ``GrantCheck.repo_scope`` returns ``None`` for it; it used to return
          ``([], False)``, which landed in the allow-everything state below.
        * ``((), external)`` -- a principal with NO repo scope on its key. Allow
          every repository. An absent axis is not a scope, the same reading
          ``grants._check_tools`` gives an absent ``tools`` axis.
        * ``((patterns...), external)`` -- scoped.

        The first two were one state in the first draft of this class, because both
        produced an empty pattern list and ``_unscoped`` read empty as "allow". A
        proxy holding no principal then served everything. The test that found it
        constructs the proxy DIRECTLY; driven through a tool call it would never
        have reached the branch, because the identity refusal fires first.
        """
        memo = _memo(self)
        if "scope" not in memo:
            got = self._scope_source()
            memo["scope"] = None if got is None else (
                tuple(got[0]), bool(got[1]))
        return memo["scope"]

    def _unscoped(self) -> bool:
        """True only when a KNOWN principal has no repo scope. Never for no principal."""
        scope = self._scope()
        return scope is not None and not scope[0]

    def _allows(self, partition_id: str | None) -> bool:
        if partition_id is None:
            return False
        scope = self._scope()
        if scope is None:
            return False
        memo = _memo(self).setdefault("partitions", {})
        if partition_id not in memo:
            patterns, external = scope
            memo[partition_id] = owns_partition(
                partition_id, patterns, external=external)
        return memo[partition_id]

    def visible_partitions(self) -> list[str]:
        """The concrete partition ids this caller may read, for an ``IN`` clause.

        The predicate intersected with what the store actually holds. This is the
        function that makes the open-ended ``@wiki:<repo>::<module>`` family
        expressible as SQL parameters -- see ``kb/scope.py``.
        """
        memo = _memo(self)
        if "visible" not in memo:
            scope = self._scope()
            if scope is None:
                memo["visible"] = []
            else:
                patterns, external = scope
                memo["visible"] = partitions_in_scope(
                    self._store.list_partitions(), patterns, external=external)
        return list(memo["visible"])

    def allows_repo(self, repo_id: str) -> bool:
        """PUBLIC. Whether this caller may read ``repo_id``.

        For the tool bodies that read from DISK rather than through the store: five
        of them resolve a path from ``store.path`` and open a file, so no forwarded
        method ever sees the request and this class cannot filter it. They ask here
        instead.

        Measured 2026-09-08 over a real HTTP server: before this existed,
        ``get_repo_brief`` answered ``found=true`` with the denied repository's own
        brief to a key scoped elsewhere, while ``search_code`` on the same key was
        correctly filtered. The difference was that one went through a covered
        method and the other went to disk.
        """
        return self._allows(repo_id)

    def allows_page_of(self, repo_id: str) -> bool:
        """PUBLIC. Whether this caller may read the per-repo PAGE ``repo_id`` names.

        For the tools that open ``<dir>/<repo_id with "/" as "__">.md`` (the wiki
        and the generated docs). ``allows_repo`` answers about the STRING the caller
        sent, and the file is named by an encoding that is not one-to-one, so the
        string and the file's owner can differ:

        * ``*`` matches inside one segment, so a scope ``team/*`` admits
          ``team/x__y``, whose file is the one written for the denied ``team/x/y``.
        * An admitted id without ``__`` can name the file of a denied repository
          whose id has one: ``team/y`` and ``team__y`` both encode to ``team__y.md``.
        * A namespace the scope admits falls through to its CLUSTER page, which
          narrates every member of the namespace, denied ones included.

        So the gate resolves who owns the file from the REAL store, below this
        proxy (through it, a denied owner is invisible and the collision would be
        missed), and allows only when ``repo_id`` is itself an indexed repository
        and EVERY id that encodes to the same file is readable. A ``__`` in the
        argument is refused outright for a scoped caller, and a namespace is not an
        indexed repository, so a cluster page never reaches a scoped caller: it
        spans repositories, and a scope is a statement about single ones.

        An unscoped caller is answered ``True`` without a lookup, matching every
        other early return in this class. No principal is ``False``.
        """
        scope = self._scope()
        if scope is None:
            return False
        if not scope[0]:
            return True
        if "__" in repo_id or not self._allows(repo_id):
            return False
        # The encoding `get_wiki` and `get_generated_doc` open the file by:
        # `visualize.html_render.repo_slug`, the one the page writers use. IMPORTED,
        # not copied. A copy is a second definition of the boundary this gate
        # protects, and it drifts the day either one changes. The cost that made the
        # first version copy it (that module pulls in the visualize package) is
        # avoided by importing here, inside the one branch that needs it: a scoped
        # caller asking for a page.
        from .visualize.html_render import repo_slug
        slug = repo_slug(repo_id)
        known = {r.id for r in self._store.list_repos()}
        known.update(p for p in self._store.list_partitions()
                     if not p.startswith(("@", "(")))
        owners = {rid for rid in known if repo_slug(rid) == slug}
        return repo_id in owners and all(self._allows(rid) for rid in owners)

    def is_scoped(self) -> bool:
        """Whether ANY repo scope applies to this caller.

        The fleet-wide disk readers (``get_fleet_doc``, ``graph_health``) have no
        repo argument to check, so they refuse a scoped caller outright rather than
        serve a document about repositories it cannot list.
        """
        scope = self._scope()
        return scope is None or bool(scope[0])

    def _node_visible(self, node) -> bool:
        return node is not None and self._allows(getattr(node, "repo", None))

    # -- forwarded, unfiltered ---------------------------------------------

    @property
    def path(self):
        """The STORE's own path, not a per-repo one, so exposing it discloses nothing.

        It has to be forwarded. Five tool bodies read ``getattr(store, "path", None)``
        and each has a pathless fallback branch, so a proxy that omits it does not
        raise: it silently takes the wrong branch and answers ``found=False`` or "nothing
        could be checked", which reads as an empty result rather than a broken proxy.
        """
        return self._store.path

    @property
    def conn(self):
        """Not forwarded: raw SQL would read past the scope. See the module docstring."""
        raise AttributeError(
            "ScopedStore does not forward .conn; raw SQL would read past the caller's "
            "scope. Add a filtered method to the Store protocol instead.")

    def close(self) -> None:
        self._store.close()

    # -- filtered reads ----------------------------------------------------

    def get_node(self, node_id: str):
        node = self._store.get_node(node_id)
        return node if self._node_visible(node) else None

    def neighbors(self, node_id: str, relation=None, direction="both"):
        """Edges where BOTH endpoints are visible.

        One-sided filtering leaks the far repository's name in plain text. A file
        node's id is its repo id followed by a path inside it, so an edge from a
        visible ``(shared)`` module node to a denied repo's file hands over that
        repo's id and a path within it, while the node the caller asked about was
        legitimately visible the whole time.
        """
        edges = self._store.neighbors(node_id, relation, direction)
        if self._unscoped():
            return edges
        return [e for e in edges
                if self._node_visible(self._store.get_node(e.src))
                and self._node_visible(self._store.get_node(e.dst))]

    def search(self, query: str, kind=None, repo=None, limit: int = 20):
        """The best ``limit`` rows THIS CALLER may read, not the best ``limit`` rows
        filtered afterwards.

        It used to pass ``limit`` to the store and filter the result. The relevance
        floor (``relevance.term_anchors``) probes each term with ``limit=1``, so when
        the single best match in the whole store sat in a denied repository, the
        filter emptied the one row and a scoped key was told the term is not in the
        graph while its own repositories held it: ``semantic_search`` answered empty
        with no note, and ``ask`` said "No indexed symbol matches".

        So a scoped caller re-queries with a growing limit until it has ``limit``
        visible rows or the store returns fewer rows than were asked for, which
        means every match has been seen.

        THE GROWTH IS BOUNDED at ``_SCOPED_SEARCH_CEILING`` rows. The version that
        first fixed this had no bound and said none was needed, on the grounds that
        a larger ``LIMIT`` costs only the rows it materialises. That was wrong twice.
        The store sorts with ``ORDER BY bm25(...)``, so EVERY round scores every
        match, and ``.fetchall()`` then materialises the whole round. And the caller
        chooses the term: a scoped key searching a common word whose matches all sit
        in repositories it cannot read drove the rounds until the entire match set
        was in memory, on a store with hundreds of thousands of nodes. A key holder
        could exhaust the server.

        The ceiling brings the original defect back only for a term with more than
        that many better-ranked matches in denied repositories ahead of the first
        readable one, and it fails CLOSED: the term reads as absent, nothing leaks.
        It is logged when reached, so the loss is not silent. The real fix pushes the
        scope into the SQL as ``repo_id IN (visible_partitions())``, so the store
        returns only readable rows in one round; that changes the ``Store`` protocol
        and is tracked separately.
        """
        if self._unscoped():
            return self._store.search(query, kind, repo, limit)
        if repo and not self._allows(repo):
            return []
        if limit <= 0:
            return self._filter_nodes(
                self._store.search(query, kind, repo, limit), repo)
        ask = limit
        while True:
            rows = self._store.search(query, kind, repo, ask)
            kept = self._filter_nodes(rows, repo)
            if len(kept) >= limit or len(rows) < ask:
                return kept[:limit]
            if ask >= _SCOPED_SEARCH_CEILING:
                from ..logging_setup import log
                log(f"scoped search for {query!r} stopped at {ask} rows with "
                    f"{len(kept)} readable; later matches were not examined",
                    level=logging.WARNING)
                return kept[:limit]
            ask = min(ask * 4, _SCOPED_SEARCH_CEILING)

    def nodes_by_name(self, name: str, kind=None, repo=None):
        return self._filter_nodes(self._store.nodes_by_name(name, kind, repo), repo)

    def _filter_nodes(self, nodes, repo):
        """Drop nodes outside the scope, and refuse a ``repo=`` outside it outright.

        The second half matters as much as the first. Passing a denied ``repo`` through
        and filtering the (empty) result would return a normal empty answer, which is
        indistinguishable from a repository that exists and has no match -- and the
        note-building code above it would then echo the denied repo id back in prose.
        """
        if self._unscoped():
            return nodes
        if repo and not self._allows(repo):
            return []
        return [n for n in nodes if self._node_visible(n)]

    def get_repo(self, repo_id: str):
        return self._store.get_repo(repo_id) if self._allows(repo_id) else None

    def list_repos(self):
        repos = self._store.list_repos()
        if self._unscoped():
            return repos
        return [r for r in repos if self._allows(r.id)]

    def list_partitions(self) -> list[str]:
        return self.visible_partitions()

    def repo_counts(self, repo_id: str) -> tuple[int, int]:
        return self._store.repo_counts(repo_id) if self._allows(repo_id) else (0, 0)

    def repo_pairs_via_shared_target(self, a_relation: str, b_relation: str):
        """Only pairs where the caller may read BOTH repos: a pair from a readable repo
        to a denied one would hand over the denied repo's id, as ``neighbors`` says."""
        rows = self._store.repo_pairs_via_shared_target(a_relation, b_relation)
        if self._unscoped():
            return rows
        return [r for r in rows if self.allows_repo(r[0]) and self.allows_repo(r[1])]

    def edges_with_unmatched_target(self, relation: str, target_relation: str):
        """Only rows whose source repo the caller may read. The other column is a raw
        host from the edge's attrs, not a repo id."""
        rows = self._store.edges_with_unmatched_target(relation, target_relation)
        if self._unscoped():
            return rows
        return [r for r in rows if self.allows_repo(r[0])]

    def get_repo_parser_version(self, repo_id: str):
        return (self._store.get_repo_parser_version(repo_id)
                if self._allows(repo_id) else None)

    def get_repo_indexed_at(self, repo_id: str):
        return (self._store.get_repo_indexed_at(repo_id)
                if self._allows(repo_id) else None)

    def stats(self):
        """Store-wide counts, recomputed over the visible partitions.

        Forwarding the real ``stats()`` would disclose the fleet's size to a key scoped
        to one repository, which is the same fact ``list_repos`` is filtered to hide.
        """
        if self._unscoped():
            return self._store.stats()
        visible = self.visible_partitions()
        nodes = edges = 0
        for part in visible:
            n, e = self._store.repo_counts(part)
            nodes += n
            edges += e
        from .store.base import Stats

        # `by_confidence` is dropped to an empty mapping rather than forwarded: it is
        # a store-wide breakdown and there is no per-partition version of it to sum,
        # so forwarding it would put an unscoped number beside two scoped ones.
        return Stats(repos=len([p for p in visible if not p.startswith(("@", "("))]),
                     nodes=nodes, edges=edges, by_confidence={})
