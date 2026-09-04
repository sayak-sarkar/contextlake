"""The `files` source reads text OUT OF IMAGES with local OCR, and says so when it can't.

Why OCR and not a vision model. Reading an image is the one ingest path that could have
left local-first: the approach on offer elsewhere sends each raster to a vision-capable
LLM. `rapidocr-onnxruntime` ships its models inside the wheel, so a first run downloads
nothing and no image leaves the machine, and image ingestion stays inside the offline
boundary INV-2 draws.

**The engine is NOT a dev dependency, deliberately.** It pulls onnxruntime and opencv for
roughly 390 MB, which is a poor trade for every CI job on every push. pypdf rides in `dev`
because it is small; this does not. The cost of that choice is that a test needing the real
engine would skip in CI, and a skipped test is green without having exercised anything --
so the tests here are split. Everything about the SOURCE's behaviour (routing by suffix,
the honest degrade, warn-once, `max_bytes`, the attrs) runs everywhere against a stub
engine. Exactly one test uses the real engine, and it is the only one that skips.
"""

import logging

import pytest

from contextlake.kb.sources import files as files_mod
from contextlake.kb.sources.files import FilesSource

try:
    import rapidocr_onnxruntime  # noqa: F401

    HAS_OCR = True
except ImportError:
    HAS_OCR = False

requires_ocr = pytest.mark.skipif(not HAS_OCR, reason="the kb-ocr extra is not installed")


class _StubEngine:
    """Shaped like the real engine's call: ``(results, elapsed)``, each result
    ``[box, text, confidence]``. Returning ``None`` for "nothing found" is the real
    library's behaviour and is what the source's ``or []`` exists for."""

    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        if self.lines is None:
            return None, 0.0
        return [[[[0, 0]], t, 0.99] for t in self.lines], 0.0


@pytest.fixture(autouse=True)
def _no_cached_engine(monkeypatch):
    """The engine is cached in a module global. Reset it around every test, or the first
    test to touch it decides what every later one sees."""
    monkeypatch.setattr(files_mod, "_OCR_ENGINE", None)


def _png(path, name="shot.png"):
    """A real 1x1 PNG on disk. Its PIXELS never matter -- every test but the live one
    stubs the engine -- but the source stats and routes a real file, so it must exist
    and must actually be a PNG."""
    import base64

    p = path / name
    p.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6"
        "kgAAAABJRU5ErkJggg=="))
    return p


def _stub(monkeypatch, lines):
    eng = _StubEngine(lines)
    monkeypatch.setattr(files_mod, "_ocr_engine", lambda: eng)
    return eng


def test_an_image_with_text_is_ingested(tmp_path, monkeypatch):
    _png(tmp_path)
    _stub(monkeypatch, ["ForecastService samples the grid", "TELEMETRY_MAX_BATCH = 512"])
    docs = list(FilesSource(path=str(tmp_path)).iter_documents())
    assert len(docs) == 1
    assert "ForecastService samples the grid" in docs[0].text
    assert "TELEMETRY_MAX_BATCH = 512" in docs[0].text


def test_an_ocr_document_is_stamped_as_ocr(tmp_path, monkeypatch):
    """A consumer must be able to tell OCR'd text from a text file without inferring it
    from the extension: OCR misreads, and the evidence is weaker."""
    _png(tmp_path)
    _stub(monkeypatch, ["some words"])
    doc = next(iter(FilesSource(path=str(tmp_path)).iter_documents()))
    assert doc.attrs["ocr"] is True
    assert doc.attrs["lines"] == 1


def test_an_image_with_no_text_is_reported_not_ingested(tmp_path, gls_logs, monkeypatch):
    """The signature failure mode this project exists to avoid: a node that looks like
    knowledge and holds none. A logo must not become an empty document."""
    _png(tmp_path, "logo.png")
    _stub(monkeypatch, None)
    assert list(FilesSource(path=str(tmp_path)).iter_documents()) == []
    assert any("read no text" in r.message for r in gls_logs.records)


def test_a_missing_extra_is_guidance_not_a_traceback(tmp_path, gls_logs, monkeypatch):
    _png(tmp_path)
    monkeypatch.setattr(files_mod, "_ocr_engine", lambda: None)
    assert list(FilesSource(path=str(tmp_path)).iter_documents()) == []
    msg = " ".join(r.message for r in gls_logs.records)
    assert "kb-ocr" in msg and "pip install" in msg
    # The install line must not imply a download or a network call, because there is none.
    assert "nothing is downloaded" in msg


def test_many_missing_images_warn_once_for_the_run(tmp_path, gls_logs, monkeypatch):
    for i in range(5):
        _png(tmp_path, f"s{i}.png")
    monkeypatch.setattr(files_mod, "_ocr_engine", lambda: None)
    assert list(FilesSource(path=str(tmp_path)).iter_documents()) == []
    # A SET of messages, not a count of records: whether a record reaches this fixture
    # once or twice is a logging-handler detail, and the PDF twin of this test records
    # that asserting on the count made it pass in a file run and fail run on its own.
    said = {r.getMessage() for r in gls_logs.records
            if r.levelno >= logging.WARNING and "kb-ocr" in r.getMessage()}
    assert len(said) == 1, said
    only = said.pop()
    assert "skipped 5 image(s)" in only          # the tally, not one file's name
    assert "and 2 more" in only                  # names a few, counts the rest


def test_an_undecodable_image_is_reported_not_fatal(tmp_path, gls_logs, monkeypatch):
    _png(tmp_path, "broken.png")

    def _boom(path):
        raise ValueError("not an image the engine can decode")

    monkeypatch.setattr(files_mod, "_ocr_engine", lambda: _boom)
    monkeypatch.setattr(files_mod, "_ocr_lines",
                        lambda engine, path: engine(path))
    assert list(FilesSource(path=str(tmp_path)).iter_documents()) == []
    assert any("could not read it" in r.message for r in gls_logs.records)


def test_an_image_over_max_bytes_is_skipped_out_loud(tmp_path, gls_logs, monkeypatch):
    p = _png(tmp_path)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 4096)
    _stub(monkeypatch, ["never reached"])
    assert list(FilesSource(path=str(tmp_path), max_bytes=100).iter_documents()) == []
    assert any("over max_bytes" in r.message for r in gls_logs.records)


def test_the_engine_is_built_once_not_once_per_image(monkeypatch):
    """Construction loads the models, so per-image construction would make a directory of
    screenshots pathologically slow. Asserted against `_ocr_engine` itself rather than
    through the source, because the cache lives in it -- patching it out, which every
    other test here does, would leave this test measuring the stub."""
    import sys
    import types

    built = {"n": 0}

    class _Counting:
        def __init__(self):
            built["n"] += 1

        def __call__(self, path):
            return None, 0.0

    fake = types.ModuleType("rapidocr_onnxruntime")
    fake.RapidOCR = _Counting
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake)
    monkeypatch.setattr(files_mod, "_OCR_ENGINE", None)

    first = files_mod._ocr_engine()
    for _ in range(5):
        files_mod._ocr_engine()
    assert first is not None
    assert built["n"] == 1, f"engine constructed {built['n']} times, expected once"


def test_a_missing_engine_is_cached_too(monkeypatch):
    """The absent case is cached as well, or a tree of a thousand images retries the
    failed import a thousand times."""
    import builtins
    import sys

    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)
    monkeypatch.setattr(files_mod, "_OCR_ENGINE", None)
    tried = {"n": 0}
    real_import = builtins.__import__

    def counting(name, *a, **k):
        if name == "rapidocr_onnxruntime":
            tried["n"] += 1
            raise ImportError("absent")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", counting)
    assert files_mod._ocr_engine() is None
    for _ in range(5):
        assert files_mod._ocr_engine() is None
    assert tried["n"] == 1, f"import retried {tried['n']} times, expected once"


_TEXT_GLOBS = {"*.md", "*.markdown", "*.mdx", "*.rst", "*.txt"}


def test_every_default_glob_is_routed_somewhere():
    """A glob with no route is worse than no glob: the file is found, read as UTF-8,
    raises UnicodeDecodeError, and is swallowed -- so a binary type silently contributes
    nothing while appearing to be supported.

    Written against ALL the routes rather than the image one. The first version excluded a
    hardcoded list of text suffixes and asserted everything else was an image, which broke
    the moment video globs were added -- correctly, but for a reason that had nothing to
    do with images.
    """
    from contextlake.kb.sources.files import _DEFAULT_GLOBS, _IMAGE_EXTS, _VIDEO_EXTS

    assert "*.png" in _DEFAULT_GLOBS and "*.jpg" in _DEFAULT_GLOBS
    routed = _TEXT_GLOBS | {"*.pdf"} | {f"*{e}" for e in _IMAGE_EXTS | _VIDEO_EXTS}
    unrouted = [g for g in _DEFAULT_GLOBS if g not in routed]
    assert not unrouted, f"globbed but routed nowhere, so read as text and dropped: {unrouted}"


def test_an_image_is_not_read_as_text(tmp_path, monkeypatch):
    """The routing branch, asserted directly: without it an image reaches `read_text`,
    raises UnicodeDecodeError, and is swallowed -- indistinguishable from 'no images'."""
    _png(tmp_path)
    eng = _stub(monkeypatch, ["routed to ocr"])
    docs = list(FilesSource(path=str(tmp_path)).iter_documents())
    assert eng.calls == 1, "the image never reached the OCR branch"
    assert docs[0].text == "routed to ocr"


@requires_ocr
def test_the_real_engine_reads_real_text_offline(tmp_path, monkeypatch):
    """The one test that uses the real engine, and the only one that skips.

    Blocks the network first: the claim is not merely that OCR works, but that it works
    with no network at all, which is what keeps this path inside INV-2.
    """
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (620, 90), "white")
    ImageDraw.Draw(img).text((20, 35), "TELEMETRY_MAX_BATCH = 512", fill="black")
    img.save(tmp_path / "real.png")

    import socket

    def _blocked(*a, **k):
        raise OSError("network blocked: OCR must not need it")

    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    docs = list(FilesSource(path=str(tmp_path)).iter_documents())
    assert len(docs) == 1
    assert "TELEMETRY_MAX_BATCH" in docs[0].text.replace(" ", "")
    assert docs[0].attrs["ocr"] is True
