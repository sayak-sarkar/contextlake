"""Source/plugin seam: built-in FilesSource, entry-point discovery, and `ingest`."""

import importlib.metadata as importlib_metadata
import json

import pytest

from contextlake.cli import main
from contextlake.kb.model import Node
from contextlake.kb.sources import build_source, discover_sources
from contextlake.kb.sources.files import FilesSource
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import read_shard
from contextlake.kb.store.sqlite_store import SqliteStore


def test_files_source_recurses_skips_binary_and_empty(tmp_path):
    (tmp_path / "a.md").write_text("# Title A\nbody\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("hello world")
    (tmp_path / "empty.md").write_text("   \n")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\x00\x00binary")

    docs = list(FilesSource(path=str(tmp_path)).iter_documents())
    assert sorted(d.title for d in docs) == ["a.md", "sub/b.txt"]  # empty + binary dropped
    a = next(d for d in docs if d.title == "a.md")
    assert "body" in a.text and a.uri.endswith("a.md")


def test_files_source_single_file(tmp_path):
    f = tmp_path / "one.md"
    f.write_text("solo")
    docs = list(FilesSource(path=str(f)).iter_documents())
    assert len(docs) == 1 and docs[0].text == "solo"


def test_registry_builtin_and_build_source():
    reg = discover_sources()
    assert {"files", "web"} <= set(reg)
    assert isinstance(build_source("files", path="."), FilesSource)
    assert build_source("nope-xyz") is None


def test_html_to_text_extracts_title_and_drops_script_style():
    from contextlake.kb.sources.web import html_to_text
    html = ("<html><head><title> Hello </title><style>x{color:red}</style></head>"
            "<body><h1>Heading</h1><script>bad()</script><p>Body text</p></body></html>")
    title, text = html_to_text(html)
    assert title == "Hello"
    assert "Heading" in text and "Body text" in text
    assert "bad()" not in text and "color:red" not in text


def test_web_source_yields_doc_with_mocked_fetch(monkeypatch):
    import contextlake.kb.sources.web as web

    class _Headers:
        def get_content_charset(self):
            return "utf-8"

    class _Resp:
        headers = _Headers()

        def read(self):
            return b"<title>Page</title><body><p>Hello web</p></body>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(web.urllib.request, "urlopen", lambda *a, **k: _Resp())
    docs = list(web.WebSource(url="https://example.com/x").iter_documents())
    assert len(docs) == 1
    assert docs[0].title == "Page" and "Hello web" in docs[0].text
    assert docs[0].uri == "https://example.com/x"


def test_web_source_skips_unreachable(monkeypatch):
    import contextlake.kb.sources.web as web

    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(web.urllib.request, "urlopen", boom)
    assert list(web.WebSource(urls=["https://x", "https://y"]).iter_documents()) == []


def test_api_source_maps_records_and_skips_textless(monkeypatch):
    from contextlake.kb.sources.api import ApiSource, _dig
    assert _dig({"data": {"items": [1]}}, "data.items") == [1]
    assert _dig({"a": 1}, "a.b") is None

    payload = {"data": {"items": [
        {"id": "1", "title": "One", "body": "first"},
        {"id": "2", "title": "Two", "body": ""},        # no text -> skipped
        {"id": "3", "title": "Three", "body": "third"},
    ]}}
    monkeypatch.setattr(ApiSource, "_fetch", lambda self: payload)
    docs = list(ApiSource(url="https://api/x", items="data.items",
                          text_field="body").iter_documents())
    assert [d.id for d in docs] == ["1", "3"]
    assert docs[0].title == "One" and docs[0].text == "first"


def test_api_source_uses_token_env_for_auth(monkeypatch):
    import contextlake.kb.sources.api as api

    captured = {}

    class _Resp:
        headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()

        def read(self):
            return b'[{"id":"a","text":"hi"}]'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return _Resp()

    monkeypatch.setenv("MY_TOKEN", "sekret")
    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    docs = list(api.ApiSource(url="https://api/x", token_env="MY_TOKEN").iter_documents())
    assert docs and docs[0].id == "a"
    assert captured["auth"] == "Bearer sekret"   # pulled from the env var, not config


def test_graphql_source_maps_records_and_skips_textless(monkeypatch):
    from contextlake.kb.sources.graphql import GraphQLSource

    payload = {"data": {"repository": {"issues": {"nodes": [
        {"id": "1", "title": "One", "body": "first"},
        {"id": "2", "title": "Two", "body": ""},        # no text -> skipped
        {"id": "3", "title": "Three", "body": "third"},
    ]}}}}
    monkeypatch.setattr(GraphQLSource, "_fetch", lambda self: payload)
    docs = list(GraphQLSource(url="https://api/graphql", query="{ x }",
                              items="repository.issues.nodes",
                              text_field="body").iter_documents())
    assert [d.id for d in docs] == ["1", "3"]
    assert docs[0].title == "One" and docs[0].text == "first"
    assert docs[0].uri == "https://api/graphql"


def test_graphql_source_skips_on_reported_errors(monkeypatch):
    from contextlake.kb.sources.graphql import GraphQLSource

    payload = {"data": {"x": [{"id": "1", "text": "partial"}]},
               "errors": [{"message": "field x.y not found"}]}
    monkeypatch.setattr(GraphQLSource, "_fetch", lambda self: payload)
    docs = list(GraphQLSource(url="https://api/graphql", query="{ x }",
                              items="x").iter_documents())
    assert docs == []  # never trust partial data alongside a reported error


def test_graphql_source_noop_without_url_or_query():
    from contextlake.kb.sources.graphql import GraphQLSource
    assert list(GraphQLSource(url="https://api/graphql").iter_documents()) == []
    assert list(GraphQLSource(query="{ x }").iter_documents()) == []


def test_graphql_source_uses_token_env_and_posts_query(monkeypatch):
    import contextlake.kb.sources.graphql as graphql

    captured = {}

    class _Resp:
        headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()

        def read(self):
            return b'{"data": {"id": "a", "text": "hi"}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setenv("MY_TOKEN", "sekret")
    monkeypatch.setattr(graphql.urllib.request, "urlopen", fake_urlopen)
    docs = list(graphql.GraphQLSource(
        url="https://api/graphql", query="{ x }", variables={"n": 1},
        token_env="MY_TOKEN").iter_documents())
    assert docs and docs[0].id == "a"
    assert captured["auth"] == "Bearer sekret"
    assert captured["method"] == "POST"
    assert captured["body"] == {"query": "{ x }", "variables": {"n": 1}}


def test_registry_has_graphql():
    assert "graphql" in discover_sources()


def test_registry_has_mcp():
    assert "mcp" in discover_sources()


def test_mcp_texts_extracts_text_skips_blobs():
    from contextlake.kb.sources.mcp import _texts

    class _C:
        def __init__(self, text=None):
            self.text = text

    class _R:
        contents = [_C("hello"), _C(None), _C("world")]

    assert _texts(_R()) == "hello\nworld"


def test_mcp_source_noop_without_target():
    from contextlake.kb.sources.mcp import McpSource
    assert list(McpSource().iter_documents()) == []


def test_mcp_read_all_maps_resources():
    import asyncio

    from contextlake.kb.sources.mcp import McpSource

    class _Res:
        def __init__(self, uri, name):
            self.uri, self.name, self.mimeType = uri, name, "text/plain"

    class _ListResult:
        resources = [_Res("res://a", "Doc A"), _Res("res://b", "Doc B")]

    class _Content:
        def __init__(self, text):
            self.text = text

    class _ReadResult:
        def __init__(self, text):
            self.contents = [_Content(text)]

    class _Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            pass

        async def list_resources(self):
            return _ListResult()

        async def read_resource(self, uri):
            return _ReadResult(f"body of {uri}")

    docs = asyncio.run(McpSource._read_all(_Session, None, None))
    assert [d.id for d in docs] == ["res://a", "res://b"]
    assert docs[0].title == "Doc A" and "body of res://a" in docs[0].text
    assert docs[0].attrs["mimeType"] == "text/plain"


def test_plugin_discovery_via_entry_points(monkeypatch):
    class _Plugin:
        def __init__(self, **_):
            pass

        def iter_documents(self):
            return []

    class _EP:
        name = "myplugin"

        def load(self):
            return _Plugin

    class _EPs:
        def select(self, group=None):
            return [_EP()] if group == "contextlake.sources" else []

    monkeypatch.setattr(importlib_metadata, "entry_points", lambda: _EPs())
    found = discover_sources()
    assert found.get("myplugin") is _Plugin and "files" in found  # plugin + built-in


def test_broken_plugin_is_skipped_not_fatal(monkeypatch):
    class _BadEP:
        name = "broken"

        def load(self):
            raise RuntimeError("boom")

    class _EPs:
        def select(self, group=None):
            return [_BadEP()]

    monkeypatch.setattr(importlib_metadata, "entry_points", lambda: _EPs())
    found = discover_sources()
    assert "broken" not in found and "files" in found  # discovery survived


def test_cmd_ingest_writes_document_nodes(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))   # isolate from any real ~/.contextlake
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("# Guide\nstep one\n")
    (docs_dir / "faq.md").write_text("# FAQ\nq and a\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", str(docs_dir), "--config", str(cfg)])
    assert e.value.code == 0

    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    try:
        # documents land as nodes under the synthetic @ingest: partition
        n = store.get_node("@ingest:cli:guide.md")
        assert n is not None and n.kind == "document" and n.name == "guide.md"
        assert n.repo == "@ingest:cli"
        assert any(h.id == "@ingest:cli:faq.md" for h in store.search("faq"))
    finally:
        store.close()
    out = capsys.readouterr().out
    assert "2 document(s)" in out
    assert "✓ Ingest complete: 2 document(s) aggregated" in out   # glyph-prefixed summary


def _seed_indexed_symbol(store_dir, repo_id="team/api"):
    """An indexed repo carrying one function symbol, so ingest has real code to
    match document text against."""
    store_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        check_schema(store)
        store.upsert_nodes(repo_id, [
            Node(id="team_api_chargeCard", repo=repo_id, kind="function",
                 name="chargeCard", file="billing.py"),
        ])
    finally:
        store.close()


def test_cmd_ingest_links_documents_to_the_symbols_they_mention(tmp_path, capsys,
                                                                monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runbook.md").write_text("If chargeCard fails, check the gateway logs.\n")
    (docs_dir / "offsite.md").write_text("Lunch is at noon.\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    _seed_indexed_symbol(tmp_path / "kb")

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", str(docs_dir), "--for-repo", "team/api",
              "--config", str(cfg)])
    assert e.value.code == 0
    assert "team/api" not in capsys.readouterr().out   # a valid target never warns

    shard = read_shard(tmp_path / "kb", "@ingest:cli")
    runbook = "@ingest:cli:runbook.md"
    assert any(e.src == "team_api_chargeCard" and e.dst == runbook
               and e.relation == "documented_by" for e in shard.edges)
    # a document mentioning nothing gets no edges at all, not even the repo-level one
    assert not any(e.dst == "@ingest:cli:offsite.md" for e in shard.edges)

    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    try:  # live in the index too, not only in the shard
        out = store.neighbors("team_api_chargeCard", direction="out")
        assert [e.dst for e in out] == [runbook]
    finally:
        store.close()


def test_cmd_ingest_without_a_target_repo_writes_no_edges(tmp_path, monkeypatch):
    """The safe default: with no `--for-repo`, ingest has no notion of which
    indexed repo a document is about, so it links nothing -- exactly as before."""
    monkeypatch.setenv("HOME", str(tmp_path))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runbook.md").write_text("If chargeCard fails, check the gateway logs.\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    _seed_indexed_symbol(tmp_path / "kb")

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", str(docs_dir), "--config", str(cfg)])
    assert e.value.code == 0
    assert read_shard(tmp_path / "kb", "@ingest:cli").edges == []


def test_cmd_ingest_source_config_can_name_its_target_repo(tmp_path, monkeypatch):
    """`for_repo` on a `[[sources]]` entry is the config-driven equivalent of
    `--for-repo`, and must not leak into the source constructor's options."""
    monkeypatch.setenv("HOME", str(tmp_path))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runbook.md").write_text("If chargeCard fails, check the gateway logs.\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        '[[sources]]\ntype = "files"\nname = "docs"\n'
        f'path = "{docs_dir}"\nfor_repo = "team/api"\n'
    )
    _seed_indexed_symbol(tmp_path / "kb")

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--config", str(cfg)])
    assert e.value.code == 0
    shard = read_shard(tmp_path / "kb", "@ingest:docs")
    assert any(e.src == "team_api_chargeCard" for e in shard.edges)


def test_cmd_ingest_unknown_target_repo_warns_and_links_nothing(tmp_path, capsys,
                                                                monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runbook.md").write_text("If chargeCard fails, check the gateway logs.\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    _seed_indexed_symbol(tmp_path / "kb")

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", str(docs_dir), "--for-repo", "team/typo",
              "--config", str(cfg)])
    assert e.value.code == 0
    assert "team/typo" in capsys.readouterr().out          # a typo is never silent
    assert read_shard(tmp_path / "kb", "@ingest:cli").edges == []


def test_cmd_ingest_skips_disabled_sources(tmp_path, capsys, monkeypatch):
    """A configured `[[sources]]` entry with `enabled = false` must not be
    turned into an ingest job at all -- `disable` has to be a real no-op
    guarantee across both connect and ingest."""
    monkeypatch.setenv("HOME", str(tmp_path))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("# Guide\nstep one\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        '[[sources]]\ntype = "files"\nname = "docs"\n'
        f'path = "{docs_dir}"\nenabled = false\n'
    )

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--config", str(cfg)])
    assert e.value.code == 0
    assert "No document sources" in capsys.readouterr().out

    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    try:
        assert store.get_node("@ingest:docs:guide.md") is None
    finally:
        store.close()


# --- ingest fetchers open network URLs only --------------------------------
#
# `[[sources]] url` can arrive from a config found by walking up from the cwd
# (kb/trust.py explains why that file is not trusted), and contextlake clones
# repositories into the workspace itself. `urllib` speaks `file:` as happily as
# `https:`, so without a scheme allowlist an ingest run read local files into the
# graph -- where they surface in the wiki, the dashboard and every MCP client.
#
# These drive a real canary file rather than asserting the helper was called: the
# question is whether the *fetcher* opens it, and a mocked urlopen cannot answer
# that.

_CANARY = "SECRET-CANARY-LINE"


def test_web_source_refuses_file_urls(tmp_path, caplog):
    import contextlake.kb.sources.web as web

    secret = tmp_path / "secret.txt"
    secret.write_text(f"<body>{_CANARY}</body>\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        docs = list(web.WebSource(url=secret.as_uri()).iter_documents())

    assert docs == []
    assert _CANARY not in str([d.text for d in docs])
    # and it says so -- a silent skip would look identical to an empty page
    assert "refusing to fetch" in caplog.text


def test_web_source_refuses_other_non_network_schemes(tmp_path):
    import contextlake.kb.sources.web as web

    for url in ("file:///etc/passwd", "ftp://host/x", "data:text/html,<p>x",
                "FILE:///etc/passwd", "/etc/passwd"):
        assert list(web.WebSource(url=url).iter_documents()) == []


def test_web_source_still_fetches_http_and_https(monkeypatch):
    """The allowlist must not break the case the source exists for."""
    import contextlake.kb.sources.web as web

    class _Headers:
        def get_content_charset(self):
            return "utf-8"

    class _Resp:
        headers = _Headers()

        def read(self):
            return b"<title>P</title><body><p>Hi</p></body>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(web.urllib.request, "urlopen", lambda *a, **k: _Resp())
    for url in ("http://example.net/x", "https://example.net/x"):
        docs = list(web.WebSource(url=url).iter_documents())
        assert len(docs) == 1, url


def test_api_and_graphql_sources_refuse_file_urls(tmp_path):
    from contextlake.kb.sources.api import ApiSource
    from contextlake.kb.sources.graphql import GraphQLSource

    secret = tmp_path / "secret.json"
    secret.write_text(f'[{{"id": "1", "text": "{_CANARY}"}}]', encoding="utf-8")

    assert list(ApiSource(url=secret.as_uri()).iter_documents()) == []
    assert list(GraphQLSource(url=secret.as_uri(), query="{ x }").iter_documents()) == []
