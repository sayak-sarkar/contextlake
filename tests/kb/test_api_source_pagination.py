"""The generic `api` source must read every page, not just the first.

`api` and `graphql` are the escape hatch people reach for when pointing contextlake at
an issue tracker, and neither followed pagination: a 4,000-issue tracker ingested one
page and reported `✓`. `grep -i "page\\|cursor\\|next"` over both files returned zero hits.

Two unambiguous mechanisms are followed now -- the RFC 8288 `Link: rel="next"` header
and an explicit `next_field` cursor. Nothing is guessed: an API paginating by some other
convention reads one page exactly as before, and the page count is reported either way.

These run against a real local HTTP server rather than a mocked `urlopen`, because the
defect is in what the source does with a *response*, and a mock of the thing under test
would encode the same assumption twice.
"""

from __future__ import annotations

import http.server
import inspect
import json
import re
import socketserver
import threading
from pathlib import Path

import pytest

from contextlake.kb.connectors.gitlab import GitLabConnector
from contextlake.kb.sources.api import ApiSource, _next_from_link_header

DOCS = Path(__file__).resolve().parents[2] / "docs" / "document-sources.md"


@pytest.fixture
def paging_server():
    """Three pages chained by Link headers. Each response lists `prev` BEFORE `next`."""
    state = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            pages = {
                "/p1": ([{"id": 1, "title": "one", "text": "alpha"}], "/p2"),
                "/p2": ([{"id": 2, "title": "two", "text": "beta"}], "/p3"),
                "/p3": ([{"id": 3, "title": "three", "text": "gamma"}], None),
            }
            recs, nxt = pages.get(self.path, ([], None))
            body = json.dumps(recs).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if nxt:
                port = state["port"]
                self.send_header("Link",
                                 f'<http://127.0.0.1:{port}/prev>; rel="prev", '
                                 f'<http://127.0.0.1:{port}{nxt}>; rel="next"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), H) as srv:
        state["port"] = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{state['port']}"
        srv.shutdown()


def test_every_page_is_read(paging_server):
    """THE LOAD-BEARING ASSERTION. Before the fix this returned only 'one'."""
    src = ApiSource(url=f"{paging_server}/p1")
    docs = list(src.iter_documents())
    assert [d.title for d in docs] == ["one", "two", "three"], (
        "pagination did not follow the Link header; a paginated tracker would ingest "
        "page one and report success")
    assert src.pages_read == 3


def test_the_page_cap_is_reported_rather_than_silent(paging_server):
    """A cap nobody is told about reads as a complete ingest -- the same defect in a
    different coat."""
    src = ApiSource(url=f"{paging_server}/p1", max_pages=2)
    docs = list(src.iter_documents())
    assert len(docs) == 2
    assert src.hit_page_cap is True, "the cap was reached and not reported"


def test_the_cap_message_still_tells_an_ingest_user_to_raise_max_pages(paging_server, gls_logs):
    """The advice is right for `kb ingest`, which reads `max_pages`. Only `kb enrich`,
    which pins it to 1, words it differently."""
    list(ApiSource(url=f"{paging_server}/p1", max_pages=2).iter_documents())
    caps = [r.getMessage() for r in gls_logs.records if "-page cap" in r.getMessage()]
    assert caps, "control: the cap is reported"
    assert all("raise `max_pages` to read the rest" in m for m in caps), caps


def test_a_single_page_api_is_unchanged(paging_server):
    """The near-miss. Most APIs do not paginate, and they must behave exactly as before
    -- one request, no cap flag, the raw payload shape preserved."""
    src = ApiSource(url=f"{paging_server}/p3")
    docs = list(src.iter_documents())
    assert [d.title for d in docs] == ["three"]
    assert src.pages_read == 1
    assert src.hit_page_cap is False


@pytest.mark.parametrize("header,expected", [
    ('<http://x/2>; rel="next"', "http://x/2"),
    ('<http://x/0>; rel="prev", <http://x/2>; rel="next"', "http://x/2"),
    # THE TRAP: prev first, and "next" appears nowhere in the prev URL. A substring
    # match on the whole header returns the prev URL and the paginator walks backwards.
    ('<http://x/0>; rel="prev"', None),
    ('<http://x/2>; rel="nextpage"', None),
    ("", None),
    (None, None),
])
def test_the_link_header_is_parsed_not_substring_matched(header, expected):
    assert _next_from_link_header(header) == expected


def test_a_self_referential_next_does_not_loop(paging_server, monkeypatch):
    """A real API bug -- `next` pointing at the current page -- must terminate rather
    than spin until the process dies."""
    src = ApiSource(url=f"{paging_server}/p1", max_pages=100)
    calls = {"n": 0}
    real = src._fetch_one

    def looping(url):
        calls["n"] += 1
        payload, _ = real(url)
        return payload, f"{paging_server}/p1"      # always points back at page one

    monkeypatch.setattr(src, "_fetch_one", looping)
    list(src.iter_documents())
    assert calls["n"] <= 2, f"followed a self-referential next {calls['n']} times"


def _documented_key(key):
    """The text of the `- `key`: ...` bullet in the docs page's extra-keys list, or None."""
    m = re.search(rf"^- `{key}`: (.*?)(?=^- `|^## )", DOCS.read_text(encoding="utf-8"),
                  re.M | re.S)
    return m.group(1) if m else None


def test_the_docs_name_the_source_type_that_reads_each_paging_key():
    """The page listed `per_page` as "the API page size" for connector and ingest sources.
    Only the GitLab connector reads it. The `api` source drops it without a word, and its real
    cap, `max_pages`, was not on the page. Both facts are read from the code, so a source type
    that starts reading `per_page` makes this fail and sends the editor to the page."""
    assert "per_page" in inspect.signature(GitLabConnector.__init__).parameters
    api_params = inspect.signature(ApiSource.__init__).parameters
    assert "per_page" not in api_params, "an api source reads per_page now; update the docs page"
    assert "max_pages" in api_params

    per_page = _documented_key("per_page")
    assert per_page is not None and "GitLab" in per_page
    max_pages = _documented_key("max_pages")
    assert max_pages is not None and "`api` source" in max_pages
