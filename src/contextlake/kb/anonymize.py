"""The last step before anonymized output leaves for a client: one rewrite of the payload.

`--anonymize` promises to hash author identities and drop external URLs and README and wiki
prose. Each serializer applied that on its own, and each one that missed a field leaked it:
the export's wiki pages, the chat answers, and then connector link names and titles and ADR
bodies on fourteen routes. This module is the second layer. It runs on everything the
dashboard server sends as JSON, on every graph payload (`viz.to_payload` consults
:func:`active`), and on the `--site` snapshot. The per-serializer drops stay in place as the
first layer: one bug here must not become a full leak.

Rules, keyed on the node, not on the route:

- **External nodes** (connector output: ``repo`` is ``(external)``, or a cross-source kind
  such as ``issue`` or ``mr``) keep their kind and get a label and an id derived from their
  ORIGINAL id with a per-run key. Every text field that could hold what an external system
  wrote (title, summary, URL, doc, file, qualified name, signature) is dropped, and the
  original id and name are replaced wherever they appear inside another string.
- **Document nodes** (``adr``, ``document``, ``wiki``) lose their body text (``doc`` and its
  siblings). Their names stay, as symbol names do.

The key is random per export and per server start, so labels change between the two and cannot
be matched across exports. Anonymized external nodes therefore do not open: their ids resolve
to nothing.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import os
import re
import threading

from .model import EXTERNAL_REPO

# Fields that hold text an external system wrote, or a path or URL into it.
_EXTERNAL_TEXT = ("title", "summary", "url", "doc", "file", "qualified_name", "signature",
                  "text", "snippet", "excerpt", "body", "description", "attrs")
# Fields that hold a document's body.
_DOCUMENT_TEXT = ("doc", "summary", "text", "snippet", "excerpt", "body")


def _kinds_in(group: str) -> frozenset[str]:
    from .kinds import KIND_REGISTRY

    return frozenset(k for k, spec in KIND_REGISTRY.items() if spec.group == group)


# A run of the characters ids and keys are built from: no `/`, `#` or `@`, so an id inside a
# route such as `#/symbol/<id>` is its own token. Trailing punctuation is left out, so a key at
# the end of a sentence still matches. A key outside this class takes the alternation path.
_TOKEN = re.compile(r"[\w.:\-]*\w")


def _distinctive(name: str) -> bool:
    """A name to replace inside other text as well as where it stands alone: five characters
    or more and not only letters, as ``host:ticket:77`` or a tracker key like ``ACME-123``.
    A plain word such as ``Login`` is replaced only as a whole field: inside other strings it
    would rewrite symbol names that merely contain it."""
    return len(name) >= 5 and not name.isalpha()


class Anonymizer:
    """One run's rewrite. Labels and ids are derived from the original id only, and every
    value this produced is remembered, so a payload rewritten twice comes out unchanged."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key if key is not None else os.urandom(16)
        self._external_kinds = _kinds_in("Cross-source")
        self._document_kinds = _kinds_in("Documents")
        self._produced: set[str] = set()
        self._ids: dict[str, str] = {}     # original id -> anonymized id
        self._names: dict[str, str] = {}   # original name -> label
        self._sub: dict[str, str] = {}
        self._spaced: re.Pattern | None = None
        self._pattern_size = -1
        # One per server, shared by its request threads, and the maps grow as it runs.
        self._lock = threading.Lock()

    def _digest(self, value: str) -> str:
        return hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _is_external(self, d: dict) -> bool:
        kind = d.get("kind")
        return d.get("repo") == EXTERNAL_REPO or (isinstance(kind, str)
                                                  and kind in self._external_kinds)

    def _remember(self, d: dict) -> None:
        orig_id = d.get("id") if isinstance(d.get("id"), str) else None
        name = d.get("name") if isinstance(d.get("name"), str) else None
        basis = orig_id or name
        if not basis or basis in self._produced:
            return
        h = self._digest(basis)
        kind = d.get("kind") if isinstance(d.get("kind"), str) else "item"
        if orig_id and orig_id not in self._ids:
            new_id = f"anon:{kind}:{h[:12]}"
            self._ids[orig_id] = new_id
            self._produced.add(new_id)
        if name and name not in self._names and name not in self._produced:
            label = f"{kind} {h[:4]}"
            self._names[name] = label
            self._produced.add(label)

    def _collect(self, obj) -> None:
        if isinstance(obj, dict):
            if self._is_external(obj):
                self._remember(obj)
            for v in obj.values():
                self._collect(v)
        elif isinstance(obj, list):
            for v in obj:
                self._collect(v)

    def _string(self, s: str) -> str:
        if s in self._produced:
            return s
        if s in self._ids:
            return self._ids[s]
        if s in self._names:
            return self._names[s]
        # Inside longer strings: chat prose, a "No indexed package named ..." note, a
        # `#/symbol/<id>` route. Only ids and distinctive names: a frame named "Login"
        # replaced inside every string would rewrite symbol names that merely contain it.
        self._refresh_substrings()
        if not self._sub:
            return s
        # Most keys are single tokens (`host:ticket:77`, `ACME-123`, an id): find tokens once
        # and look each up, which is linear in the string. A regex alternation of thousands
        # of keys over every string took 1.4 s on a 2 MB payload with 5,000 items. Names
        # with a space are rare and go through a small alternation.
        s = _TOKEN.sub(lambda m: self._sub.get(m.group(0), m.group(0)), s)
        if self._spaced is not None:
            s = self._spaced.sub(lambda m: self._sub[m.group(0)], s)
        return s

    def _refresh_substrings(self) -> None:
        size = len(self._ids) + len(self._names)
        if size == self._pattern_size:
            return
        self._pattern_size = size
        self._sub = dict(self._ids)
        self._sub.update({n: lbl for n, lbl in self._names.items() if _distinctive(n)})
        spaced = sorted((k for k in self._sub if not _TOKEN.fullmatch(k)), key=len,
                        reverse=True)
        self._spaced = re.compile("|".join(map(re.escape, spaced))) if spaced else None

    def _rewrite(self, obj):
        if isinstance(obj, dict):
            out = {}
            external = self._is_external(obj)
            document = obj.get("kind") in self._document_kinds
            for k, v in obj.items():
                if external and k in _EXTERNAL_TEXT:
                    continue
                if document and (k in _DOCUMENT_TEXT or k == "url"
                                 or (k == "file" and isinstance(v, str) and "://" in v)):
                    continue      # an ingested document's file can be the URL it came from
                out[k] = self._rewrite(v)
            if external and isinstance(obj.get("name"), str):
                out["name"] = self._names.get(obj["name"], self._string(obj["name"]))
            return out
        if isinstance(obj, list):
            return [self._rewrite(v) for v in obj]
        if isinstance(obj, str):
            return self._string(obj)
        return obj

    def rewrite(self, payload):
        """``payload`` with the rules above applied. Pure: the input is not modified."""
        with self._lock:
            self._collect(payload)
            return self._rewrite(payload)

    def label_for(self, kind: str, original: str) -> str:
        """The label an external node with this original id or name gets (for a serializer
        that builds one entry at a time, such as the Links panel)."""
        with self._lock:
            self._remember({"kind": kind, "id": original, "name": original})
            return self._names.get(original) or self._ids[original]


_ACTIVE: contextvars.ContextVar[Anonymizer | None] = contextvars.ContextVar(
    "contextlake_anonymizer", default=None)


def active() -> Anonymizer | None:
    """The anonymizer for the output being built in this context, or None."""
    return _ACTIVE.get()


@contextlib.contextmanager
def using(anonymizer: Anonymizer | None):
    """Make ``anonymizer`` active for the block, and reset it afterwards. A thread starts with
    an empty context, so a server sets this in each request handler."""
    token = _ACTIVE.set(anonymizer)
    try:
        yield anonymizer
    finally:
        _ACTIVE.reset(token)
