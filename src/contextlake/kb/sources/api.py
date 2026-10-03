"""Built-in source: ingest documents from a JSON HTTP API.

Standard library only (``urllib`` + ``json``). Auth, when needed, reads its secret from
an environment variable named in config (``token_env``) — the secret itself never lives in
the config file. Two schemes: a bearer token (the default) and HTTP Basic, which is what
Atlassian Cloud, Jira and Confluence require of an API token.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse
import urllib.request

from ...logging_setup import log
from .base import _OPENER, Document, FetchFailures, _same_origin, url_is_fetchable


def _dig(obj, path: str):
    """Resolve a dotted path (e.g. ``data.items``) into ``obj``, or None if absent."""
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _field(rec: dict, path: str, default=None):
    """One record field by name or dotted path, or ``default`` when it is absent.

    ``id_field`` / ``title_field`` / ``text_field`` used to be a flat ``rec.get(name)``.
    Only ``items`` and ``next_field`` went through `_dig`, so the documented Atlassian
    example (``text_field = "fields.summary"``) looked up a key no Jira record has,
    skipped every record as textless and reported zero documents with no error. A literal
    key is tried first, so a record that really has a key named ``fields.summary`` (the
    only thing the flat lookup could read) keeps working. Only then is the name read as a
    path. A present key holding None is still returned as None, as `dict.get` did; only
    an absent path returns ``default``. `_dig` cannot be reused for that, because it
    returns None for both cases.
    """
    if path in rec:
        return rec[path]
    cur = rec
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
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
      - ``id_field`` / ``title_field`` / ``text_field``: record keys or dotted paths
        into a record, e.g. ``fields.summary`` (default ``id`` / ``title`` / ``text``);
        a record without text is skipped
      - ``token_env``: name of an env var holding the secret (optional)
      - ``auth``: ``bearer`` (default) or ``basic``. ``basic`` sends
        ``Authorization: Basic base64(user:secret)``, which is the only scheme Atlassian
        Cloud accepts for an API token -- a bearer header there returns 401 with a body
        that does not say why
      - ``user``: the username half for ``auth="basic"`` (for Atlassian, the account
        email). Not a secret on its own, so it may live in config; the token may not
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

    A next link is resolved against the page that named it, so relative links work. It must
    then be ``http(s)``: a ``file:`` link is refused, recorded in ``failures``, and the pages
    already read are kept. The ``Authorization`` header goes only to the configured origin.
    """

    def __init__(self, url=None, items=None, id_field="id", title_field="title",
                 text_field="text", token_env=None, timeout=20,
                 next_field=None, max_pages=50, auth=None, user=None, **_):
        self.url = url
        self.items = items
        self.id_field = id_field
        self.title_field = title_field
        self.text_field = text_field
        self.token_env = token_env
        # Normalised here rather than at each use: a config file may carry any casing,
        # and an unknown value falls back to bearer (the prior behaviour) rather than
        # inventing a scheme.
        self.auth = (auth or "bearer").strip().lower()
        self.user = user
        self.timeout = int(timeout)
        self.next_field = next_field
        # A cap, not a target. An API that always returns a `next` link would otherwise
        # loop until the process died; reaching the cap is reported rather than silent.
        self.max_pages = max(1, int(max_pages))
        self.pages_read = 0
        self.hit_page_cap = False

    def _headers(self):
        """Request headers, with the Authorization one built from the configured scheme.

        A configured-but-unbuildable credential is REPORTED, not dropped in silence. The
        old code added no header when the env var was unset, so the request went out
        anonymous and the API answered 401 or an empty list -- which reaches the operator
        as `0 documents` from a source that looks configured. That is the same
        indistinguishable-empty failure `FetchFailures` exists for, one layer earlier.
        """
        headers = {"User-Agent": "contextlake-ingest", "Accept": "application/json"}
        if not self.token_env:
            return headers
        token = os.environ.get(self.token_env)
        if not token:
            log(f"  source auth: ${self.token_env} is unset or empty, so the request "
                f"goes out UNAUTHENTICATED and may return 0 documents")
            return headers
        if self.auth == "basic":
            # Atlassian Cloud accepts an API token only this way. `user` is the account
            # email there. Without it the pair is meaningless, so say so rather than
            # sending `base64(":token")`, which 401s with no hint of the cause.
            if not self.user:
                log('  source auth: auth="basic" needs `user` (for Atlassian, the '
                    "account email); sending no Authorization header")
                return headers
            pair = base64.b64encode(f"{self.user}:{token}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {pair}"
        else:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _fetch_one(self, url) -> tuple[object, str | None]:
        """One page: ``(payload, next_url_or_cursor)``."""
        headers = self._headers()
        if "Authorization" in headers and not _same_origin(self.url, url):
            # A `next` URL comes from the response body or a `Link` header, so a
            # compromised endpoint can name any host. The redirect handler cannot see
            # this hop: it is a fresh request, not a redirect.
            del headers["Authorization"]
            log(f"api source: the next page {url} is a different origin from {self.url}; "
                f"the Authorization header was not sent there", level=logging.WARNING)
        req = urllib.request.Request(url, headers=headers)  # noqa: S310 - URL from trusted config
        with _OPENER.open(req, timeout=self.timeout) as resp:
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
        page_url = self.url
        while nxt and self.pages_read < self.max_pages:
            # Three steps, in this order. A `next` link is response data, so it gets the
            # checks the configured URL got. (1) Resolve it against the page that NAMED it:
            # `/p2`, `?page=2` and `b` mean something only there. (2) Run the scheme check
            # on the result. Checked first, a relative link has no scheme and is refused;
            # an absolute `file:` link is refused either way. (3) `_fetch_one` then decides
            # the credential from the resolved URL's origin.
            nxt = urllib.parse.urljoin(page_url, nxt)
            if not url_is_fetchable(nxt, source="api source"):
                self._record_failure(
                    nxt, ValueError("next page URL is not http(s); pagination stopped"),
                    what="api source")
                nxt = None             # stopped on purpose, so not reported as the page cap
                break
            if nxt in seen:            # a self-referential `next` is a real API bug
                break
            seen.add(nxt)
            page_url = nxt
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
            text = _field(rec, self.text_field)
            if not text:
                continue
            rid = str(_field(rec, self.id_field, i))
            yield Document(id=rid, title=str(_field(rec, self.title_field) or rid),
                           text=str(text), uri=self.url, attrs={"index": i})
