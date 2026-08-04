"""Storage interface for the knowledge layer.

A ``Store`` is a rebuildable cross-repo index over per-repo graph shards. The
interface is abstract so alternative backends (e.g. a future server-backed one)
can be dropped in; Phase 2.0 ships a single SQLite implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

from ..model import Edge, Node, Repo


@dataclass
class Stats:
    repos: int
    nodes: int
    edges: int
    by_confidence: dict[str, int]


class Store(ABC):
    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def upsert_repo(self, repo: Repo) -> None: ...

    @abstractmethod
    def get_repo(self, repo_id: str) -> Repo | None: ...

    @abstractmethod
    def list_repos(self) -> list[Repo]: ...

    @abstractmethod
    def mark_indexed(self, repo_id: str, head_commit: str | None, indexed_at: str,
                     parser_version: str | None = None) -> None:
        """Record that a repo was indexed at a commit + timestamp, by a parser.

        ``parser_version`` is the stamp carried by the shard that was just
        written, not the running build's constant: the store mirrors what is
        actually on disk, so the two can never drift.
        """

    @abstractmethod
    def get_repo_parser_version(self, repo_id: str) -> str | None:
        """The parser version of the repo's last index, or None if unstamped."""

    @abstractmethod
    def get_meta(self, key: str) -> str | None: ...

    @abstractmethod
    def upsert_nodes(self, repo_id: str, nodes: Iterable[Node]) -> None: ...

    @abstractmethod
    def upsert_edges(self, repo_id: str, edges: Iterable[Edge]) -> None: ...

    @abstractmethod
    def get_node(self, node_id: str) -> Node | None: ...

    @abstractmethod
    def neighbors(
        self, node_id: str, relation: str | None = None, direction: str = "both"
    ) -> list[Edge]: ...

    @abstractmethod
    def search(
        self, query: str, kind: str | None = None, repo: str | None = None, limit: int = 20
    ) -> list[Node]: ...

    @abstractmethod
    def nodes_by_name(
        self, name: str, kind: str | None = None, repo: str | None = None
    ) -> list[Node]:
        """Exact-name lookup (for 'where is X defined')."""

    @abstractmethod
    def stats(self) -> Stats: ...

    @abstractmethod
    def clear_repo(self, repo_id: str) -> None:
        """Remove all nodes/edges for a repo (for a clean re-index). Leaves the
        ``repos`` row itself in place -- use :meth:`delete_repo` to drop that too."""

    @abstractmethod
    def delete_repo(self, repo_id: str) -> None:
        """Remove a repo entirely: its nodes/edges (as :meth:`clear_repo`) and its
        ``repos`` row. For a repo that no longer exists under this id at all (moved,
        renamed, migrated to a new canonical id) -- not for a routine re-index,
        which should keep the row and just clear its content."""

    @abstractmethod
    def prune_orphan_nodes(self, repo_id: str) -> int:
        """Drop nodes in ``repo_id`` that no edge references. Returns how many.

        For the ``(external)`` partition, whose nodes exist only to be linked to
        code. Connector output is split across two partitions -- the edges live
        in the per-repo ``@connect:`` partition, the nodes they point at in
        ``(external)`` -- so the stale-data sweep that clears the partition
        removes a repo's connector edges and leaves its nodes behind. An
        external node with no edges cannot surface in any answer and is not
        reachable by any traversal, so it is not a node, it is litter."""

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
