"""Built-in source: ingest documents from a JSON HTTP API.

Standard library only (``urllib`` + ``json``). Auth, when needed, is a **bearer token
read from an environment variable** named in config (``token_env``) — the secret itself
never lives in the config file.
"""

from __future__ import annotations

import json
import os
import urllib.request

from .base import Document


def _dig(obj, path: str):
    """Resolve a dotted path (e.g. ``data.items``) into ``obj``, or None if absent."""
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


class ApiSource:
    """GET a JSON endpoint and map its records to documents.

    Config (``[[sources]] type="api"``):
      - ``url`` (required)
      - ``items``: dotted path to the list of records (default: the top-level value)
      - ``id_field`` / ``title_field`` / ``text_field``: record keys (default
        ``id`` / ``title`` / ``text``); a record without text is skipped
      - ``token_env``: name of an env var holding a bearer token (optional)
      - ``timeout``: seconds (default 20)
    """

    def __init__(self, url=None, items=None, id_field="id", title_field="title",
                 text_field="text", token_env=None, timeout=20, **_):
        self.url = url
        self.items = items
        self.id_field = id_field
        self.title_field = title_field
        self.text_field = text_field
        self.token_env = token_env
        self.timeout = int(timeout)

    def _fetch(self):
        headers = {"User-Agent": "contextlake-ingest", "Accept": "application/json"}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            charset = resp.headers.get_content_charset() or "utf-8"
            return json.loads(resp.read().decode(charset, errors="replace"))

    def iter_documents(self):
        if not self.url:
            return
        try:
            data = self._fetch()
        except Exception:  # noqa: BLE001 - an unreachable/invalid endpoint yields nothing
            return
        records = _dig(data, self.items) if self.items else data
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
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
