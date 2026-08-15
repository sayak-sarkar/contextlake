"""Built-in source: ingest documents from a JSON HTTP API.

Standard library only (``urllib`` + ``json``). Auth, when needed, is a **bearer token
read from an environment variable** named in config (``token_env``) — the secret itself
never lives in the config file.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from ...logging_setup import log
from .base import Document, FetchFailures, url_is_fetchable


def _dig(obj, path: str):
    """Resolve a dotted path (e.g. ``data.items``) into ``obj``, or None if absent."""
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _records_of(payload, items: str | None) -> list:
    """The record list inside one page, normalised. Shared by `_fetch` and the reader
    so a merged multi-page result and a single-page one are the same shape."""
    recs = _dig(payload, items) if items else payload
    if isinstance(recs, dict):
        recs = [recs]
    return recs if isinstance(recs, list) else []


def _next_from_link_header(value: str | None) -> str | None:
    """The ``rel="next"`` URL from an RFC 8288 ``Link`` header, or None.

    Parsed rather than substring-matched: `rel="next"` and `rel="prev"` both contain
    "next" as a substring of nothing useful, but a naive `in` test on the whole header
    would happily return a `prev` URL when both are present -- which is how a paginator
    walks backwards forever. Anchored on the parsed rel value.
    """
    if not value:
        return None
    for part in value.split(","):
        segs = part.split(";")
        if len(segs) < 2:
            continue
        url = segs[0].strip()
        if not (url.startswith("<") and url.endswith(">")):
            continue
        for attr in segs[1:]:
            k, _, v = attr.strip().partition("=")
            if k.strip().lower() == "rel" and v.strip().strip('"\'') == "next":
                return url[1:-1]
    return None


class ApiSource(FetchFailures):
    """GET a JSON endpoint and map its records to documents.

    Config (``[[sources]] type="api"``):
      - ``url`` (required)
      - ``items``: dotted path to the list of records (default: the top-level value)
      - ``id_field`` / ``title_field`` / ``text_field``: record keys (default
        ``id`` / ``title`` / ``text``); a record without text is skipped
      - ``token_env``: name of an env var holding a bearer token (optional)
      - ``timeout``: seconds (default 20)
      - ``next_field``: dotted path to the NEXT-PAGE URL or cursor in the response
        (e.g. ``next``, ``meta.next_cursor``). Optional.
      - ``max_pages``: hard cap on pages followed (default 50)

    **Pagination.** This is the generic escape hatch people reach for when pointing
    contextlake at an issue tracker, and it used to read page one and report success --
    so a 4,000-issue tracker ingested 100 issues and said `✓`. Two unambiguous mechanisms
    are followed now: the HTTP ``Link: rel="next"`` header (GitHub, GitLab and anything
    else following RFC 8288) and an explicit ``next_field`` cursor. Nothing is guessed:
    an API that paginates by some other convention reads one page exactly as before, and
    the page count is reported either way so a truncated ingest is visible.
    """

    def __init__(self, url=None, items=None, id_field="id", title_field="title",
                 text_field="text", token_env=None, timeout=20,
                 next_field=None, max_pages=50, **_):
        self.url = url
        self.items = items
        self.id_field = id_field
        self.title_field = title_field
        self.text_field = text_field
        self.token_env = token_env
        self.timeout = int(timeout)
        self.next_field = next_field
        # A cap, not a target. An API that always returns a `next` link would otherwise
        # loop until the process died; reaching the cap is reported rather than silent.
        self.max_pages = max(1, int(max_pages))
        self.pages_read = 0
        self.hit_page_cap = False

    def _headers(self):
        headers = {"User-Agent": "contextlake-ingest", "Accept": "application/json"}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _fetch_one(self, url) -> tuple[object, str | None]:
        """One page: ``(payload, next_url_or_cursor)``."""
        req = urllib.request.Request(url, headers=self._headers())  # noqa: S310 - URL from trusted config
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            charset = resp.headers.get_content_charset() or "utf-8"
            payload = json.loads(resp.read().decode(charset, errors="replace"))
            nxt = _next_from_link_header(resp.headers.get("Link"))
        if nxt is None and self.next_field:
            nxt = _dig(payload, self.next_field)
            nxt = str(nxt) if isinstance(nxt, str) and nxt else None
        return payload, nxt

    def _fetch(self):
        """Every page, concatenated. Returns the first payload when only one page exists,
        so single-page APIs and the `items` dotted path behave exactly as before."""
        payload, nxt = self._fetch_one(self.url)
        self.pages_read = 1
        self.hit_page_cap = False
        if not nxt:
            return payload
        merged = list(_records_of(payload, self.items))
        seen = {self.url}
        while nxt and self.pages_read < self.max_pages:
            if nxt in seen:            # a self-referential `next` is a real API bug
                break
            seen.add(nxt)
            page, nxt = self._fetch_one(nxt)
            self.pages_read += 1
            merged.extend(_records_of(page, self.items))
        if nxt:
            self.hit_page_cap = True
            log(f"api source: stopped at the {self.max_pages}-page cap with more pages "
                f"available -- raise `max_pages` to read the rest", level=logging.WARNING)
        return merged

    def iter_documents(self):
        self._reset_failures()
        if not self.url:
            return
        # Before the try: a refusal raised inside it would be swallowed silently.
        if not url_is_fetchable(self.url, source="api source"):
            return
        try:
            data = self._fetch()
        except Exception as e:  # noqa: BLE001 - an unreachable endpoint must not raise
            # Recorded, not swallowed. An unreachable endpoint, an expired token
            # and a genuinely empty response used to be the same `0 documents`.
            self._record_failure(self.url, e, what="api source")
            return
        # `_fetch` returns the raw payload for a single page and an
        # already-merged record list when it followed pagination.
        records = data if isinstance(data, list) else _records_of(data, self.items)
        if not records:
            return
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
            text = rec.get(self.text_field)
            if not text:
                continue
            rid = str(rec.get(self.id_field, i))
            yield Document(id=rid, title=str(rec.get(self.title_field) or rid),
                           text=str(text), uri=self.url, attrs={"index": i})
