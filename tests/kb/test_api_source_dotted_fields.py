"""`id_field`, `title_field` and `text_field` on the `api` source accept dotted paths.

The Atlassian example in `docs/document-sources.md` sets `text_field = "fields.summary"`.
Only `items` and `next_field` went through the dotted-path resolver, so the lookup was a
flat `rec.get("fields.summary")`, which is None on every Jira record. The record was
skipped as textless and the source reported zero documents with no error. A user who
copied the headline example for the Basic-auth feature got nothing and no hint why.

The first test below runs the DOCUMENTED EXAMPLE LITERALLY: it extracts the TOML block
from the docs page, parses it, points `url` at a local server that answers in the shape of
the Jira search API, and builds the source through `build_source`, the same factory the
ingest command uses. A hand-copied config in a test would pass while the page stayed
broken. The server is real HTTP for the reason `test_api_source_basic_auth.py` gives.
"""

from __future__ import annotations

import http.server
import json
import re
import socketserver
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from contextlake.kb.sources.api import ApiSource
from contextlake.kb.sources.base import build_source

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10, the same fallback kb/config.py carries
    import tomli as tomllib

DOCS = Path(__file__).resolve().parents[2] / "docs" / "document-sources.md"

# The Jira search API returns `{"issues": [{"key": ..., "fields": {"summary": ...}}]}`.
# Names and text are invented.
JIRA_SHAPED = {
    "startAt": 0,
    "total": 3,
    "issues": [
        {"id": "10001", "key": "DEMO-1", "fields": {"summary": "Retry the nightly export"}},
        {"id": "10002", "key": "DEMO-2", "fields": {"summary": "Rotate the signing key"}},
        # No summary: a record without text is still skipped, as before.
        {"id": "10003", "key": "DEMO-3", "fields": {"summary": None}},
    ],
}


@pytest.fixture
def jira_like_server():
    """Serves the Jira-shaped payload on any path and records the request line."""
    seen: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization")
            body = json.dumps(JIRA_SHAPED).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{srv.server_address[1]}", seen
        srv.shutdown()


def _documented_atlassian_entry() -> dict:
    """The `[[sources]]` table from the Atlassian example on the docs page, parsed."""
    page = DOCS.read_text(encoding="utf-8")
    blocks = re.findall(r"```toml\n(.*?)```", page, flags=re.DOTALL)
    hits = [b for b in blocks if "atlassian.net/rest/api" in b]
    assert len(hits) == 1, (
        f"expected one Atlassian example on {DOCS.name}, found {len(hits)}")
    return tomllib.loads(hits[0])["sources"][0]


def test_the_documented_atlassian_example_ingests_the_issues(jira_like_server, monkeypatch):
    """FAILS before this change: the example yielded `[]`, because `fields.summary` was
    looked up as a flat key."""
    base, seen = jira_like_server
    entry = _documented_atlassian_entry()
    # Keep the documented path and query, swap only the host.
    doc_url = urlsplit(entry["url"])
    entry["url"] = base + doc_url.path + (f"?{doc_url.query}" if doc_url.query else "")
    monkeypatch.setenv(entry["token_env"], "tok")
    opts = {k: v for k, v in entry.items() if k not in ("type", "name")}

    src = build_source(entry["type"], **opts)
    docs = list(src.iter_documents())

    assert seen, "the fixture server was never called, so the document assertions are vacuous"
    assert [d.id for d in docs] == ["DEMO-1", "DEMO-2"]
    assert [d.title for d in docs] == ["DEMO-1", "DEMO-2"]
    assert [d.text for d in docs] == ["Retry the nightly export", "Rotate the signing key"]


def test_a_dotted_title_field_resolves_and_a_missing_one_falls_back_to_the_id(
        jira_like_server):
    base, _ = jira_like_server
    docs = list(ApiSource(url=base, items="issues", id_field="key",
                          title_field="fields.summary",
                          text_field="fields.summary").iter_documents())
    assert [d.title for d in docs] == ["Retry the nightly export", "Rotate the signing key"]

    # `fields.nope` is absent on every record, so the title falls back to the id.
    docs = list(ApiSource(url=base, items="issues", id_field="key",
                          title_field="fields.nope",
                          text_field="fields.summary").iter_documents())
    assert [d.title for d in docs] == ["DEMO-1", "DEMO-2"]


def test_a_dotted_id_field_resolves(jira_like_server):
    base, _ = jira_like_server
    docs = list(ApiSource(url=base, items="issues", id_field="fields.summary",
                          text_field="fields.summary").iter_documents())
    assert [d.id for d in docs] == ["Retry the nightly export", "Rotate the signing key"]


def test_flat_field_names_keep_working(monkeypatch):
    """Backward compatibility: every config written before this change names a flat key."""
    flat = [{"id": "a", "title": "A", "text": "alpha"},
            {"id": "b", "title": "B", "text": ""}]
    monkeypatch.setattr(ApiSource, "_fetch", lambda self: flat)
    docs = list(ApiSource(url="http://127.0.0.1:9/x").iter_documents())
    assert [(d.id, d.title, d.text) for d in docs] == [("a", "A", "alpha")]


def test_a_flat_key_that_contains_a_dot_still_wins(monkeypatch):
    """A record may use a literal key such as `fields.summary`. The old flat lookup read
    it, so the new resolver must not hide it behind a nested-path miss."""
    recs = [{"id": "x", "fields.summary": "literal dotted key"}]
    monkeypatch.setattr(ApiSource, "_fetch", lambda self: recs)
    docs = list(ApiSource(url="http://127.0.0.1:9/x",
                          text_field="fields.summary").iter_documents())
    assert [d.text for d in docs] == ["literal dotted key"]
