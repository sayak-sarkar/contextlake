"""The `files` source reads a PDF's TEXT LAYER, and says so when there isn't one.

No fixture binary is checked in: every PDF here is written by `_write_pdf` below, a
few hundred bytes of hand-rolled PDF syntax, so the tests carry their own inputs and
nothing opaque enters the repository.

The behaviour under test is the *honest degrade*, not just the happy path. A scanned
PDF has no text layer, and ingesting it as an empty document would put a node in the
graph that looks like knowledge and holds none -- this project's signature failure
mode. `test_a_pdf_with_no_text_layer_is_reported_not_ingested` pins the message; the
near-miss recorded in AGENT-REPORT-PDF-INGEST.md shows it failing when the warning is
removed, so it is pinning behaviour rather than agreeing with the code.
"""

import importlib.util
import logging
import sys
import zlib

import pytest

from contextlake.kb.sources.files import FilesSource

# Gated per test, not for the module: `_write_pdf` needs only zlib, so the tests that
# assert on the behaviour when pypdf is *absent* run everywhere -- they are the ones a
# machine without the extra can still prove. Only the tests that actually read a PDF
# need it, and `dev` carries pypdf so CI runs those rather than skipping them green.
needs_pypdf = pytest.mark.skipif(
    importlib.util.find_spec("pypdf") is None,
    reason="reading a PDF's text layer needs the kb-pdf extra (pypdf)")


def _write_pdf(path, pages, title=None):
    """Write a minimal PDF whose pages hold `pages` text. `""` means *no* text layer.

    Hand-built rather than produced by a writer library because pypdf has no
    text-drawing API: a page it creates is blank, which is the very case the
    no-text-layer test needs to be distinguishable from a page with words on it.
    """
    n = len(pages)
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (f"<< /Title ({title}) >>".encode() if title else b"<< >>"),
    ]
    for i, text in enumerate(pages):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {6 + 2 * i} 0 R >>".encode()
        )
        if text:
            esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            # Deflated, like every real PDF: a page's text is routinely several times
            # the bytes it occupies on disk, which is why the text bound and the file
            # size gate are not the same measurement even though they share a knob.
            stream = zlib.compress(f"BT /F1 12 Tf 20 100 Td ({esc}) Tj ET".encode())
            head = b"<< /Length %d /Filter /FlateDecode >>" % len(stream)
        else:
            stream, head = b"", b"<< /Length 0 >>"
        objs.append(head + b"\nstream\n" + stream + b"\nendstream")

    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 4 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref_at)
    path.write_bytes(bytes(out))


@needs_pypdf
def test_a_pdf_with_a_text_layer_is_ingested_with_page_provenance(tmp_path):
    _write_pdf(tmp_path / "design.pdf",
               ["retry budgets are per tenant", "the scheduler drains in order"],
               title="Retry design")

    docs = list(FilesSource(path=str(tmp_path)).iter_documents())

    assert len(docs) == 1
    doc = docs[0]
    assert doc.id == "design.pdf"                      # id stays the path, as for text
    assert doc.title == "Retry design"                 # the PDF states its own title
    assert "retry budgets are per tenant" in doc.text
    assert "the scheduler drains in order" in doc.text
    assert doc.uri.endswith("design.pdf") and "#" not in doc.uri  # a real, citable path
    # A page number is what a line number is to source code, so it travels with the
    # document: page N of the PDF starts at page_offsets[N - 1] in doc.text.
    assert doc.attrs["pages"] == 2 and doc.attrs["pages_read"] == 2
    offsets = doc.attrs["page_offsets"]
    assert len(offsets) == 2 and offsets[0] == 0
    assert doc.text[offsets[1]:].startswith("the scheduler drains in order")
    assert "truncated" not in doc.attrs


@needs_pypdf
def test_an_ingested_pdf_is_searchable(tmp_path, monkeypatch):
    """End to end: `kb ingest` stores the PDF's text and full-text search finds it."""
    from contextlake.cli import main
    from contextlake.kb.store.sqlite_store import SqliteStore

    monkeypatch.setenv("HOME", str(tmp_path))   # isolate from any real ~/.contextlake
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_pdf(docs_dir / "adr.pdf", ["we chose quorum writes for the ledger"],
               title="ADR 7 quorum writes")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", str(docs_dir), "--config", str(cfg)])
    assert e.value.code == 0

    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    try:
        node = store.get_node("@ingest:cli:adr.pdf")
        assert node is not None and node.kind == "document"
        assert node.name == "ADR 7 quorum writes"
        # attrs survive into the node, page provenance included
        assert node.attrs["pages"] == 1 and node.attrs["page_offsets"] == [0]
        assert any(h.id == "@ingest:cli:adr.pdf" for h in store.search("quorum"))
    finally:
        store.close()


@needs_pypdf
def test_a_pdf_with_no_text_layer_is_reported_not_ingested(tmp_path, gls_logs):
    """A scanned PDF must degrade loudly. Ingesting it empty is the failure mode."""
    _write_pdf(tmp_path / "scan.pdf", ["", ""])      # pages, no text layer at all
    (tmp_path / "notes.md").write_text("# Notes\nreal text\n")

    docs = list(FilesSource(path=str(tmp_path)).iter_documents())

    # No empty document node: the PDF produced nothing at all...
    assert [d.id for d in docs] == ["notes.md"]      # the markdown still came through
    # ...and it said so, naming the file and why.
    warnings = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    assert any("scan.pdf" in m and "no extractable text" in m for m in warnings), warnings
    assert any("text layer" in m and "OCR" in m for m in warnings), warnings


def test_a_missing_extra_is_guidance_not_a_traceback(tmp_path, gls_logs, monkeypatch):
    """Without pypdf the run continues, the text files still land, and the message
    names the extra. It must not raise: `cmds/ingest.py` wraps `iter_documents()` in
    a broad except, so an ImportError would be reported as "source failed" and would
    lose every remaining file in the run."""
    _write_pdf(tmp_path / "rfc.pdf", ["some real text"])
    (tmp_path / "notes.md").write_text("# Notes\nreal text\n")
    # Break the real `from pypdf import PdfReader`, not our own helper.
    monkeypatch.setitem(sys.modules, "pypdf", None)

    docs = list(FilesSource(path=str(tmp_path)).iter_documents())

    assert [d.id for d in docs] == ["notes.md"]
    warnings = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    assert any("kb-pdf" in m and "pip install 'contextlake[kb-pdf]'" in m
               for m in warnings), warnings
    assert any("rfc.pdf" in m for m in warnings), warnings


def test_many_missing_pdfs_warn_once_for_the_run(tmp_path, gls_logs, monkeypatch):
    for i in range(5):
        _write_pdf(tmp_path / f"d{i}.pdf", ["text"])
    monkeypatch.setitem(sys.modules, "pypdf", None)

    assert list(FilesSource(path=str(tmp_path)).iter_documents()) == []

    said = {r.getMessage() for r in gls_logs.records if "kb-pdf" in r.getMessage()}
    # One line for the run, not one per file: a single message, and it is the message
    # that carries the whole tally. (A set, not a count of records: whether a record
    # reaches this fixture once or twice is a logging-handler detail, and asserting on
    # it made this test pass in a file run and fail run on its own.)
    assert len(said) == 1, said
    only = said.pop()
    assert "skipped 5 PDF(s)" in only                   # the tally, not one file's name
    assert "and 2 more" in only                         # names a few, counts the rest


@needs_pypdf
def test_an_unreadable_pdf_is_reported_not_ingested(tmp_path, gls_logs):
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf at all\n")

    docs = list(FilesSource(path=str(tmp_path)).iter_documents())

    assert docs == []
    warnings = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    assert any("broken.pdf" in m and "could not read the PDF" in m for m in warnings)


@needs_pypdf
def test_max_bytes_bounds_the_pdf_and_the_truncation_is_declared(tmp_path, gls_logs):
    """The cap is the source's existing `max_bytes`, applied to the text as it is
    collected -- there is no second PDF-only knob. Reachable because a PDF's streams
    are compressed: this file is ~1 KB on disk and holds 9,000 characters."""
    big = tmp_path / "big.pdf"
    _write_pdf(big, ["a" * 3000, "b" * 3000, "c" * 3000])
    assert big.stat().st_size < 4000        # under the size gate...

    docs = list(FilesSource(path=str(tmp_path), max_bytes=4000).iter_documents())

    assert len(docs) == 1
    doc = docs[0]
    # ...but its text is over the bound, so reading stops at the first page boundary
    # past it: page 1 (3,000 chars) is under, page 2 crosses it, page 3 is never read.
    assert doc.attrs["truncated"] is True
    assert doc.attrs["pages"] == 3 and doc.attrs["pages_read"] == 2
    assert "c" * 3000 not in doc.text
    assert any("truncated" in r.getMessage() and "big.pdf" in r.getMessage()
               for r in gls_logs.records)


@needs_pypdf
def test_a_pdf_over_max_bytes_is_skipped_out_loud(tmp_path, gls_logs):
    """The existing size gate applies to PDFs too, but it says so: a PDF's on-disk
    size is mostly images, so a silent skip here would look like "no PDFs found"."""
    _write_pdf(tmp_path / "huge.pdf", ["x" * 5000])

    docs = list(FilesSource(path=str(tmp_path), max_bytes=200).iter_documents())

    assert docs == []
    assert any("huge.pdf" in r.getMessage() and "max_bytes" in r.getMessage()
               for r in gls_logs.records)


def test_pdf_is_in_the_default_globs():
    assert "*.pdf" in FilesSource(path=".").include
