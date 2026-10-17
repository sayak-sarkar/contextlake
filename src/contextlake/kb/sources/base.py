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

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ...logging_setup import log
from .._util import _OPENER, _same_origin  # noqa: F401 - the sources import these from here

# Schemes an ingest fetcher will open. `urllib.request.urlopen` also speaks
# `file:`, `ftp:` and `data:`, and a source URL is *config*, not a constant -- an
# auto-discovered `.contextlake.kb.toml` can set `[[sources]] url` without the
# user ever naming the file, because contextlake clones repositories into the
# workspace itself. `file:///home/<user>/.ssh/id_rsa` therefore turned an ingest
# run into a local-file read whose contents land in the graph, the wiki and every
# MCP client. `kb/trust.py` gates the keys that reach a subprocess argv for exactly
# this reason and deliberately leaves `url` alone as "an HTTP endpoint"; this is
# what makes that description true.
#
# An allowlist, not a `file:`-denylist: the point is that a fetcher opens network
# URLs, so anything that is not one is refused by default rather than enumerated.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


# What a source reports when `url_is_fetchable` refused every URL it was given.
NOT_HTTP_REASON = ("every configured URL was skipped, because only http and https "
                   "URLs are fetched")


def url_is_fetchable(url: str, *, source: str) -> bool:
    """True if ``url`` is an ``http(s)`` URL an ingest source may open.

    Logs a WARNING naming the source and scheme when it refuses. It returns a
    bool rather than raising because all three fetchers wrap their request in a
    broad ``except Exception: continue`` so one bad URL cannot abort a run -- a
    raise would be swallowed there and the refusal would be silent, which is the
    failure mode this project treats as worse than the bug. Callers must check
    *before* entering that block.
    """
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme in _ALLOWED_URL_SCHEMES:
        return True
    log(f"{source}: refusing to fetch {scheme or 'scheme-less'!r} URL -- only "
        f"http/https are fetched, so a config-supplied URL cannot be used to read "
        f"local files or other non-network resources. Skipping this URL.",
        level=logging.WARNING)
    return False


# The opener every HTTP source uses lives in `kb/_util.py`, beside `post_json`, so the
# hosted LLM and embeddings clients share it. The redirect guard was first added to the
# `api` source alone, and `graphql` -- which sends the same bearer header through the same
# urllib -- kept forwarding it to any origin a redirect named. A fix applied to one
# sibling and not the other is the defect class this project keeps re-learning.


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


class FetchFailures:
    """Mixin: record what a source could not reach, so zero documents can be explained.

    A source that swallows its own network errors and yields nothing is
    indistinguishable, from the outside, from a source that is genuinely empty. Measured
    before this existed: a wrong URL, an expired token, an HTTP 500, a proxy block and an
    empty page all produced the same `✓ 0 documents`, exit 0. On a content pipeline that
    means ingestion silently stops and nothing in CI can detect it.

    `sources/files.py` was already the in-house standard -- it names every file it skips
    and why. This gives the network sources the same manners plus a machine-readable
    tally, because `cmds/ingest.py` has to be able to tell "empty" from "broken" without
    parsing log lines.

    A source still must not abort the run when ONE target fails: the remaining targets
    are usually fine, and a whole ingest lost to a single dead URL is a worse outcome.
    So failures are recorded and reported rather than raised.
    """

    #: ``(target, reason)`` for every target this source could not read, most recent run.
    failures: list[tuple[str, str]]

    #: Why this source sent no request at all in the most recent run, or None when it sent
    #: one. A source that sent one and found nothing is "reachable"; one that sent none has
    #: not shown anything about the endpoint, so `cmds/ingest.py` must not say it is.
    no_request_reason: str | None = None

    def _reset_failures(self) -> None:
        self.failures = []
        self.no_request_reason = None

    def _record_no_request(self, reason: str) -> None:
        """Remember why this run sent nothing. Not a failure: the config asked for it."""
        self.no_request_reason = reason

    def _record_failure(self, target: str, exc: BaseException, *, what: str) -> None:
        """Log the miss the way `files.py` does, and remember it for the caller."""
        reason = f"{type(exc).__name__}: {exc}"
        if not hasattr(self, "failures"):
            self.failures = []
        self.failures.append((target, reason))
        log(f"{what}: could not read {target} -- {reason}", level=logging.WARNING)


def _builtin_sources() -> dict[str, type]:
    from .api import ApiSource
    from .files import FilesSource
    from .graphql import GraphQLSource
    from .mcp import McpSource
    from .web import WebSource

    return {"files": FilesSource, "web": WebSource, "api": ApiSource,
            "graphql": GraphQLSource, "mcp": McpSource}


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
            except Exception:  # noqa: BLE001,S112 - a bad plugin must not break discovery
                continue
    except Exception:  # noqa: BLE001,S110 - importlib.metadata quirks are non-fatal
        pass
    return found


def build_source(type_name: str, /, **options) -> Source | None:
    """Instantiate a source by type name with ``options``, or ``None`` if unknown."""
    cls = discover_sources().get(type_name)
    return cls(**options) if cls else None
