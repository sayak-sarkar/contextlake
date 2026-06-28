"""Built-in source: fetch web pages and ingest their readable text.

Standard library only (``urllib`` + ``html.parser``) — no new dependency, no headless
browser. The network is touched only when a ``web`` source is actually configured, so
the core stays offline-first.
"""

from __future__ import annotations

import re
import urllib.request
from html.parser import HTMLParser

from .base import Document

# Note: do NOT drop <head> wholesale — <title> lives there; script/style/etc. inside
# it are dropped individually below.
_DROP = {"script", "style", "noscript", "template", "svg"}
_WS = re.compile(r"[ \t ]+")


class _Reader(HTMLParser):
    """Pull visible text (and the <title>) out of an HTML document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._drop = 0
        self._in_title = False
        self.title = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self._drop += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in _DROP and self._drop:
            self._drop -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._drop:
            return
        if self._in_title:
            self.title += data
            return
        text = _WS.sub(" ", data).strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        return "\n".join(self._parts)


def html_to_text(html: str) -> tuple[str, str]:
    """Return ``(title, text)`` extracted from an HTML string. Pure, testable."""
    r = _Reader()
    try:
        r.feed(html)
    except Exception:  # noqa: BLE001 - malformed markup must not raise
        pass
    return r.title.strip(), r.text().strip()


class WebSource:
    """Fetch one or more URLs and yield each page's readable text as a Document.

    Config (``[[sources]] type="web"`` or a plugin): ``url`` (single) or ``urls`` (list),
    and ``timeout`` (seconds, default 20). Unreachable/empty pages are skipped.
    """

    def __init__(self, url: str | None = None, urls=None, timeout: int = 20, **_):
        self.urls = list(urls) if urls else ([url] if url else [])
        self.timeout = int(timeout)

    def iter_documents(self):
        for u in self.urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "contextlake-ingest"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    charset = resp.headers.get_content_charset() or "utf-8"
                    html = resp.read().decode(charset, errors="replace")
            except Exception:  # noqa: BLE001 - one bad URL must not abort the source
                continue
            title, text = html_to_text(html)
            if not text:
                continue
            yield Document(id=u, title=(title or u), text=text, uri=u,
                           attrs={"chars": len(text)})
