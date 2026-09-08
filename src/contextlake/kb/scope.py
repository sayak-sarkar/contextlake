"""Which repositories a scope covers, and which store partitions belong to them.

ONE PLACE, because there were three and they disagreed. ``embeddings.store._repo_scope``
and ``cmds.forget._partitions`` both expanded a repo id to
``[repo_id, @connect:<repo>, @enrich:<repo>]`` and both omitted ``@wiki:<repo>``
entirely, so a repo-scoped search never saw its own wiki prose and ``kb forget`` left
that partition behind. A third copy written for access control would have inherited the
same omission and turned a cleanup gap into a disclosure.

WHY A PREDICATE AND A LIST, RATHER THAN ONE OR THE OTHER. The partitions a repo owns are
not a fixed set. ``@wiki:<repo>::<module>`` is one partition per qualifying module
(``cmds/wiki.py`` writes them), so the family is open-ended and cannot be written out as
four strings. But both vector backends filter with ``repo_id IN (?,?,...)``, one
placeholder per id, and the approximate-nearest-neighbour query carries its row cap in
the same SQL -- so narrowing after the query loses rows the cap already discarded.

A predicate has no ``IN``-clause form and a list cannot enumerate an open-ended family.
The resolution is neither: :func:`owns_partition` is the authority on what a scope
covers, and a caller that needs a list intersects that predicate with the partitions
that ACTUALLY EXIST (:func:`partitions_in_scope`), rather than with the partitions a
scope could name. The predicate decides; the store supplies the domain.

NO OPTIONAL IMPORTS. The two functions this replaced each imported
``connect_partition``/``enrich_partition`` from the connectors package and fell back to
``[repo_id]`` alone on ``ImportError``, so a partial install silently narrowed every
repo-scoped search. Partition ids are string formatting over the constants below; there
is nothing to fail. The connectors now build their ids FROM these constants rather than
the other way round, so there is still exactly one spelling of each prefix.

DIRECTION. Everything here is an ALLOW computation. A partition this module fails to
recognise is denied, never granted. Used to compute a DENY set, that default would fail
open. Do not invert these functions.
"""

from __future__ import annotations

from collections.abc import Iterable

from .model import EXTERNAL_REPO, PACKAGES_REPO, SHARED_REPO, SYSTEM_REPO

# The partition prefixes that belong to a repo. Each is `<prefix><repo>`, and
# `@wiki:` additionally carries a `::<module>` family below it. `@ingest:<name>`
# is deliberately ABSENT: an ingested document set belongs to no repository
# (`sqlite_store.list_partitions` says so in its own docstring), so it is
# unreachable by expanding a repo id and is visible only to a scope that names it
# some other way.
#
# THESE ARE THE ONLY DEFINITIONS. `connectors.orchestrate.connect_partition`,
# `connectors.enrich.enrich_partition` and `cmds.wiki._wiki_partition` all build
# their ids from these constants, so the code that WRITES a partition id and the
# code that RECOGNISES one cannot drift. They did drift: `@wiki:` was written by
# the wiki command and absent from both expansion copies, so a repo-scoped search
# never saw its own wiki prose and `kb forget` never deleted it.
CONNECT_PREFIX = "@connect:"
ENRICH_PREFIX = "@enrich:"
WIKI_PREFIX = "@wiki:"

_REPO_PREFIXES = (CONNECT_PREFIX, ENRICH_PREFIX, WIKI_PREFIX)

# The separator `cmds/wiki.py:_module_partition_head` puts between a repo id and
# a module prefix inside the `@wiki:` partition family. The whole-repo page's own
# key is `@wiki:<repo>` WITHOUT it, which is why the split below is bounded to
# one occurrence rather than greedy: a module prefix may itself contain `::`.
MODULE_SEP = "::"

# The four sentinels, and the rule for each. `(shared)` and `(packages)` are
# visible to every scope or imports break: `model.SHARED_REPO` records that
# module/endpoint/topic nodes are deduped to one row per store because their id
# encodes no repo, so a scope that hid them would cut every `imports` edge.
#
# `(external)` and `(system)` are NOT visible by default, and for different
# reasons. `(external)` is connector-fetched third-party content, gated by its
# own axis. `(system)` is discovered purely from the fleet's own outbound calls,
# so its mere presence discloses that SOME indexed repo calls that target --
# information about repositories the scope may not cover.
#
# `model.is_sentinel_repo` treats all four alike (a `(`-prefix test) and is left
# alone: `cmds/forget.py` and `dashboard.js` both depend on that contract.
ALWAYS_VISIBLE_SENTINELS = frozenset({SHARED_REPO, PACKAGES_REPO})
EXTERNAL_SENTINELS = frozenset({EXTERNAL_REPO})
WILDCARD_ONLY_SENTINELS = frozenset({SYSTEM_REPO})

#: Every sentinel this module rules on. A fifth one added to ``model.py`` and not
#: to a set above lands in no ruling, which the completeness test turns red
#: rather than letting it default to something nobody chose.
KNOWN_SENTINELS = (ALWAYS_VISIBLE_SENTINELS | EXTERNAL_SENTINELS
                   | WILDCARD_ONLY_SENTINELS)

#: The pattern that matches every repository, including the ``(system)`` sentinel.
WILDCARD = "**"


def repo_partitions(repo_id: str) -> list[str]:
    """The FIXED partitions ``repo_id`` owns: its own shard and the three named ones.

    This is the concrete list, for a caller that must name partitions to act on
    them (``clear_repo`` takes an id, not a predicate). It does NOT include the
    ``@wiki:<repo>::<module>`` family, which is open-ended and has to be
    discovered from the store -- see :func:`module_partitions_of`.

    Replaces ``embeddings.store._repo_scope`` and ``cmds.forget._partitions``,
    which returned three entries and omitted ``@wiki:`` from both.
    """
    return [repo_id] + [f"{prefix}{repo_id}" for prefix in _REPO_PREFIXES]


def partition_repo(partition_id: str) -> str | None:
    """The repository ``partition_id`` belongs to, or ``None`` if it belongs to none.

    ``None`` is returned for a sentinel, for ``@ingest:<name>``, and for any shape
    this module does not recognise. Every caller treats ``None`` as "not covered by
    a repo scope", which is the fail-closed reading: an unrecognised id is denied.

    That rule is written here because this codebase has the incident on record.
    ``visualize/payload.py:18-38`` once tested only the ``(``-prefixed sentinels
    and missed the ``@``-prefixed partitions, which made the fleet count DOUBLE
    the first time ``kb wiki`` ran, with ``@wiki:*`` entries indistinguishable
    from real repositories in the list.
    """
    for prefix in _REPO_PREFIXES:
        if partition_id.startswith(prefix):
            rest = partition_id[len(prefix):]
            if not rest:
                return None
            # Split once from the LEFT so a module prefix containing `::` keeps
            # its own separators; only the first one divides repo from module.
            return rest.split(MODULE_SEP, 1)[0] or None
    if partition_id.startswith("@"):
        # A partition family this module has no rule for -- `@ingest:` today,
        # and whatever a later release adds. Belongs to no repo BY DEFAULT,
        # rather than being guessed into one.
        return None
    if partition_id.startswith("("):
        # A sentinel. Ruled on by `sentinel_visible`, not by repo ownership.
        return None
    return partition_id


def match_repo(pattern: str, repo_id: str) -> bool:
    """Whether ``repo_id`` is covered by one glob ``pattern``, matched SEGMENT-WISE.

    Repo ids are path-shaped, so ``str.startswith`` is wrong on an authorization
    question: ``"a/bc".startswith("a/b")`` is ``True`` and a scope for ``a/b``
    would read ``a/bc``. That is this workspace's recorded unanchored-string-match
    bug class landing on a security decision, so it is never used here.

    The rules, and they are the whole grammar:

    * ``/`` divides segments. Matching is per segment, and a pattern matches only
      when it consumes the whole id.
    * ``*`` matches within ONE segment, and never across a ``/``.
    * ``**`` matches zero or more whole segments.

    So ``a/*`` matches ``a/b`` and not ``a/b/c``; ``a/**`` matches ``a``, ``a/b``
    and ``a/b/c``; ``a/b`` matches neither ``a/bc`` nor ``a/b/c``.
    """
    return _match_segments(pattern.split("/"), repo_id.split("/"))


def _match_segments(pat: list[str], seg: list[str]) -> bool:
    """Segment-wise glob, iterative over ``**`` and recursive only across it.

    Written out rather than delegated to ``fnmatch``/``PurePath.match``:
    ``fnmatch`` translates ``*`` to a regex that happily crosses ``/``, which is
    the exact hole this function exists to close, and ``PurePath.match`` anchors
    from the right.
    """
    pi = si = 0
    while pi < len(pat):
        if pat[pi] == WILDCARD:
            if pi + 1 == len(pat):
                return True  # trailing `**` eats every remaining segment
            # Try every split point for the `**`, shortest first.
            for take in range(si, len(seg) + 1):
                if _match_segments(pat[pi + 1:], seg[take:]):
                    return True
            return False
        if si >= len(seg):
            return False
        if not _match_one(pat[pi], seg[si]):
            return False
        pi += 1
        si += 1
    return si == len(seg)


def _match_one(pat: str, seg: str) -> bool:
    """One segment against one pattern segment. ``*`` matches within the segment."""
    if pat == "*":
        return True
    if "*" not in pat:
        return pat == seg
    parts = pat.split("*")
    if not seg.startswith(parts[0]) or not seg.endswith(parts[-1]):
        return False
    # Walk the interior literals left to right, so `a*b*c` needs them in order.
    pos = len(parts[0])
    end = len(seg) - len(parts[-1])
    if pos > end:
        return False
    for piece in parts[1:-1]:
        if not piece:
            continue
        found = seg.find(piece, pos, end)
        if found < 0:
            return False
        pos = found + len(piece)
    return True


def sentinel_visible(sentinel: str, patterns: Iterable[str], *,
                     external: bool = False) -> bool:
    """Whether a ``(``-prefixed sentinel is visible to this scope.

    ``external`` is the separate axis that rides alongside the repo scope; it
    keeps its own label rather than being folded into the patterns, because
    "may see third-party connector content" and "may see repository X" are
    different questions an operator answers separately.
    """
    if sentinel in ALWAYS_VISIBLE_SENTINELS:
        return True
    if sentinel in EXTERNAL_SENTINELS:
        return bool(external)
    if sentinel in WILDCARD_ONLY_SENTINELS:
        return any(p == WILDCARD for p in patterns)
    # A sentinel this module has no ruling for. Denied, not defaulted.
    return False


def owns_partition(partition_id: str, patterns: Iterable[str], *,
                   external: bool = False) -> bool:
    """THE PREDICATE. Whether ``partition_id`` is inside a scope.

    Covers the literal shard, ``@connect:``, ``@enrich:``, ``@wiki:`` and the whole
    ``@wiki:<repo>::<module>`` family, because the repo is recovered from the id
    rather than the ids being constructed from the repo.

    An empty ``patterns`` is an unscoped caller and sees everything. That is the
    same reading ``grants._check_tools`` gives an absent axis: "nobody wrote a
    scope" is not "scope to nothing", and collapsing the two would stop every key
    issued before the axis existed.
    """
    patterns = list(patterns)
    if partition_id.startswith("("):
        return sentinel_visible(partition_id, patterns, external=external)
    owner = partition_repo(partition_id)
    if owner is None:
        return False  # `@ingest:` and anything unrecognised: denied, never guessed
    if not patterns:
        return True
    return any(match_repo(p, owner) for p in patterns)


def partitions_in_scope(known: Iterable[str], patterns: Iterable[str], *,
                        external: bool = False) -> list[str]:
    """The subset of ``known`` this scope covers, as a concrete list.

    ``known`` is what the store actually holds (``Store.list_partitions()``), not
    what a scope could name. That is what makes an open-ended family expressible
    as an ``IN`` clause: the predicate above decides membership, and the store
    supplies a finite domain to decide it over.

    Order is preserved so a caller's SQL parameters are stable run to run, which
    keeps a query plan and a test assertion reproducible.
    """
    patterns = list(patterns)
    return [p for p in known
            if owns_partition(p, patterns, external=external)]


def module_partitions_of(known: Iterable[str], repo_id: str) -> list[str]:
    """Every ``@wiki:<repo>::<module>`` partition of ``repo_id`` present in ``known``.

    For ``kb forget``, which must name partitions to delete them and so cannot use
    the predicate alone. Kept separate from :func:`repo_partitions` because that
    one answers "what does a repo always own" with no store, and this one needs
    the store to answer at all.
    """
    head = f"{WIKI_PREFIX}{repo_id}{MODULE_SEP}"
    return [p for p in known if p.startswith(head)]
