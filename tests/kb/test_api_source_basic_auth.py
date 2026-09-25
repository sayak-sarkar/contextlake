"""The generic `api` source must be able to authenticate with HTTP Basic, not only Bearer.

Bearer-only silently failed against the most common API anyone points this at. Atlassian
Cloud (Jira and Confluence) accepts an API token ONLY as
``Authorization: Basic base64(email:token)``; a bearer header there returns 401 with a
body that does not say why, which reaches the operator as `0 documents` from a source
that looks correctly configured.

Driven through a real local HTTP server that echoes the Authorization header it received,
for the reason `test_api_source_pagination.py` gives: the behaviour under test is what
reaches the SERVER, and mocking `urlopen` would encode the same assumption twice. A unit
test over `_headers()` would pass against a build that never sent the header at all.
"""

from __future__ import annotations

import base64
import http.server
import json
import socketserver
import threading

import pytest

from contextlake.kb.sources.api import ApiSource


@pytest.fixture
def echo_server():
    """Serves one document and reports back the Authorization header it was sent."""
    seen: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["authorization"] = self.headers.get("Authorization")
            body = json.dumps([{"id": "1", "title": "T", "text": "body"}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:{srv.server_address[1]}/", seen
        srv.shutdown()


def test_basic_auth_sends_the_base64_pair_atlassian_requires(echo_server, monkeypatch):
    """FAILS before this change: the source sent `Bearer <token>`, which Atlassian 401s."""
    url, seen = echo_server
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    docs = list(ApiSource(url=url, token_env="CL_TEST_API_TOKEN", auth="basic",
                          user="person@example.com").iter_documents())

    assert len(docs) == 1, "the fixture served no document, so the header assertion is vacuous"
    expected = base64.b64encode(b"person@example.com:s3cr3t").decode()
    assert seen["authorization"] == f"Basic {expected}"


def test_bearer_stays_the_default(echo_server, monkeypatch):
    """Backward compatibility: a config with no `auth` key behaves exactly as before."""
    url, seen = echo_server
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    list(ApiSource(url=url, token_env="CL_TEST_API_TOKEN").iter_documents())

    assert seen["authorization"] == "Bearer s3cr3t"


def test_an_unknown_scheme_falls_back_to_bearer_rather_than_inventing_one(
        echo_server, monkeypatch):
    url, seen = echo_server
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    list(ApiSource(url=url, token_env="CL_TEST_API_TOKEN", auth="BaSiCally-wrong").iter_documents())

    assert seen["authorization"] == "Bearer s3cr3t"


def test_basic_without_a_user_says_so_instead_of_sending_half_a_pair(
        echo_server, monkeypatch, gls_logs):
    """`base64(":token")` is a well-formed header that 401s with no hint of the cause.

    Sending it would turn a config mistake into an opaque server error. The request still
    goes out, because refusing to fetch at all would abort a run over one misconfigured
    source, which `FetchFailures` exists to avoid.
    """
    url, seen = echo_server
    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")

    list(ApiSource(url=url, token_env="CL_TEST_API_TOKEN", auth="basic").iter_documents())

    assert seen["authorization"] is None
    # `gls_logs`, not capsys: the package logger sets propagate=False
    # (logging_setup.py:199), so caplog and stdout both miss these lines. An earlier
    # draft asserted on capsys and PASSED while capturing nothing.
    assert gls_logs.text, "captured nothing, so the assertion below is vacuous"
    assert "user" in gls_logs.text, "the misconfiguration was not reported"


def test_an_unset_token_env_is_reported_not_silently_anonymous(
        echo_server, monkeypatch, gls_logs):
    """The old code added no header and said nothing, so a request went out anonymous and
    the empty result looked like an empty source."""
    url, seen = echo_server
    monkeypatch.delenv("CL_TEST_API_TOKEN", raising=False)

    list(ApiSource(url=url, token_env="CL_TEST_API_TOKEN", auth="basic",
                   user="person@example.com").iter_documents())

    assert seen["authorization"] is None
    assert gls_logs.text, "captured nothing, so the assertions below are vacuous"
    assert "CL_TEST_API_TOKEN" in gls_logs.text
    assert "UNAUTHENTICATED" in gls_logs.text


def test_the_secret_never_appears_in_the_configured_source(monkeypatch):
    """The token lives in the environment, never on the instance or in config.

    `auth` and `user` are gated as privileged for the same reason: they decide how the
    secret is spent, so a discovered config must not be able to set them.
    """
    from contextlake.kb.trust import PRIVILEGED_SOURCE_KEYS

    monkeypatch.setenv("CL_TEST_API_TOKEN", "s3cr3t")
    src = ApiSource(url="http://x/", token_env="CL_TEST_API_TOKEN", auth="basic",
                    user="person@example.com")

    assert "s3cr3t" not in repr(vars(src))
    assert {"auth", "user", "token_env"} <= set(PRIVILEGED_SOURCE_KEYS)
