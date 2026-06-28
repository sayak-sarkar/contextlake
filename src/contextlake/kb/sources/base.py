"""The source/plugin seam: pluggable document sources for RAG aggregation.

A *source* yields :class:`Document`s that ``contextlake ingest`` writes into the graph
(as ``kind="document"`` nodes) and, when an embedder is configured, into the semantic
vector store. Common sources ship built-in and are configured with **no code**
(``[[sources]] type="files"`` or ``contextlake ingest --path …``); anything else is a
**loosely-coupled plugin**: a separate package that registers a ``contextlake.sources``
entry point. Plugins and built-ins share one :class:`Source` protocol.

Writing a plugin (third-party package)::

    # in the plugin's pyproject.toml
    [project.entry-points."contextlake.sources"]
    confluence = "my_pkg.sources:ConfluenceSource"

    # the class just needs iter_documents() -> Iterable[Document]
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Document:
    """One unit of ingestible content."""

    id: str                      # stable id within its source (e.g. a relative path)
    title: str                   # human label (becomes the graph node name)
    text: str                    # the body that gets embedded
    uri: str = ""                # origin path / URL, for citation
    attrs: dict = field(default_factory=dict)


@runtime_checkable
class Source(Protocol):
    """Anything that can yield documents. The whole plugin contract."""

    def iter_documents(self) -> Iterable[Document]: ...


def _builtin_sources() -> dict[str, type]:
    from .api import ApiSource
    from .files import FilesSource
    from .web import WebSource

    return {"files": FilesSource, "web": WebSource, "api": ApiSource}


def discover_sources() -> dict[str, type]:
    """All known source types: built-ins + ``contextlake.sources`` entry-point plugins.

    A plugin shadows a built-in of the same name. A plugin that fails to import is
    skipped, never fatal — one broken plugin must not take down discovery.
    """
    found = dict(_builtin_sources())
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        group = (eps.select(group="contextlake.sources")
                 if hasattr(eps, "select") else eps.get("contextlake.sources", []))
        for ep in group:
            try:
                found[ep.name] = ep.load()
            except Exception:  # noqa: BLE001 - a bad plugin must not break discovery
                continue
    except Exception:  # noqa: BLE001 - importlib.metadata quirks are non-fatal
        pass
    return found


def build_source(type_name: str, /, **options) -> Source | None:
    """Instantiate a source by type name with ``options``, or ``None`` if unknown."""
    cls = discover_sources().get(type_name)
    return cls(**options) if cls else None
