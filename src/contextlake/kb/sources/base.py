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
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ...logging_setup import log

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


# The opener every HTTP source uses. It lives here, not in one source, so that
# a credential-bearing source cannot be written without it: the redirect guard was
# first added to the `api` source alone, and `graphql` -- which sends the same
# bearer header through the same urllib -- kept forwarding it to any origin a
# redirect named. A fix applied to one sibling and not the other is the defect
# class this project keeps re-learning.

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    """``(scheme, host, port)`` of ``url``, lower-cased, with the default port filled in,
    so ``http://h`` and ``http://H:80/x`` compare equal."""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:      # a malformed port is its own origin, never a match
        port = -1
    return scheme, (parts.hostname or "").lower(), port


def _same_origin(a: str, b: str) -> bool:
    """True when both URLs share scheme, host and port: the unit a credential may travel in."""
    return _origin(a) == _origin(b)


class _OriginGuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but drop `Authorization` when the target is a different origin.

    urllib builds the redirected request from the original's headers, so the credential
    went wherever a ``Location`` pointed. An open redirect on the API host, or a
    compromised endpoint, then received the token (for ``auth="basic"`` on Atlassian that
    is an account-wide ``email:token``). A same-origin redirect keeps the header, because
    APIs do redirect within their own host and that request still needs to authenticate.
    One handler covers both schemes, since the check is on the header name.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _same_origin(req.full_url, new.full_url):
            # `headers` keys are stored capitalised (`Authorization`) by `Request`.
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
            log(f"api source: a redirect left {_origin(req.full_url)[1]} for "
                f"{_origin(new.full_url)[1]}; the Authorization header was not sent there",
                level=logging.WARNING)
        return new


# Built once: an opener carries no per-request state, and `urlopen` builds a default one
# per call anyway. Same proxy and HTTPS handling as `urlopen`; only the redirect handler
# differs.
_OPENER = urllib.request.build_opener(_OriginGuardedRedirect)


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

    def _reset_failures(self) -> None:
        self.failures = []

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
