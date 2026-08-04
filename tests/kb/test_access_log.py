"""The local HTTP servers' access log (off by default, opt in with --access-log).

``LocalHttpHandler.log_message`` used to be an unconditional ``pass``, which left
no way at all to answer "what did the dashboard serve, and to whom" -- the one
question an access log exists for, about a server that holds the whole code
graph. It stays silent unless asked, so the default behaviour is unchanged.
"""

import pytest

from contextlake import observability
from contextlake.kb.http_base import LocalHttpHandler


class _Handler(LocalHttpHandler):
    """A handler with no socket: ``BaseHTTPRequestHandler.__init__`` would try to
    serve a request, and all that is under test here is the logging method."""

    def __init__(self, client="192.0.2.5"):
        self._client = client

    def address_string(self):
        return self._client


@pytest.fixture(autouse=True)
def access_log_off():
    observability.set_access_log(False)
    yield
    observability.set_access_log(False)


def test_no_access_log_by_default(gls_logs):
    _Handler().log_message('"%s" %s %s', "GET /api/graph HTTP/1.1", "200", "-")
    assert gls_logs.text == ""


def test_access_log_records_the_request_and_client(gls_logs):
    observability.set_access_log(True)
    _Handler().log_message('"%s" %s %s', "GET /api/graph HTTP/1.1", "200", "-")

    assert "GET /api/graph HTTP/1.1" in gls_logs.text
    assert "192.0.2.5" in gls_logs.text
    record = gls_logs.records[-1]
    assert record.fields["client"] == "192.0.2.5"
    assert "200" in record.fields["http"]


def test_a_hostile_request_line_cannot_inject_into_the_log(gls_logs):
    """The request line is attacker-controlled and stdlib only started escaping
    control characters here in Python 3.10, so it is sanitised locally."""
    observability.set_access_log(True)
    _Handler().log_message('"%s" %s %s',
                           "GET /\r\n[fake] admin logged in\x1b[31m HTTP/1.1", "200", "-")

    logged = gls_logs.records[-1].getMessage()
    assert "\n" not in logged and "\r" not in logged and "\x1b" not in logged
    assert "fake" in logged  # sanitised, not silently dropped


def test_a_format_string_with_no_arguments_still_logs(gls_logs):
    # log_error() routes through log_message() too, sometimes with a bare string.
    observability.set_access_log(True)
    _Handler().log_message("code 404, message File not found")
    assert "404" in gls_logs.text
