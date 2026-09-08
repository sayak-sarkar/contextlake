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

WHAT IS AND IS NOT CLOSED HERE, stated because the gap is real and dated:

* Filtered: ``get_node``, ``neighbors``, ``search``, ``nodes_by_name``, ``list_repos``,
  ``get_repo``, ``stats``, ``repo_counts``, ``list_partitions`` and the two per-repo
  index-metadata reads.
* NOT closed: ``.conn`` is still forwarded. Three tools (``repo_dependencies``,
  ``repo_flow``, ``repo_event_flow``) reach raw SQL through ``arch/resolve.py``, so
  hiding it turns every one of those calls into an ``AttributeError``. They are filtered
  at the tool bodies instead, which scopes what a caller receives; what remains open is
  NEW code written against ``.conn``, which is a review concern rather than a live hole.
  Closing it means rewriting ``arch/resolve.py`` off ``.conn``, which is S4.3.6.

Do not "fix" that by hiding ``conn`` without doing the rewrite first.
"""

from __future__ import annotations

from contextvars import ContextVar

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
_SCOPE_MEMO: ContextVar[dict | None] = ContextVar("contextlake_scope_memo", default=None)


def open_request_scope():
    """Start a memo for one request. Returns the token to reset with.

    Call as the first statement of the request, reset in the same ``finally`` that
    resets the drift probe. Both are per-request caches that are only sound for the
    instant one request is answered.
    """
    return _SCOPE_MEMO.set({})


def reset_request_scope(token) -> None:
    _SCOPE_MEMO.reset(token)


def _memo(owner) -> dict:
    """This proxy's slice of the current request's memo.

    KEYED BY PROXY INSTANCE, not shared across the request. There is one
    ``ScopedStore`` per server today, so a single flat dict worked -- until a second
    proxy existed in one request, which read the FIRST one's cached scope and
    answered with it. A test constructing two proxies in one request found it; in
    production the second proxy does not exist yet, so nothing would have.

    A throwaway when there is no request, rather than a shared fallback: a direct
    call outside a request must not write into state a later request reads.
    """
    memo = _SCOPE_MEMO.get()
    if memo is None:
        return {}
    return memo.setdefault(id(owner), {})


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

        * ``None`` -- there is no principal. DENY EVERYTHING. Unreachable through
          ``guarded``, which raises ``IdentityUnset`` above the anchor before any
          tool body runs, so this exists for a direct construction.
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
        """STILL FORWARDED, and this is the one hole in this class. See the module
        docstring: hiding it breaks three tools until ``arch/resolve.py`` is rewritten
        off raw SQL. Those three are filtered at their tool bodies instead."""
        return self._store.conn

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
        return self._filter_nodes(
            self._store.search(query, kind, repo, limit), repo)

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
