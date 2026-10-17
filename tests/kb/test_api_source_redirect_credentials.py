"""The `api` source must not hand its credential to a different origin.

`_fetch_one` put `Authorization` in `Request(headers=)`, and urllib's redirect handler
copies those headers onto the request it builds for the new location. An open redirect on
the API host, or a compromised endpoint, therefore received the token. The `next` URL a
response names (a `Link: rel="next"` header or a `next_field` value) was fetched with the
same header and no origin check either. Bearer had the exposure already; Basic
`email:token` for Atlassian made it an account-wide credential.

An origin is scheme, host and port. A redirect to the same origin keeps the header, since
real APIs redirect within their own host. Anything that differs drops it.

Two real local servers on different ports stand in for the two origins. The assertion is on
what the SECOND server actually receives, which a mocked `urlopen` cannot show, and each
case asserts the second server was reached so a refused redirect cannot pass as a strip.

The hosted LLM and embeddings clients (`post_json`) are the same exposure: a `base_url`
that names a gateway which redirects sent `Authorization` (OpenAI) or `x-api-key`
(Anthropic) to the redirect target. The guarded opener drops both.
"""

from __future__ import annotations

import base64
import http.server
import json
import socketserver
import threading

import pytest

from contextlake.kb.sources.api import ApiSource, _same_origin

RECORDS = [{"id": "1", "title": "T", "text": "body"}]


class _Server:
    """One origin: serves `/final`, and redirects or links elsewhere on request."""

    def __init__(self):
        self.seen: list[dict] = []
        self.redirect_to: dict[str, str] = {}   # path -> absolute Location
        self.next_link: dict[str, str] = {}     # path -> absolute Link rel=next target
        self.body = RECORDS                     # what a non-redirect request is answered with
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append({"path": self.path,
                                   "authorization": self.headers.get("Authorization"),
                                   "x_api_key": self.headers.get("x-api-key")})
                if self.path in outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to[self.path])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps(outer.body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if self.path in outer.next_link:
                    self.send_header("Link", f'<{outer.next_link[self.path]}>; rel="next"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                # GraphQL sends a POST. Drain the body, then answer exactly as a GET
                # would; a 302 makes urllib re-issue the redirected request as a GET.
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.do_GET()

            def log_message(self, *a):
                pass

        self.srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def two_origins():
    a, b = _Server(), _Server()
    yield a, b
    a.close()
    b.close()


def _auths(server, path):
    return [s["authorization"] for s in server.seen if s["path"] == path]


def test_a_cross_origin_redirect_does_not_receive_the_credential(two_origins, monkeypatch):
    """FAILS before this change: the second server received `Bearer s3cr3t`."""
    a, b = two_origins
    a.redirect_to["/start"] = f"{b.base}/final"
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    docs = list(ApiSource(url=f"{a.base}/start", token_env="CL_TEST_API_TOKEN")
                .iter_documents())

    assert _auths(a, "/start") == ["Bearer s3cr3t"], "the first hop must still authenticate"
    assert _auths(b, "/final") == [None], (
        "the redirect target is a different origin and must not receive the credential")
    assert len(docs) == 1, "the redirect was not followed, so the strip was never exercised"


def test_a_cross_origin_redirect_does_not_receive_basic_credentials(two_origins, monkeypatch):
    a, b = two_origins
    a.redirect_to["/start"] = f"{b.base}/final"
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    docs = list(ApiSource(url=f"{a.base}/start", token_env="CL_TEST_API_TOKEN",
                          auth="basic", user="person@example.com").iter_documents())

    pair = base64.b64encode(b"person@example.com:s3cr3t").decode()
    assert _auths(a, "/start") == [f"Basic {pair}"]
    assert _auths(b, "/final") == [None]
    assert len(docs) == 1


def test_a_same_origin_redirect_keeps_the_credential(two_origins, monkeypatch):
    """The strip must not break an API that redirects within its own host."""
    a, _ = two_origins
    a.redirect_to["/start"] = f"{a.base}/final"
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    docs = list(ApiSource(url=f"{a.base}/start", token_env="CL_TEST_API_TOKEN")
                .iter_documents())

    assert _auths(a, "/final") == ["Bearer s3cr3t"]
    assert len(docs) == 1


def test_a_cross_origin_next_page_does_not_receive_the_credential(two_origins, monkeypatch):
    """The pagination path builds a fresh request per page, with no redirect involved."""
    a, b = two_origins
    a.next_link["/p1"] = f"{b.base}/final"
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    docs = list(ApiSource(url=f"{a.base}/p1", token_env="CL_TEST_API_TOKEN")
                .iter_documents())

    assert _auths(a, "/p1") == ["Bearer s3cr3t"]
    assert _auths(b, "/final") == [None]
    assert len(docs) == 2, "both pages should have been read"


def test_a_same_origin_next_page_keeps_the_credential(two_origins, monkeypatch):
    a, _ = two_origins
    a.next_link["/p1"] = f"{a.base}/final"
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    list(ApiSource(url=f"{a.base}/p1", token_env="CL_TEST_API_TOKEN").iter_documents())

    assert _auths(a, "/final") == ["Bearer s3cr3t"]


@pytest.mark.parametrize("one, two, same", [
    ("http://h.example/a", "http://h.example/b?q=1", True),
    ("http://h.example/a", "http://H.EXAMPLE/b", True),         # host is case-insensitive
    ("http://h.example/a", "http://h.example:80/b", True),      # default port is implied
    ("https://h.example/a", "https://h.example:443/b", True),
    ("http://h.example/a", "https://h.example/a", False),       # scheme differs
    ("https://h.example/a", "https://h.example:8443/a", False),  # port differs
    ("https://h.example/a", "https://other.example/a", False),  # host differs
])
def test_same_origin_is_scheme_host_and_port(one, two, same):
    assert _same_origin(one, two) is same


def test_a_graphql_source_does_not_send_its_token_across_origins(two_origins, monkeypatch):
    """The sibling of the `api` fix, which first landed on `api` alone.

    `graphql` sends the same bearer header through the same urllib. Until the guarded
    opener moved into `sources/base.py` it still called `urlopen`, which follows a
    redirect WITH the header, so an open redirect on a GraphQL endpoint received the
    token. FAILS when graphql.py goes back to `urllib.request.urlopen`.
    """
    from contextlake.kb.sources.graphql import GraphQLSource

    a, b = two_origins
    a.redirect_to["/graphql"] = f"{b.base}/final"
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    list(GraphQLSource(url=f"{a.base}/graphql", query="{ x }",
                       token_env="CL_TEST_API_TOKEN").iter_documents())

    assert _auths(a, "/graphql") == ["Bearer s3cr3t"], "the first hop must still authenticate"
    assert _auths(b, "/final") == [None], (
        "a different origin received the GraphQL source's bearer token")


# --- the hosted LLM and embeddings clients (`post_json`) ----------------------------------

def _openai_llm(base):
    from contextlake.kb.llm.openai import OpenAILlm
    OpenAILlm(base_url=base, api_key_env="CL_TEST_API_TOKEN").generate("hi")


def _anthropic_llm(base):
    from contextlake.kb.llm.anthropic import AnthropicLlm
    AnthropicLlm(base_url=base, api_key_env="CL_TEST_API_TOKEN").generate("hi")


def _openai_embedder(base):
    from contextlake.kb.embeddings.openai import OpenAIEmbedder
    OpenAIEmbedder(base_url=base, api_key_env="CL_TEST_API_TOKEN").embed(["hi"])


# (call, path the client posts to, header carrying the key, a reply the client can parse)
_HOSTED = [
    pytest.param(_openai_llm, "/chat/completions", "authorization",
                 {"choices": [{"message": {"content": "ok"}}]}, id="openai-llm"),
    pytest.param(_anthropic_llm, "/v1/messages", "x_api_key",
                 {"content": [{"type": "text", "text": "ok"}]}, id="anthropic-llm"),
    pytest.param(_openai_embedder, "/embeddings", "authorization",
                 {"data": [{"index": 0, "embedding": [0.1]}]}, id="openai-embeddings"),
]


@pytest.mark.parametrize("call, path, header, reply", _HOSTED)
def test_a_hosted_client_does_not_send_its_key_across_origins(
        two_origins, monkeypatch, call, path, header, reply):
    """FAILS before this change: the second server received the API key.

    `post_json` called `urlopen`, which re-issues a 302'd POST as a GET to the new
    location with the original headers. The Anthropic key travels in `x-api-key`, which
    urllib stores as `X-api-key`, so a guard that only pops `Authorization` still leaks it.
    """
    a, b = two_origins
    a.redirect_to[path] = f"{b.base}/final"
    b.body = reply
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    call(a.base)

    first = [s[header] for s in a.seen if s["path"] == path]
    assert first and first[0] and "s3cr3t" in first[0], "the first hop must still authenticate"
    second = [s for s in b.seen if s["path"] == "/final"]
    assert second, "the redirect was not followed, so the strip was never exercised"
    assert second[0]["authorization"] is None and second[0]["x_api_key"] is None, (
        "a different origin received the API key")


@pytest.mark.parametrize("call, path, header, reply", _HOSTED)
def test_a_hosted_client_keeps_its_key_on_a_same_origin_redirect(
        two_origins, monkeypatch, call, path, header, reply):
    """The strip must not break a gateway that redirects within its own host."""
    a, _ = two_origins
    a.redirect_to[path] = f"{a.base}/final"
    a.body = reply
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    call(a.base)

    hop = [s[header] for s in a.seen if s["path"] == "/final"]
    assert hop and "s3cr3t" in hop[0]
