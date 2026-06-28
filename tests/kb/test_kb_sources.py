"""Source/plugin seam: built-in FilesSource, entry-point discovery, and `ingest`."""

import importlib.metadata as importlib_metadata

import pytest

from contextlake.cli import main
from contextlake.kb.sources import build_source, discover_sources
from contextlake.kb.sources.files import FilesSource
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
        main(["ingest", "--path", str(docs_dir), "--config", str(cfg)])
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
    assert "2 document(s)" in capsys.readouterr().out
