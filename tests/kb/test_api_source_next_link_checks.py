"""The `api` source must check a `next` link the same way it checks the configured URL.

`url_is_fetchable` ran on the configured URL only. A `next` link (from a `Link` header or a
`next_field`) went straight to the fetcher, so a page whose body said
`"next": "file:///home/me/.ssh/config"` made the ingest read that file into the graph. A
relative `next` (`/page2`, `?page=2`, `b`) was not resolved at all: urllib raised
`unknown url type` and the whole ingest failed.

The fix is three steps, in this order:
1. resolve the link against the page that NAMED it (not against the configured URL),
2. run `url_is_fetchable` on the resolved URL and stop if it fails,
3. let the existing same-origin rule decide the credential, on the resolved URL.

Checking before resolving would let a relative link skip both checks, so the order is
tested through behaviour: a relative link must be followed, and a `file:` link must not.

These use a real local HTTP server on 127.0.0.1, as `test_api_source_pagination.py` does.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import types

import pytest

from contextlake.kb.sources.api import ApiSource


def _recs(*names):
    return [{"id": n, "title": n, "text": f"text of {n}"} for n in names]


@pytest.fixture
def server():
    """Routes are `path -> (records, next_value, link_header)`, filled in by each test.

    `hits` records `(path, authorization header or None)` for every request served.
    """
    state = types.SimpleNamespace(routes={}, hits=[], port=0)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state.hits.append((self.path, self.headers.get("Authorization")))
            recs, nxt, link = state.routes.get(self.path, ([], None, None))
            body = json.dumps({"items": recs, "next": nxt}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if link:
                self.send_header("Link", link)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), H) as srv:
        state.port = srv.server_address[1]
        state.base = f"http://127.0.0.1:{state.port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield state
        srv.shutdown()


def _read(server, start, **kw):
    src = ApiSource(url=server.base + start, items="items", next_field="next", **kw)
    return src, [d.title for d in src.iter_documents()]


# --- a next link that is not http(s) is refused -------------------------------------------

def test_a_file_next_link_is_not_read(server, tmp_path):
    """The reproduced defect. The file is inside tmp_path; its content must not reach a
    document, and the server must see only the first request."""
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"items": _recs("LOCALFILE")}))
    server.routes["/p1"] = (_recs("one"), outside.as_uri(), None)

    src, titles = _read(server, "/p1")

    assert titles == ["one"], f"the file page was ingested: {titles}"
    assert [p for p, _ in server.hits] == ["/p1"]


def test_a_refused_link_is_recorded_and_keeps_the_pages_already_read(server, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    server.routes["/p1"] = (_recs("one"), outside.as_uri(), None)

    src, titles = _read(server, "/p1")

    assert titles == ["one"], "page one was thrown away along with the refused link"
    assert [t for t, _ in src.failures] == [outside.as_uri()], src.failures
    assert "http" in src.failures[0][1]
    # Stopped on purpose, which is not the same as stopped at the cap.
    assert src.hit_page_cap is False


def test_a_file_link_in_the_link_header_is_refused_too(server, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"items": _recs("LOCALFILE")}))
    server.routes["/p1"] = (_recs("one"), None, f'<{outside.as_uri()}>; rel="next"')

    src, titles = _read(server, "/p1")

    assert titles == ["one"]
    assert src.failures and src.failures[0][0] == outside.as_uri()


@pytest.mark.parametrize("link", ["ftp://example.invalid/p2", "data:application/json,[]"])
def test_other_schemes_are_refused(server, link):
    server.routes["/p1"] = (_recs("one"), link, None)
    src, titles = _read(server, "/p1")
    assert titles == ["one"]
    assert [p for p, _ in server.hits] == ["/p1"]
    assert src.failures


# --- a relative next link is resolved against the page that named it ---------------------

def test_a_root_relative_next_link_is_followed(server):
    server.routes["/p1"] = (_recs("one"), "/p2", None)
    server.routes["/p2"] = (_recs("two"), None, None)

    src, titles = _read(server, "/p1")

    assert titles == ["one", "two"]
    assert [p for p, _ in server.hits] == ["/p1", "/p2"]
    assert not src.failures


def test_a_query_only_link_header_is_followed(server):
    """`?page=2` is the common relative form in a `Link` header."""
    server.routes["/list"] = (_recs("one"), None, '<?page=2>; rel="next"')
    server.routes["/list?page=2"] = (_recs("two"), None, None)

    src, titles = _read(server, "/list")

    assert titles == ["one", "two"]
    assert [p for p, _ in server.hits] == ["/list", "/list?page=2"]


def test_a_relative_link_resolves_against_the_current_page_not_the_first(server):
    """Page two lives in another directory and names its successor by a bare name. Resolved
    against the configured URL that would be `/y`. Against the page that named it, it is
    `/other/y`."""
    server.routes["/s"] = (_recs("one"), "/other/x", None)
    server.routes["/other/x"] = (_recs("two"), "y", None)
    server.routes["/other/y"] = (_recs("three"), None, None)

    src, titles = _read(server, "/s")

    assert titles == ["one", "two", "three"]
    assert [p for p, _ in server.hits] == ["/s", "/other/x", "/other/y"]


def test_a_relative_self_link_still_ends_the_walk(server):
    """The `seen` set must hold RESOLVED URLs. If it held the raw `/p1`, the resolved
    `http://127.0.0.1:PORT/p1` would not match the start URL and the walk would go on."""
    server.routes["/p1"] = (_recs("one"), "/p1", None)

    src, titles = _read(server, "/p1", max_pages=20)

    assert len(server.hits) <= 2, f"followed a relative self-link {len(server.hits)} times"


# --- the credential follows the resolved URL ---------------------------------------------

def test_a_protocol_relative_link_to_another_host_does_not_get_the_credential(
        server, monkeypatch):
    """`//localhost:PORT/p2` resolves to another origin from `127.0.0.1:PORT`. The page one
    request carries the token. The page two request must not."""
    monkeypatch.setenv("CL_TEST_API_TOKEN", "tok-for-test")
    server.routes["/p1"] = (_recs("one"), f"//localhost:{server.port}/p2", None)
    server.routes["/p2"] = (_recs("two"), None, None)

    src, titles = _read(server, "/p1", token_env="CL_TEST_API_TOKEN")

    assert titles == ["one", "two"]
    assert server.hits[0] == ("/p1", "Bearer tok-for-test")
    assert server.hits[1] == ("/p2", None), "the credential went to another origin"


def test_a_same_origin_relative_link_keeps_the_credential(server, monkeypatch):
    """The near-miss for the test above: a relative link stays on the origin, so page two
    still authenticates. Without this, a guard that strips the header on every follow-up
    request would pass the test above."""
    monkeypatch.setenv("CL_TEST_API_TOKEN", "tok-for-test")
    server.routes["/p1"] = (_recs("one"), "/p2", None)
    server.routes["/p2"] = (_recs("two"), None, None)

    _read(server, "/p1", token_env="CL_TEST_API_TOKEN")

    assert server.hits == [("/p1", "Bearer tok-for-test"), ("/p2", "Bearer tok-for-test")]
