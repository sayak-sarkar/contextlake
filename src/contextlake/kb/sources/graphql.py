"""Built-in source: ingest documents from a GraphQL API.

Standard library only (``urllib`` + ``json``). POSTs a query (and optional
variables) to a single endpoint and maps records in the response's ``data``
payload to documents, mirroring :class:`~contextlake.kb.sources.api.ApiSource`'s
record-mapping shape so the two connectors are configured the same way.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from ...logging_setup import log
from .api import _dig
from .base import Document, FetchFailures, url_is_fetchable


def _has_next_page(obj, _depth: int = 0) -> bool:
    """Does any ``pageInfo`` in this response say ``hasNextPage``?

    Relay's ``pageInfo`` shape is a convention rather than a rule, so this only ever
    REPORTS -- it never changes what is read. Bounded depth because a GraphQL response
    is arbitrarily nested and a full walk of a large payload is not worth the scan.
    """
    if _depth > 6:
        return False
    if isinstance(obj, dict):
        info = obj.get("pageInfo")
        if isinstance(info, dict) and info.get("hasNextPage") is True:
            return True
        return any(_has_next_page(v, _depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_next_page(v, _depth + 1) for v in obj[:50])
    return False


class GraphQLSource(FetchFailures):
    """POST a GraphQL query and map records in the response to documents.

    Config (``[[sources]] type="graphql"``):
      - ``url`` (required)
      - ``query`` (required): the GraphQL query document
      - ``variables``: a dict of query variables (optional)
      - ``items``: dotted path into the response, rooted at ``data`` (e.g.
        ``repository.issues.nodes``); default: ``data`` itself
      - ``id_field`` / ``title_field`` / ``text_field``: record keys (default
        ``id`` / ``title`` / ``text``); a record without text is skipped
      - ``token_env``: name of an env var holding a bearer token (optional)
      - ``timeout``: seconds (default 20)
    """

    def __init__(self, url=None, query=None, variables=None, items=None,
                 id_field="id", title_field="title", text_field="text",
                 token_env=None, timeout=20, **_):
        self.url = url
        self.query = query
        self.variables = variables or {}
        self.items = items
        self.id_field = id_field
        self.title_field = title_field
        self.text_field = text_field
        self.token_env = token_env
        self.timeout = int(timeout)

    def _fetch(self):
        headers = {"User-Agent": "contextlake-ingest", "Accept": "application/json",
                   "Content-Type": "application/json"}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        body = json.dumps({"query": self.query, "variables": self.variables}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")  # noqa: S310 - URL from trusted config
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            charset = resp.headers.get_content_charset() or "utf-8"
            return json.loads(resp.read().decode(charset, errors="replace"))

    def iter_documents(self):
        self._reset_failures()
        if not self.url or not self.query:
            return
        # Before the try: a refusal raised inside it would be swallowed silently.
        if not url_is_fetchable(self.url, source="graphql source"):
            return
        try:
            payload = self._fetch()
        except Exception as e:  # noqa: BLE001 - an unreachable endpoint must not raise
            # Recorded, not swallowed. An unreachable endpoint, an expired token
            # and a genuinely empty response used to be the same `0 documents`.
            self._record_failure(self.url, e, what="graphql source")
            return
        # A GraphQL response can carry partial `data` alongside `errors`; treat
        # any reported error as untrustworthy rather than guess which fields
        # are safe to keep.
        if not isinstance(payload, dict):
            self._record_failure(self.url, TypeError("response was not a JSON object"),
                                 what="graphql source")
            return
        if payload.get("errors"):
            # An error payload used to return silently, so a query rejected by the
            # server -- a bad field, an expired token, a rate limit -- was
            # indistinguishable from a query that legitimately matched nothing.
            errs = payload["errors"]
            first = ""
            if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                first = str(errs[0].get("message", ""))[:200]
            self._record_failure(self.url, RuntimeError(first or "GraphQL errors"),
                                 what="graphql source")
            return
        data = payload.get("data")
        records = _dig(data, self.items) if self.items else data
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            return
        # GraphQL pagination needs the query itself to carry a cursor variable, so it
        # cannot be followed generically the way an RFC 8288 `Link` header can. What CAN
        # be done without guessing is to notice that the server said there is more, and
        # say so -- rather than reporting a truncated read as a complete one.
        if _has_next_page(data):
            log(f"graphql source: the response reports more pages (pageInfo.hasNextPage), "
                f"and GraphQL cursors cannot be followed generically. {len(records)} "
                f"record(s) read. Add the cursor to your query to page through the rest.",
                level=logging.WARNING)
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
            text = rec.get(self.text_field)
            if not text:
                continue
            rid = str(rec.get(self.id_field, i))
            yield Document(id=rid, title=str(rec.get(self.title_field) or rid),
                           text=str(text), uri=self.url, attrs={"index": i})
