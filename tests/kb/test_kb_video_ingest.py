"""The `files` source reads a video's frames and its speech, and says which it got.

Two extras, two promises, and the tests are split the same way the feature is.
`kb-video` only decodes -- `av` bundles ffmpeg, so there is no system package -- and the
frames go through the OCR engine, which downloads nothing. `kb-transcribe` adds the spoken
track and fetches a speech model on first use.

Neither extra is a dev dependency (together they are ~550 MB), so every behavioural test
runs against stubs and the two live tests skip. A skipped test proves nothing, so the live
ones were run in a venv that has the extras; what they cover is decoding and routing, NOT
transcription accuracy -- there is no offline way to synthesise speech with known words,
and a test asserting a real model's output would be pinning the model, not this code.
"""

import logging
import pathlib

import pytest

from contextlake.kb.sources import files as files_mod
from contextlake.kb.sources.files import FilesSource

try:
    import av  # noqa: F401

    HAS_VIDEO = True
except ImportError:
    HAS_VIDEO = False

requires_video = pytest.mark.skipif(not HAS_VIDEO, reason="the kb-video extra is not installed")


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    monkeypatch.setattr(files_mod, "_OCR_ENGINE", None)
    monkeypatch.setattr(files_mod, "_TRANSCRIBER", None)


def _mp4(path, name="demo.mp4"):
    """A file with a video suffix. Its BYTES never matter -- the decoder is stubbed --
    but the source stats and routes a real file, so it must exist."""
    p = path / name
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    return p


class _Eng:
    def __init__(self, per_frame):
        self.per_frame = per_frame
        self.calls = 0

    def __call__(self, source):
        out = self.per_frame[min(self.calls, len(self.per_frame) - 1)]
        self.calls += 1
        if out is None:
            return None, 0.0
        return [[[[0, 0]], t, 0.99] for t in out], 0.0


def _wire(monkeypatch, *, frames=3, ocr=None, transcript=None, decoder=True):
    if decoder:
        monkeypatch.setattr(files_mod, "_video_frames",
                            lambda p, **k: [object()] * frames)
    else:
        def _boom(p, **k):
            raise files_mod._NoDecoder
        monkeypatch.setattr(files_mod, "_video_frames", _boom)
    monkeypatch.setattr(files_mod, "_ocr_engine",
                        (lambda: _Eng(ocr)) if ocr is not None else (lambda: None))
    if transcript is None:
        monkeypatch.setattr(files_mod, "_transcriber", lambda: None)
    else:
        monkeypatch.setattr(files_mod, "_transcriber", lambda: object())
        monkeypatch.setattr(files_mod, "_transcribe", lambda m, p: transcript)


def test_a_video_yields_its_speech_and_its_on_screen_text(tmp_path, monkeypatch):
    _mp4(tmp_path)
    _wire(monkeypatch, ocr=[["Deploy pipeline"]], transcript="we agreed to shard by tenant")
    doc = next(iter(FilesSource(path=str(tmp_path)).iter_documents()))
    assert "we agreed to shard by tenant" in doc.text
    assert "On screen:" in doc.text and "Deploy pipeline" in doc.text
    assert doc.attrs["transcribed"] is True and doc.attrs["ocr"] is True


def test_repeated_slide_text_is_said_once(tmp_path, monkeypatch):
    """A slide holds still across many samples. Keeping every hit would repeat the deck
    once per interval and drown the transcript in its own echo."""
    _mp4(tmp_path)
    _wire(monkeypatch, frames=5, ocr=[["Agenda", "Rollout plan"]], transcript="hello")
    doc = next(iter(FilesSource(path=str(tmp_path)).iter_documents()))
    assert doc.text.count("Rollout plan") == 1
    assert doc.attrs["on_screen_lines"] == 2
    assert doc.attrs["frames_sampled"] == 5


def test_without_the_transcriber_the_video_is_still_read_and_says_so(
        tmp_path, gls_logs, monkeypatch):
    """The half that matters most. "The meeting discussed nothing" and "nobody installed
    the speech model" must never look the same, in the log OR in the document."""
    _mp4(tmp_path)
    _wire(monkeypatch, ocr=[["Q3 roadmap"]], transcript=None)
    # `list`, not `next(iter(...))`: the per-run summary is emitted after the loop, so a
    # generator abandoned at its first document never reaches it. That is correct
    # behaviour -- a tally cannot be reported before the run ends -- and it means a test
    # that stops early is asserting on a warning the code was never given the chance to
    # write.
    docs = list(FilesSource(path=str(tmp_path)).iter_documents())
    doc = docs[0]
    assert "Q3 roadmap" in doc.text
    assert doc.attrs["transcribed"] is False, (
        "an absent field reads as 'nothing to report'; false reads as 'checked, absent'")
    said = {r.getMessage() for r in gls_logs.records if "kb-transcribe" in r.getMessage()}
    assert len(said) == 1, said
    assert "FRAMES ONLY" in said.pop()


def test_without_the_decoder_videos_are_skipped_once_for_the_run(
        tmp_path, gls_logs, monkeypatch):
    for i in range(4):
        _mp4(tmp_path, f"v{i}.mp4")
    _wire(monkeypatch, decoder=False)
    assert list(FilesSource(path=str(tmp_path)).iter_documents()) == []
    said = {r.getMessage() for r in gls_logs.records
            if r.levelno >= logging.WARNING and "kb-video" in r.getMessage()}
    assert len(said) == 1, said
    only = said.pop()
    assert "skipped 4 video(s)" in only
    assert "bundles its own ffmpeg" in only, "must not imply a system package is needed"


def test_a_wordless_video_is_reported_not_ingested(tmp_path, gls_logs, monkeypatch):
    _mp4(tmp_path)
    _wire(monkeypatch, ocr=[None], transcript="")
    assert list(FilesSource(path=str(tmp_path)).iter_documents()) == []
    assert any("no speech was transcribed" in r.getMessage() for r in gls_logs.records)


def test_a_video_is_not_read_as_text(tmp_path, monkeypatch):
    """Routing, asserted directly: without the branch the file reaches `read_text`, raises
    UnicodeDecodeError, and is swallowed -- indistinguishable from 'no videos found'."""
    _mp4(tmp_path)
    _wire(monkeypatch, ocr=[["routed"]], transcript="spoken")
    docs = list(FilesSource(path=str(tmp_path)).iter_documents())
    assert len(docs) == 1 and "spoken" in docs[0].text


def test_max_bytes_does_not_reject_videos(tmp_path, monkeypatch):
    """`max_bytes` bounds how much TEXT a document contributes, and for every other type
    file size predicts that. For a video it predicts resolution and length instead, so a
    1 MB cap would reject every real recording while admitting nothing useful. What is
    bounded is the work: frames sampled, not bytes on disk."""
    _mp4(tmp_path)
    _wire(monkeypatch, ocr=[["still read"]], transcript="")
    docs = list(FilesSource(path=str(tmp_path), max_bytes=8).iter_documents())
    assert len(docs) == 1


def test_video_globs_and_routing_suffixes_agree():
    from contextlake.kb.sources.files import _DEFAULT_GLOBS, _VIDEO_EXTS

    globbed = {g.lstrip("*") for g in _DEFAULT_GLOBS}
    assert _VIDEO_EXTS <= globbed, "a suffix routed to video but never globbed is dead"
    for ext in (".mp4", ".mov", ".mkv", ".webm"):
        assert ext in _VIDEO_EXTS and f"*{ext}" in _DEFAULT_GLOBS


def test_ocr_lines_passes_a_frame_through_unstringified():
    """`str()` on a frame array yields a truncated repr of pixel data. The engine would
    take it as a filename, fail to open it, and report an unreadable image -- a wrong
    answer shaped like a legitimate one."""
    seen = {}

    def engine(source):
        seen["type"] = type(source).__name__
        seen["value"] = source
        return None, 0.0

    frame = ["not", "a", "path"]
    files_mod._ocr_lines(engine, frame)
    assert seen["value"] is frame, "the frame was converted before reaching the engine"


def test_a_video_with_no_audio_is_not_called_a_transcription_failure(
        tmp_path, gls_logs, monkeypatch):
    """A screen recording with no microphone is ordinary. Reporting it as a failed
    transcription sends a reader hunting a broken model.

    Found by running the real transcriber over a real silent clip: `av` raises IndexError
    from inside faster-whisper when there is no audio stream, and the generic catch below
    it reported that as "transcription failed".
    """
    _mp4(tmp_path)
    _wire(monkeypatch, ocr=[["slide text"]], transcript="never reached")
    monkeypatch.setattr(files_mod, "_has_audio", lambda p: False)
    docs = list(FilesSource(path=str(tmp_path)).iter_documents())
    assert docs[0].attrs["transcribed"] is False
    said = " ".join(r.getMessage() for r in gls_logs.records)
    assert "no audio track" in said
    assert "transcription failed" not in said
    assert "kb-transcribe" not in said, (
        "the transcriber was installed; suggesting the extra would be wrong advice")


def test_has_audio_fails_open_when_the_container_cannot_be_inspected(monkeypatch):
    """An unreadable container is not proof of no audio. Failing closed would silently
    write off a video whose audio was merely awkward to probe."""
    import builtins

    real_import = builtins.__import__

    class _Boom:
        def open(self, *a, **k):
            raise OSError("cannot inspect")

    def fake_import(name, *a, **k):
        if name == "av":
            return _Boom()
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert files_mod._has_audio(pathlib.Path("/nonexistent.mp4")) is True


@requires_video
def test_a_real_video_decodes_and_samples_frames(tmp_path):
    """Live decode. Builds a real MP4 with `av`, then asserts sampling actually reads
    frames -- the decoding half, which is what `kb-video` alone promises."""
    import av
    import numpy as np

    p = tmp_path / "real.mp4"
    with av.open(str(p), mode="w") as out:
        stream = out.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 320, 240, "yuv420p"
        for i in range(60):                      # 6 seconds at 10 fps
            arr = np.full((240, 320, 3), i * 4 % 256, dtype=np.uint8)
            out.mux(stream.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")))
        out.mux(stream.encode(None))

    frames = files_mod._video_frames(p, every=1.0, max_frames=10)
    assert 2 <= len(frames) <= 10, f"sampled {len(frames)} frames from a 6s clip"
    assert frames[0].shape == (240, 320, 3)


@requires_video
def test_the_frame_cap_bounds_the_work(tmp_path):
    """Cost must be capped, not proportional to length."""
    import av
    import numpy as np

    p = tmp_path / "long.mp4"
    with av.open(str(p), mode="w") as out:
        stream = out.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 160, 120, "yuv420p"
        for _ in range(200):
            arr = np.zeros((120, 160, 3), dtype=np.uint8)
            out.mux(stream.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")))
        out.mux(stream.encode(None))

    assert len(files_mod._video_frames(p, every=0.1, max_frames=5)) == 5
