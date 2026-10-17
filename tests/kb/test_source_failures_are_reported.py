"""An unreachable source must not read as an empty one.

The measured defect: four of nine source types swallowed every fetch exception with no
log line, so a wrong URL, an expired token, an HTTP 500, a proxy block and a genuinely
empty page all produced the same `✓ 0 documents`, exit 0. On a content pipeline that
means ingestion silently stops and nothing in CI can detect it.

`sources/files.py` was already the in-house standard -- it names every file it skips and
why -- so this is that standard applied to the network sources, plus a machine-readable
tally so `cmds/ingest.py` can tell "empty" from "broken" without parsing log lines.
"""

from __future__ import annotations

import pytest

from contextlake.kb.sources.api import ApiSource
from contextlake.kb.sources.graphql import GraphQLSource
from contextlake.kb.sources.web import WebSource


def _unreachable(monkeypatch, module):
    """Make the module's fetch raise the way a dead endpoint does."""
    def boom(*a, **k):
        raise OSError("Connection refused")
    monkeypatch.setattr(module, "urlopen", boom, raising=False)


@pytest.mark.parametrize("make", [
    lambda: WebSource(url="http://127.0.0.1:9/nope"),
    lambda: ApiSource(url="http://127.0.0.1:9/nope"),
    lambda: GraphQLSource(url="http://127.0.0.1:9/nope", query="{ x }"),
])
def test_an_unreachable_source_records_the_miss_instead_of_swallowing_it(make):
    """THE LOAD-BEARING ASSERTION. Port 9 (discard) refuses fast and deterministically.

    Before the fix every one of these yielded nothing and recorded nothing, which is
    indistinguishable from success on an empty source.
    """
    src = make()
    docs = list(src.iter_documents())

    assert docs == [], "the fixture should not reach anything"
    assert getattr(src, "failures", None), (
        f"{type(src).__name__} yielded nothing and recorded nothing — a caller cannot "
        "tell this from a source that is simply empty")
    target, reason = src.failures[0]
    assert target, "the miss must name what could not be read"
    assert reason and ":" in reason, (
        f"the reason must name the exception type, got {reason!r}")


def test_failures_reset_between_runs():
    """The near-miss: a stale failure from a previous call would make a later healthy
    run look broken, which is the same lie in the other direction."""
    src = WebSource(url="http://127.0.0.1:9/nope")
    list(src.iter_documents())
    assert src.failures
    src.urls = []                      # nothing to fetch, nothing to fail
    list(src.iter_documents())
    assert src.failures == [], "failures carried over from the previous run"


def test_a_reachable_but_empty_source_records_no_failure(monkeypatch):
    """The other near-miss, and the one that keeps the signal worth reading: an empty
    source is NOT a failure. If everything reported a miss, the field would be noise."""
    import contextlake.kb.sources.web as webmod

    class _Resp:
        headers = type("H", (), {"get_content_charset": staticmethod(lambda: "utf-8")})()

        def read(self):
            return b"<html><body></body></html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(webmod, "url_is_fetchable", lambda *a, **k: True)
    monkeypatch.setattr(webmod.urllib.request, "urlopen", lambda *a, **k: _Resp())

    src = WebSource(url="http://example.invalid/empty")
    assert list(src.iter_documents()) == []
    assert src.failures == [], "an empty page was reported as a failure"


# --- the escape hatch the release note promises must actually exist -----------------

def test_the_exit_zero_flag_is_a_pre_command_global_and_the_message_says_so():
    """The CHANGELOG promises `--exit-zero-on-partial` as the one-flag escape, so the
    flag has to work AND the message has to show where it goes.

    It is a pre-command global: `contextlake --exit-zero-on-partial kb ingest` exits 0,
    while `kb ingest --exit-zero-on-partial` is rejected by argparse with exit 2 -- a
    worse outcome than the exit code the user was trying to suppress. Naming the flag
    bare, as the first version of the message did, sends people to the broken form.
    """
    import inspect

    from contextlake.kb.cmds import ingest

    src = inspect.getsource(ingest.cmd_ingest)
    assert "contextlake --exit-zero-on-partial kb ingest" in src, (
        "the message names the flag without its position, so a user will type it after "
        "the subcommand and get an argparse error")


# --- a source that sent no request must not be called "reachable" -------------------
#
# `cmds/ingest.py` printed "no documents (source reachable, nothing to ingest)" for any
# empty source that recorded no failure. An `api` source holding only a `search_url`
# sends nothing at ingest time (`kb enrich` queries it), so the word "reachable" was a
# claim no request had checked.

_SEARCH_ONLY = "an api source with only search_url is queried by kb enrich, not kb ingest"


class _NoNetwork:
    """Stands in for a urllib opener and counts how often it is asked to open anything."""

    def __init__(self):
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append(getattr(req, "full_url", req))
        raise OSError("this test must not reach the network")


class _EmptyListResponse:
    """A 200 answer whose body is an empty JSON list: a request was sent, nothing came."""

    class headers:                                           # noqa: N801 - mimics a message
        @staticmethod
        def get_content_charset():
            return "utf-8"

        @staticmethod
        def get(_name, default=None):
            return default

    def read(self):
        return b"[]"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ingest_config(tmp_path, body: str):
    # Embeddings off, as in test_ingest_reports_a_type_it_cannot_run.py.
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
                   '[embeddings]\nenabled = false\n' + body)
    return cfg


def _run_ingest(cfg, capsys):
    from contextlake.cli import main

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--config", str(cfg)])
    out = capsys.readouterr()
    return e.value.code, out.out + out.err


def test_ingest_says_an_api_source_with_only_search_url_sent_nothing(
        tmp_path, monkeypatch, capsys):
    import contextlake.kb.sources.api as apimod

    monkeypatch.setenv("HOME", str(tmp_path))
    net = _NoNetwork()
    monkeypatch.setattr(apimod, "_OPENER", net)
    cfg = _ingest_config(tmp_path, (
        '\n[[sources]]\nname = "jira"\ntype = "api"\n'
        'search_url = "https://tracker.invalid/search?q={term}"\n'))

    code, printed = _run_ingest(cfg, capsys)

    assert printed.strip(), "the capture must see something or this proves nothing"
    assert net.calls == [], f"the stub saw a request: {net.calls}"
    assert code == 0, "the exit code is unchanged: an enrich-only source is not a failure"
    assert f"jira: nothing to ingest: {_SEARCH_ONLY}" in printed
    assert "source reachable" not in printed


def test_ingest_still_says_reachable_when_a_request_was_sent_and_found_nothing(
        tmp_path, monkeypatch, capsys):
    """The near-miss: a source that DID send a request keeps the old line, so the new
    wording means "no request" and nothing wider."""
    import contextlake.kb.sources.api as apimod

    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    class _Opener:
        @staticmethod
        def open(req, timeout=None):
            calls.append(req.full_url)
            return _EmptyListResponse()

    monkeypatch.setattr(apimod, "_OPENER", _Opener)
    cfg = _ingest_config(tmp_path, (
        '\n[[sources]]\nname = "tickets"\ntype = "api"\nurl = "http://tracker.invalid/items"\n'))

    code, printed = _run_ingest(cfg, capsys)

    assert printed.strip(), "the capture must see something or this proves nothing"
    assert calls == ["http://tracker.invalid/items"], "the source should have sent one request"
    assert code == 0
    assert "tickets: no documents (source reachable, nothing to ingest)" in printed
    assert "nothing to ingest:" not in printed


def test_ingest_names_a_source_that_cannot_be_built_and_runs_the_rest(
        tmp_path, monkeypatch, capsys):
    """`build_source` ran outside the per-source `try`, so one source whose constructor
    raised stopped `kb ingest` with an error that named no source, and the valid source
    after it never ran. An `mcp` `env` that is a string makes `dict(env)` raise."""
    monkeypatch.setenv("HOME", str(tmp_path))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# Alpha\nbody")
    cfg = _ingest_config(tmp_path, (
        '\n[[sources]]\nname = "broken"\ntype = "mcp"\nurl = "http://t.invalid/m"\n'
        'env = "not-a-table"\n'
        f'\n[[sources]]\nname = "notes"\ntype = "files"\npath = "{docs}"\n'))

    code, printed = _run_ingest(cfg, capsys)

    assert "broken: source failed" in printed, printed
    assert code == 1, "a source that could not run is a failed run"
    assert "Ingest incomplete: 1 document(s)" in printed, (
        "the source after the broken one did not run")


@pytest.mark.parametrize(("make", "expect"), [
    (lambda: ApiSource(search_url="https://t.invalid/s?q={term}"), _SEARCH_ONLY),
    (lambda: ApiSource(), "no url"),
    (lambda: ApiSource(url="file:///dev/null"), "only http and https"),
    (lambda: GraphQLSource(url="http://t.invalid/graphql"), "both url and query"),
    (lambda: GraphQLSource(url="file:///dev/null", query="{ x }"), "only http and https"),
    (lambda: WebSource(), "no url or urls"),
    (lambda: WebSource(urls=["file:///dev/null", "ftp://t.invalid/x"]),
     "only http and https"),
])
def test_a_source_with_nothing_to_send_says_why(make, expect, monkeypatch):
    import contextlake.kb.sources.api as apimod
    import contextlake.kb.sources.graphql as gqlmod

    net = _NoNetwork()
    monkeypatch.setattr(apimod, "_OPENER", net)
    monkeypatch.setattr(gqlmod, "_OPENER", net)
    monkeypatch.setattr("urllib.request.urlopen", net.open)

    src = make()
    assert list(src.iter_documents()) == []

    assert net.calls == [], f"the stub saw a request: {net.calls}"
    assert src.failures == [], "a config that asks for no request is not a failure"
    assert src.no_request_reason and expect in src.no_request_reason


def test_the_mcp_source_with_no_server_says_why():
    from contextlake.kb.sources.mcp import McpSource

    src = McpSource()
    assert list(src.iter_documents()) == []
    assert src.failures == []
    assert src.no_request_reason and "no command or url" in src.no_request_reason


def test_a_source_that_sent_a_request_leaves_no_request_reason_unset(monkeypatch):
    """The reason is cleared per run, and never set once a request went out."""
    import contextlake.kb.sources.api as apimod

    monkeypatch.setattr(apimod, "_OPENER", _NoNetwork())
    src = ApiSource(url="http://t.invalid/x")
    list(src.iter_documents())                      # the stub raises: a request was tried
    assert src.failures, "the request was sent and failed"
    assert src.no_request_reason is None

    src.url = None
    list(src.iter_documents())
    assert src.no_request_reason, "no url on the second run"
    src.url = "http://t.invalid/x"
    list(src.iter_documents())
    assert src.no_request_reason is None, "a reason from the previous run carried over"


# --- a missed target must not delete the document the last run stored for it ---------
#
# `kb ingest` rewrote a source's partition from whatever came back, so one page that
# answered 500 deleted its document from the graph and the vectors, while the run said
# "Nothing was silently dropped". `kb connect` and `kb enrich` keep what they had.

_PAGES = {
    "http://pages.test/a": "<html><title>Alpha page</title><body>alpha body</body></html>",
    "http://pages.test/flip": "<html><title>Flip page</title><body>flip body</body></html>",
}


class _Page:
    class headers:                                           # noqa: N801 - mimics a message
        @staticmethod
        def get_content_charset():
            return "utf-8"

    def __init__(self, html):
        self._html = html

    def read(self):
        return self._html.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve_pages(monkeypatch, down):
    """A stand-in for the network: every page in `_PAGES` answers, except those in `down`."""
    import contextlake.kb.sources.web as webmod

    def urlopen(req, timeout=None):
        if req.full_url in down:
            raise OSError("HTTP Error 500: Internal Server Error")
        return _Page(_PAGES[req.full_url])

    monkeypatch.setattr(webmod, "url_is_fetchable", lambda *a, **k: True)
    monkeypatch.setattr(webmod.urllib.request, "urlopen", urlopen)


def _web_config(tmp_path, urls):
    listed = ", ".join(f'"{u}"' for u in urls)
    return _ingest_config(tmp_path, (
        f'\n[[sources]]\nname = "site"\ntype = "web"\nurls = [{listed}]\n'))


def _stored_titles(tmp_path):
    from contextlake.kb.store.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    try:
        return sorted(r[0] for r in store.conn.execute(
            "SELECT name FROM nodes WHERE repo_id=?", ("@ingest:site",)))
    finally:
        store.close()


def test_a_missed_target_keeps_the_document_the_last_run_stored(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    down: set = set()
    _serve_pages(monkeypatch, down)
    cfg = _web_config(tmp_path, list(_PAGES))

    code, _ = _run_ingest(cfg, capsys)
    assert code == 0
    assert _stored_titles(tmp_path) == ["Alpha page", "Flip page"]

    down.add("http://pages.test/flip")
    code, printed = _run_ingest(cfg, capsys)
    assert code == 1, "an unreadable target is still an incomplete run"
    assert _stored_titles(tmp_path) == ["Alpha page", "Flip page"], (
        "the page that answered 500 lost its document")
    assert "site: 1 document(s), kept 1 from the last run, 1 target(s) unreadable" in printed

    # A run that reads every target replaces the partition, so a page dropped from the
    # config does not stay for good.
    down.clear()
    code, _ = _run_ingest(_web_config(tmp_path, ["http://pages.test/a"]), capsys)
    assert code == 0
    assert _stored_titles(tmp_path) == ["Alpha page"]


def test_a_missed_target_keeps_the_vectors_of_the_documents_it_keeps(
        tmp_path, monkeypatch, capsys):
    """Sweeping the partition's vectors before the write would delete the vector of the
    document the graph just kept, and semantic search would stop finding it."""
    from contextlake.kb.embeddings.store import VectorStore

    class _Embedder:
        name = "fake-embedder"

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("contextlake.kb.embeddings.build_embedder", lambda cfg_: _Embedder())
    monkeypatch.setattr("contextlake.kb.embeddings.store.build_vector_store",
                        lambda path, **kw: VectorStore(path))
    down: set = set()
    _serve_pages(monkeypatch, down)
    cfg = _web_config(tmp_path, list(_PAGES))
    cfg.write_text(cfg.read_text().replace("enabled = false", "enabled = true"))

    def vector_rows():
        vs = VectorStore(tmp_path / "kb" / "embeddings.sqlite")
        try:
            return vs.conn.execute("SELECT COUNT(*) FROM embeddings WHERE repo_id=?",
                                   ("@ingest:site",)).fetchone()[0]
        finally:
            vs.close()

    assert _run_ingest(cfg, capsys)[0] == 0
    assert vector_rows() == 2

    down.add("http://pages.test/flip")
    assert _run_ingest(cfg, capsys)[0] == 1
    assert vector_rows() == 2, "the kept document lost its vector"
