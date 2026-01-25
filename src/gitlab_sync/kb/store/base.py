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
    def mark_indexed(self, repo_id: str, head_commit: str | None, indexed_at: str) -> None:
        """Record that a repo was indexed at a commit + timestamp."""

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
    def stats(self) -> Stats: ...

    @abstractmethod
    def clear_repo(self, repo_id: str) -> None:
        """Remove all nodes/edges for a repo (for a clean re-index)."""

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
