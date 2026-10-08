"""`kb enrich` over an `api` source with a `search_url`, against loopback fake servers.

The source exists so enrichment can search an issue tracker with an API token. These tests
pin what makes that safe to point at a real site: every request is a GET, the token reaches
only the configured origin, one page per query, a per-run document cap that stops querying,
a failed query (401) that keeps the previous results, and an `api` source without
`search_url` that enrichment never fetches.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from argparse import Namespace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from contextlake.kb.commands import cmd_enrich
from contextlake.kb.connectors.enrich import enrich_partition, render_search_urls
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, read_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.trust import PRIVILEGED_SOURCE_KEYS

TOKEN = "cl-canary-0a1b2c3d4e5f"   # invented for the test; reaches nothing real


class _Server:
    """A loopback JSON API that records every request it receives."""

    def __init__(self, behaviour):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _record(self):
                outer.requests.append({
                    "method": self.command, "path": self.path,
                    "auth": self.headers.get("Authorization")})

            def do_GET(self):
                self._record()
                status, headers, body = behaviour(self.path)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                self._record()
                self.send_response(405)
                self.end_headers()

            do_PUT = do_DELETE = do_PATCH = do_POST

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _term(path: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    return (q.get("text") or [""])[0]


def _issues(term: str, n: int = 2) -> dict:
    return {"issues": [{"key": f"ACME-{term}-{i}",
                        "fields": {"summary": f"{term} note {i}"}} for i in range(n)],
            "nextPageToken": "page-2"}


def _seed(store_dir, repos):
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    prov = Provenance(source_file="app/main.py", verified_at=date(2026, 10, 9))
    for repo in repos:
        nodes = [Node(id=f"{repo}:n1", repo=repo, kind="class", name="ForecastService",
                      file="app/forecast.py"),
                 Node(id=f"{repo}:n2", repo=repo, kind="function", name="readSensor",
                      file="app/readings.py")]
        edges = [Edge(src=f"{repo}:n1", dst=f"{repo}:n2", relation="calls",
                      confidence=Confidence.EXTRACTED, provenance=prov)]
        write_shard(store_dir, GraphShard(repo=repo, head_commit="h1", nodes=nodes, edges=edges))
        store.upsert_repo(Repo(id=repo, path=str(store_dir.parent / repo.replace("/", "_"))))
    store.close()


def _config(tmp_path, store_dir, search_url, *, search=True):
    key = "search_url" if search else "url"
    path = tmp_path / "kb.toml"
    path.write_text(f"""
[kb]
store_dir = "{store_dir.as_posix()}"

[embeddings]
enabled = false

[[sources]]
type = "api"
name = "tracker"
{key} = "{search_url}"
items = "issues"
id_field = "key"
title_field = "key"
text_field = "fields.summary"
auth = "basic"
user = "someone@example.test"
token_env = "CL_TEST_TRACKER_TOKEN"
""")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CL_TEST_TRACKER_TOKEN", TOKEN)
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    return tmp_path


def _run(cfg, repos, **extra):
    return cmd_enrich(Namespace(config=str(cfg), workspace=None, args=list(repos), **extra))


def test_every_request_is_a_get_and_the_token_stays_on_its_origin(env):
    other = _Server(lambda path: (200, {}, _issues("other")))
    redirect_term = "readSensor"

    def tracker(path):
        if _term(path) == redirect_term:
            return 302, {"Location": other.base + "/elsewhere"}, {}
        return 200, {}, _issues(_term(path))

    main = _Server(tracker)
    try:
        store_dir = env / "kb"
        _seed(store_dir, ["group/app"])
        cfg = _config(env, store_dir, main.base + "/search?text={term}")
        assert _run(cfg, ["group/app"]) == 0
    finally:
        main.close()
        other.close()
    seen = main.requests + other.requests
    assert seen and {r["method"] for r in seen} == {"GET"}
    assert all(r["auth"] and r["auth"].startswith("Basic ") for r in main.requests)
    assert other.requests, "control: the redirect was followed"
    assert all(r["auth"] is None for r in other.requests), "the token reached another origin"


def test_one_page_per_query(env):
    def tracker(path):
        nxt = {"Link": '<' + main.base + '/search?page=2>; rel="next"'}
        return 200, nxt, _issues(_term(path))

    main = _Server(tracker)
    try:
        store_dir = env / "kb"
        _seed(store_dir, ["group/app"])
        cfg = _config(env, store_dir, main.base + "/search?text={term}")
        assert _run(cfg, ["group/app"]) == 0
    finally:
        main.close()
    assert main.requests
    assert not [r for r in main.requests if "page=2" in r["path"]], "a second page was read"


def test_the_run_cap_stops_queries_and_keeps_repos_not_searched(env):
    main = _Server(lambda path: (200, {}, _issues(_term(path), n=3)))
    try:
        store_dir = env / "kb"
        _seed(store_dir, ["group/a", "group/b"])
        part_b = enrich_partition("group/b")
        old = Node(id=f"{part_b}:old", repo=part_b, kind="document", name="Kept")
        write_shard(store_dir, GraphShard(repo=part_b, head_commit="enrich", nodes=[old], edges=[]))
        cfg = _config(env, store_dir, main.base + "/search?text={term}")
        assert _run(cfg, ["group/a", "group/b"], max_documents=2) == 0
    finally:
        main.close()
    assert len(read_shard(store_dir, enrich_partition("group/a")).nodes) == 2
    kept = read_shard(store_dir, part_b)
    assert [n.name for n in kept.nodes] == ["Kept"], "a repo not searched lost its results"
    assert len(main.requests) == 1, f"queries went on after the cap: {len(main.requests)}"


def test_a_401_counts_as_unavailable_and_keeps_the_previous_results(env):
    main = _Server(lambda path: (401, {}, {"error": "unauthorized"}))
    try:
        store_dir = env / "kb"
        _seed(store_dir, ["group/app"])
        part = enrich_partition("group/app")
        old = Node(id=f"{part}:old", repo=part, kind="document", name="Kept")
        write_shard(store_dir, GraphShard(repo=part, head_commit="enrich", nodes=[old], edges=[]))
        cfg = _config(env, store_dir, main.base + "/search?text={term}")
        assert _run(cfg, ["group/app"]) == 1
    finally:
        main.close()
    assert main.requests, "control: the source was asked"
    assert [n.name for n in read_shard(store_dir, part).nodes] == ["Kept"]


def test_an_api_source_without_search_url_is_never_fetched(env, monkeypatch):
    """Beside a working term-searchable source, an ingest-style `api` source (fixed `url`)
    must be skipped: neither fetched nor reported unavailable, which would keep the
    repo's old results instead of storing the working source's answer."""
    import contextlake.kb.connectors.enrich as enrich
    from contextlake.kb.sources.base import Document

    monkeypatch.setattr(enrich, "mcp_tool_query", lambda cfg, terms, timeout=None: [
        Document(id="w1", title="Wiki page", text="ForecastService notes", uri="https://w.example/1")])
    main = _Server(lambda path: (200, {}, _issues("x")))
    try:
        store_dir = env / "kb"
        _seed(store_dir, ["group/app"])
        cfg = _config(env, store_dir, main.base + "/search?text=fixed", search=False)
        cfg.write_text(cfg.read_text() + """
[[sources]]
type = "mcp"
name = "wiki"
mcp = "http://127.0.0.1:9/mcp"
tool = "search"
""")
        assert _run(cfg, ["group/app"]) == 0
    finally:
        main.close()
    assert main.requests == []
    assert [n.name for n in read_shard(store_dir, enrich_partition("group/app")).nodes] == [
        "Wiki page"]


def test_dry_run_sends_nothing_and_prints_no_credential(env, capsys, gls_logs):
    main = _Server(lambda path: (200, {}, _issues(_term(path))))
    try:
        store_dir = env / "kb"
        _seed(store_dir, ["group/app"])
        cfg = _config(env, store_dir, main.base + "/search?text={term}")
        assert _run(cfg, ["group/app"], dry_run=True) == 0
    finally:
        main.close()
    out = gls_logs.text + capsys.readouterr().out
    assert main.requests == []
    assert "GET " + main.base + "/search?text=ForecastService" in out
    assert TOKEN not in out and "Authorization" not in out
    assert read_shard(store_dir, enrich_partition("group/app")) is None


def test_search_url_is_a_privileged_key():
    """It decides where code-derived search terms are sent, so a config discovered by
    walking up folders may not set it."""
    assert "search_url" in PRIVILEGED_SOURCE_KEYS


def test_search_urls_render_per_term_and_strip_quote_characters():
    assert render_search_urls('https://t.example/s?jql=text~"{term}"', ['Ab"c', 'x\\y']) == [
        'https://t.example/s?jql=text~"Abc"', 'https://t.example/s?jql=text~"xy"']
    assert render_search_urls("https://t.example/s?q={terms}", ["a", "b c"]) == [
        "https://t.example/s?q=a%20b%20c"]
    assert render_search_urls("https://t.example/s?q=fixed", ["a"]) == []
