"""Built-in source: local text / markdown files, the text layer of local PDFs, text read
out of local images, and both halves of a local video.

Text and markdown need nothing but the standard library. PDFs need one optional extra
(``kb-pdf`` -> ``pypdf``) and images need another (``kb-ocr`` -> ``rapidocr-onnxruntime``),
both imported lazily, so the core stays as it was for everyone who ingests neither.

The OCR engine runs **locally**: its models ship inside the wheel, so a first run needs no
download and the offline boundary (INV-2) holds. That is the whole reason this reads images
with OCR rather than by sending them to a vision model, which would have put an ingest path
outside local-first for the first time.

A video is read in two layers, because they make different promises. ``kb-video`` decodes it
and OCRs sampled frames -- the slides, the terminal, the UI -- and is as offline as image
ingestion is. ``kb-transcribe`` adds the spoken track, and its speech model is fetched once on
first use the way ``kb-local``'s embedder is. Taking the first without the second is a
supported configuration and says so in the document it produces, because "no words were
spoken" and "nobody installed the transcriber" must never look the same.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ...logging_setup import log
from .base import Document, FetchFailures

_DEFAULT_GLOBS = ("*.md", "*.markdown", "*.mdx", "*.rst", "*.txt", "*.pdf",
                  "*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp",
                  "*.mp4", "*.mov", "*.mkv", "*.webm")
_MAX_BYTES = 1_000_000
# The extra that carries pypdf. Named alongside `kb-vec` / `kb-local` /
# `kb-fastembed`: an optional add-on to the knowledge layer, not to the mirror.
_PDF_EXTRA = "kb-pdf"
# Suffixes routed to OCR. Kept as a set beside the globs rather than derived from them:
# a user who narrows `include` to one image type must still reach the OCR branch, and a
# user who adds an image type the engine cannot decode should fail in the engine with a
# readable message rather than be silently read as UTF-8 text.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_OCR_EXTRA = "kb-ocr"
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
#: Decoding a video needs a demuxer. `av` bundles ffmpeg, so this stays pip-only and
#: adds no system binary -- the property that keeps `pip install contextlake[...]`
#: workable on a machine where installing ffmpeg is not an option.
_VIDEO_EXTRA = "kb-video"
#: Transcription is layered ON TOP of that rather than folded into it. Frame OCR is
#: fully offline (the OCR models ship in their wheel); a speech model is fetched once
#: on first use, the way `kb-local` and `llm-local` already do. Those are different
#: promises, so they are different extras and a user can take the first without the
#: second.
_TRANSCRIBE_EXTRA = "kb-transcribe"
#: One frame every N seconds, and never more than this many. A recorded review is
#: mostly a still slide, so sampling densely buys near-duplicate text at linear cost;
#: the cap bounds a long video to a predictable amount of OCR rather than an amount
#: proportional to its length.
_FRAME_EVERY_SECONDS = 5.0
_MAX_FRAMES = 60


class FilesSource(FetchFailures):
    """Yield a :class:`Document` per text file under ``path`` (a directory tree or a
    single file), and per PDF with a readable text layer.

    Config keys (``[[sources]] type="files"`` or ``ingest --path``):
      - ``path``: directory or file (default ``"."``)
      - ``include``: list of globs (default common text/markdown extensions + ``*.pdf``)
      - ``max_bytes``: skip files larger than this, and stop reading a PDF's text
        once that many characters have been collected (default 1 MB)

    Unknown keys are ignored so connector-style config can ride along.
    """

    def __init__(self, path: str = ".", include=None, max_bytes: int = _MAX_BYTES, **_):
        self.path = path
        self.include = tuple(include) if include else _DEFAULT_GLOBS
        self.max_bytes = int(max_bytes)

    def _record_missing(self, target: str, reason: str) -> None:
        """Record a whole-root failure. `_record_failure` takes an exception because the
        network sources catch one; here nothing is raised, so the reason is written
        directly rather than manufacturing an exception to unwrap."""
        self.failures.append((target, reason))
        log(f"files: could not read {target} -- {reason}", level=logging.WARNING)

    def iter_documents(self):
        self._reset_failures()
        root = Path(self.path)
        # A missing directory yields nothing from `rglob`, with no exception -- so a
        # typo in `--path` used to produce the identical output to a real directory
        # holding no matching files: "no documents (source reachable, nothing to
        # ingest)" and exit 0. "Reachable" was never checked; it was inferred from the
        # absence of a recorded failure, and this source recorded none because it had
        # no way to. The two cases need opposite responses (fix the path / accept the
        # empty result), so they must not print the same line.
        if not root.exists():
            self._record_missing(str(root), "no such file or directory")
            return
        if not (root.is_file() or root.is_dir()):
            self._record_missing(str(root), "not a file or a directory")
            return
        if root.is_file():
            files, base = [root], root.parent
        else:
            base = root
            try:
                files = sorted({p for g in self.include for p in root.rglob(g)})
            except OSError as e:
                # An unreadable directory is a permissions problem, not an empty tree.
                self._record_missing(str(root), f"{type(e).__name__}: {e}")
                return
        needs_extra: list[str] = []   # PDFs skipped because pypdf is not installed
        needs_ocr: list[str] = []     # images skipped because the OCR engine is not
        needs_video: list[str] = []       # videos skipped: no decoder
        needs_transcribe: list[str] = []  # videos read WITHOUT their audio
        for p in files:
            if not p.is_file():
                continue
            rel = os.path.relpath(p, base)
            if p.suffix.lower() in _VIDEO_EXTS:
                doc = self._video_document(p, rel, needs_video, needs_transcribe)
                if doc is not None:
                    yield doc
                continue
            if p.suffix.lower() in _IMAGE_EXTS:
                # Branched before the text path for the same reason the PDF suffix is:
                # an image decoded as UTF-8 raises UnicodeDecodeError, which that path
                # swallows, so a missed branch here is indistinguishable from "no
                # images found".
                doc = self._image_document(p, rel, needs_ocr)
                if doc is not None:
                    yield doc
                continue
            if p.suffix.lower() == ".pdf":
                # Branch on the suffix BEFORE any read_text(): a PDF decoded as
                # UTF-8 raises UnicodeDecodeError, which the text path swallows,
                # so a missed branch here would look exactly like "no PDFs found".
                doc = self._pdf_document(p, rel, needs_extra)
                if doc is not None:
                    yield doc
                continue
            try:
                if p.stat().st_size > self.max_bytes:
                    continue
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # skip binaries / unreadable / vanished files
            if not text.strip():
                continue
            yield Document(id=rel, title=_title(text, rel), text=text,
                           uri=str(p.resolve()), attrs={"chars": len(text)})
        if needs_extra:
            shown = ", ".join(needs_extra[:3])
            more = f" (and {len(needs_extra) - 3} more)" if len(needs_extra) > 3 else ""
            log(f"files: skipped {len(needs_extra)} PDF(s) -- reading a PDF's text "
                f"layer needs the {_PDF_EXTRA!r} extra (pypdf), which is not installed. "
                f"Install it with: pip install 'contextlake[{_PDF_EXTRA}]'. "
                f"Skipped: {shown}{more}", level=logging.WARNING)
        if needs_ocr:
            shown = ", ".join(needs_ocr[:3])
            more = f" (and {len(needs_ocr) - 3} more)" if len(needs_ocr) > 3 else ""
            log(f"files: skipped {len(needs_ocr)} image(s) -- reading text out of an "
                f"image needs the {_OCR_EXTRA!r} extra, which is not installed. It runs "
                f"locally and its models ship with it, so nothing is downloaded and "
                f"nothing is sent anywhere. Install it with: "
                f"pip install 'contextlake[{_OCR_EXTRA}]'. Skipped: {shown}{more}",
                level=logging.WARNING)
        if needs_video:
            shown = ", ".join(needs_video[:3])
            more = f" (and {len(needs_video) - 3} more)" if len(needs_video) > 3 else ""
            log(f"files: skipped {len(needs_video)} video(s) -- decoding one needs the "
                f"{_VIDEO_EXTRA!r} extra, which is not installed. It bundles its own "
                f"ffmpeg, so there is no system package to install first. Add it with: "
                f"pip install 'contextlake[{_VIDEO_EXTRA}]'. Skipped: {shown}{more}",
                level=logging.WARNING)
        if needs_transcribe:
            shown = ", ".join(needs_transcribe[:3])
            more = (f" (and {len(needs_transcribe) - 3} more)"
                    if len(needs_transcribe) > 3 else "")
            # Not a failure: these videos WERE ingested, from their frames. Said out loud
            # because a transcript-shaped absence is the kind a reader fills in wrongly --
            # "the meeting discussed nothing" rather than "nobody installed the speech
            # model". The document itself carries the same fact in `attrs`.
            log(f"files: read {len(needs_transcribe)} video(s) from their FRAMES ONLY -- "
                f"the spoken track needs the {_TRANSCRIBE_EXTRA!r} extra, which is not "
                f"installed. Add it with: pip install "
                f"'contextlake[{_TRANSCRIBE_EXTRA}]'. Silent so far: {shown}{more}",
                level=logging.WARNING)

    def _image_document(self, path: Path, rel: str, needs_ocr: list[str]):
        """One :class:`Document` for an image's OCR'd text, or ``None`` with a WARNING.

        Four ways an image yields nothing, and each says which one by name at WARNING:
        it is larger than ``max_bytes``, the extra is absent, the engine could not decode
        it, or it decoded fine and holds no text. The last is the common one -- a logo, an
        icon, a photograph -- and it is the reason this is a WARNING per file rather than
        an error: a repository full of decorative images is normal, not broken.

        The text is what the engine read, not what the image "says". OCR misreads, and a
        document ingested from one is evidence at lower confidence than a text file. It is
        stamped ``attrs["ocr"] = True`` so a consumer can tell the difference rather than
        having to guess from the file extension.
        """
        try:
            size = path.stat().st_size
        except OSError:
            return None  # vanished under us, same as the text path
        if size > self.max_bytes:
            log(f"files: skipping {rel} -- {size} bytes is over max_bytes "
                f"({self.max_bytes}); raise `max_bytes` on this source to ingest it. "
                f"Screenshots pass 1 MB routinely, so this is the limit an image is "
                f"most likely to hit.", level=logging.WARNING)
            return None
        engine = _ocr_engine()
        if engine is None:
            needs_ocr.append(rel)     # warned once, at the end of the run
            return None
        try:
            lines = _ocr_lines(engine, path)
        except Exception as e:  # noqa: BLE001 - one unreadable image costs only itself
            log(f"files: skipping {rel} -- the OCR engine could not read it "
                f"({type(e).__name__}: {e}).", level=logging.WARNING)
            return None
        text = "\n".join(lines).strip()
        if not text:
            log(f"files: skipping {rel} -- the OCR engine read no text in it. An image "
                f"with no words in it (a logo, an icon, a photograph) is expected to "
                f"land here.", level=logging.WARNING)
            return None
        return Document(id=rel, title=rel, text=text, uri=str(path.resolve()),
                        attrs={"chars": len(text), "lines": len(lines), "ocr": True})


    def _video_document(self, path: Path, rel: str, needs_video: list[str],
                        needs_transcribe: list[str]):
        """One :class:`Document` for a video: its spoken track, its on-screen text, or both.

        `max_bytes` is deliberately NOT applied to the file. It exists to bound how much
        text a document contributes, and for every other type the file size predicts that;
        for a video it predicts resolution and length instead, and a 1 MB cap would reject
        essentially every real recording while admitting nothing useful. What is bounded
        is the work: at most :data:`_MAX_FRAMES` frames, one every
        :data:`_FRAME_EVERY_SECONDS`, so cost is capped rather than proportional to length.
        """
        frames_read = 0
        transcript = ""
        lines: list[str] = []
        try:
            frames = _video_frames(path)
        except _NoDecoder:
            needs_video.append(rel)      # warned once, at the end of the run
            return None
        except Exception as e:  # noqa: BLE001 - one bad container costs only itself
            log(f"files: skipping {rel} -- could not decode the video "
                f"({type(e).__name__}: {e}).", level=logging.WARNING)
            return None

        engine = _ocr_engine()
        if engine is not None:
            seen: set[str] = set()
            for frame in frames:
                frames_read += 1
                try:
                    text = "\n".join(_ocr_lines(engine, frame))
                except Exception as e:  # noqa: BLE001 - one bad frame is not fatal
                    log(f"files: {rel} frame {frames_read}: OCR failed "
                        f"({type(e).__name__}); continuing", level=logging.DEBUG)
                    continue
                # A slide holds still for many samples. Keeping every hit would repeat the
                # same deck once per interval and drown the transcript in its own echo.
                for ln in text.splitlines():
                    key = ln.strip()
                    if key and key not in seen:
                        seen.add(key)
                        lines.append(key)
        else:
            # Frames were decoded but nothing can read them. Counted under the OCR extra's
            # own warning rather than silently dropped.
            for _ in frames:
                frames_read += 1

        transcriber = _transcriber()
        if transcriber is None:
            needs_transcribe.append(rel)
        elif not _has_audio(path):
            # A screen recording with no microphone is the common case, not a fault, and
            # calling it one would send a reader looking for a broken transcriber. Found
            # by running the real model over a real silent clip: the decoder raises
            # IndexError from inside faster-whisper when no audio stream exists, which the
            # catch below would have reported as "transcription failed".
            log(f"files: {rel} carries no audio track, so there is nothing to "
                f"transcribe; reading its frames only.", level=logging.INFO)
        else:
            try:
                transcript = _transcribe(transcriber, path).strip()
            except Exception as e:  # noqa: BLE001
                log(f"files: {rel} -- transcription failed ({type(e).__name__}: {e}); "
                    f"keeping the on-screen text.", level=logging.WARNING)

        parts = []
        if transcript:
            parts.append(transcript)
        if lines:
            parts.append("On screen:\n" + "\n".join(lines))
        text = "\n\n".join(parts).strip()
        if not text:
            log(f"files: skipping {rel} -- no speech was transcribed and no on-screen "
                f"text was read from {frames_read} sampled frame(s). A screen recording "
                f"of something wordless lands here.", level=logging.WARNING)
            return None
        return Document(
            id=rel, title=rel, text=text, uri=str(path.resolve()),
            attrs={"chars": len(text), "frames_sampled": frames_read,
                   "on_screen_lines": len(lines),
                   # Stated either way, never omitted. An absent field reads as "nothing to
                   # report"; `false` reads as "checked, and the transcriber was absent",
                   # which is the difference between a quiet video and an unread one.
                   "transcribed": bool(transcript),
                   "ocr": bool(lines)})


    def _pdf_document(self, path: Path, rel: str, needs_extra: list[str]):
        """One :class:`Document` for a PDF, or ``None`` with a WARNING saying why.

        Five outcomes, never a silent two. A document is returned only when text was
        actually extracted; the four refusals -- the ``kb-pdf`` extra is missing, the
        file is over ``max_bytes``, the PDF cannot be parsed, the PDF has no text
        layer -- each say which one by name, at WARNING. A PDF that yielded nothing
        must never be ingested as an empty
        document: an empty node is indistinguishable from a real one in search
        results and in the wiki, and that is the failure this project treats as
        worse than a loud skip.
        """
        try:
            size = path.stat().st_size
        except OSError:
            return None  # vanished under us, same as the text path
        if size > self.max_bytes:
            log(f"files: skipping {rel} -- {size} bytes is over max_bytes "
                f"({self.max_bytes}); raise `max_bytes` on this source to ingest it. "
                f"A PDF's on-disk size is mostly images, so a large PDF can still "
                f"hold little text.", level=logging.WARNING)
            return None
        reader_cls = _pdf_reader_cls()
        if reader_cls is None:
            needs_extra.append(rel)   # warned once, at the end of the run
            return None
        try:
            reader = reader_cls(str(path), strict=False)
            pages_total = len(reader.pages)
            title = _pdf_metadata_title(reader)
            text, offsets, unreadable, truncated = _extract_pdf_text(
                reader, self.max_bytes)
        except Exception as e:  # noqa: BLE001 - a damaged/encrypted PDF costs itself only
            log(f"files: skipping {rel} -- could not read the PDF ({type(e).__name__}: "
                f"{e}). Encrypted PDFs are not decrypted.", level=logging.WARNING)
            return None
        if not text.strip():
            log(f"files: skipping {rel} -- no extractable text ({pages_total} page(s) "
                f"read, all empty). contextlake reads a PDF's text layer only; a "
                f"scanned or image-only PDF has none and is not OCR'd.",
                level=logging.WARNING)
            return None
        attrs = {"chars": len(text), "pages": pages_total,
                 "pages_read": len(offsets), "page_offsets": offsets}
        if truncated:
            attrs["truncated"] = True
            log(f"files: {rel} truncated at {len(offsets)} of {pages_total} page(s) -- "
                f"its text passed max_bytes ({self.max_bytes}). The document is "
                f"ingested up to that point; raise `max_bytes` to take all of it.",
                level=logging.WARNING)
        if unreadable:
            attrs["pages_unreadable"] = unreadable
        return Document(id=rel, title=title or rel, text=text,
                        uri=str(path.resolve()), attrs=attrs)


class _NoDecoder(Exception):
    """The ``kb-video`` extra is absent. Its own type so the caller can tell "nobody
    installed a decoder" from "this file is not a video", which need different messages."""


def _video_frames(path: Path, *, every: float = _FRAME_EVERY_SECONDS,
                  max_frames: int = _MAX_FRAMES) -> list:
    """Frames sampled from ``path`` as BGR arrays, at most ``max_frames`` of them.

    Returns a list rather than a generator on purpose: the caller counts what it read even
    when no OCR engine is installed, and a generator that is never drained would report
    zero frames for a video that decoded perfectly well.

    Seeking is avoided. A keyframe-sparse recording seeks badly, and decoding forward
    while skipping is both simpler and correct on every container -- the cost is bounded
    by ``max_frames`` regardless.
    """
    try:
        import av
    except ImportError as e:
        raise _NoDecoder from e

    out = []
    with av.open(str(path)) as container:
        streams = [st for st in container.streams if st.type == "video"]
        if not streams:
            return out
        stream = streams[0]
        stream.thread_type = "AUTO"
        next_at = 0.0
        for frame in container.decode(stream):
            if frame.time is None or frame.time + 1e-9 < next_at:
                continue
            out.append(frame.to_ndarray(format="bgr24"))
            next_at = frame.time + every
            if len(out) >= max_frames:
                break
    return out


def _has_audio(path: Path) -> bool:
    """Whether ``path`` carries an audio stream at all.

    Asked before transcribing rather than discovered by catching the failure, because the
    two are different facts: a silent screen recording is ordinary, and a transcriber that
    threw is not. Returns ``True`` when the container cannot be inspected, so an
    unreadable-but-present audio track is attempted rather than silently written off.
    """
    try:
        import av
    except ImportError:
        # No decoder is "cannot inspect", not "no audio" -- the same fail-open the
        # docstring promises. Production never reaches here without `av`, because
        # decoding raises _NoDecoder first; returning False would only matter under a
        # stubbed decoder, and it would matter by contradicting this function's contract.
        return True
    try:
        with av.open(str(path)) as container:
            return any(st.type == "audio" for st in container.streams)
    except Exception:  # noqa: BLE001 - inspection failing is not proof of no audio
        return True


def _transcriber():
    """The speech model, or ``None`` when the ``kb-transcribe`` extra is not installed.

    Cached like :func:`_ocr_engine`, and for the same reason: loading it dominates the cost
    of a short video. ``CONTEXTLAKE_WHISPER_MODEL`` picks the size; the default is the
    smallest useful one, because a first run downloads it and a 1.5 GB surprise is a poor
    default even when the larger model is better.
    """
    global _TRANSCRIBER
    if _TRANSCRIBER is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            _TRANSCRIBER = False
        else:
            name = os.environ.get("CONTEXTLAKE_WHISPER_MODEL", "tiny")
            _TRANSCRIBER = WhisperModel(name, device="cpu", compute_type="int8")
    return _TRANSCRIBER or None


#: Cache for :func:`_transcriber`. ``None`` = not tried, ``False`` = extra absent.
_TRANSCRIBER = None


def _transcribe(model, path: Path) -> str:
    """The spoken text of ``path``, one segment per line.

    ``transcribe`` returns a generator plus an info object; the generator is where the work
    actually happens, so it is drained here rather than handed back to a caller who might
    reasonably assume the call already did it.
    """
    segments, _info = model.transcribe(str(path))
    return "\n".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())


def _ocr_engine():
    """The OCR engine, or ``None`` when the ``kb-ocr`` extra is not installed.

    Returns rather than raises, for the same reason :func:`_pdf_reader_cls` does: a
    missing optional extra is a configuration fact the caller reports once with an
    install line, not an exception that ends the run.

    Built once and cached. Construction loads the models and costs far more than a
    single image does, so building per file would make a directory of screenshots
    pathologically slow. ``False`` is cached for the absent case so a large tree does
    not retry the import once per image.
    """
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            _OCR_ENGINE = False
        else:
            _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE or None


#: Cache for :func:`_ocr_engine`. ``None`` = not tried, ``False`` = extra absent.
_OCR_ENGINE = None


def _ocr_lines(engine, source) -> list[str]:
    """The text lines the engine read, in the order it returned them.

    ``source`` is a path for an image file and a decoded frame array for a video. Only a
    path is stringified: ``str()`` on a frame produces a truncated repr of the pixel data,
    which the engine would take as a filename, fail to open, and report as an unreadable
    image -- a wrong answer that looks like a legitimate one.

    The engine returns ``(results, elapsed)`` where each result is
    ``[box, text, confidence]``, and ``results`` is ``None`` rather than ``[]`` for an
    image it found nothing in -- so the ``or []`` is load-bearing, not defensive.
    """
    results, _ = engine(str(source) if isinstance(source, (str, Path)) else source)
    return [r[1] for r in (results or []) if len(r) > 1 and str(r[1]).strip()]


def _pdf_reader_cls():
    """``pypdf.PdfReader``, or ``None`` when the ``kb-pdf`` extra is not installed.

    Returns rather than raises, for the same reason :func:`base.url_is_fetchable`
    does: ``cmds/ingest.py`` wraps ``iter_documents()`` in a broad
    ``except Exception … continue``, so an ImportError escaping here would be
    reported as "source failed" and would abandon every remaining file in the run,
    including the plain-text ones that never needed pypdf. The caller reports the
    miss itself, once per run.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    return PdfReader


def _pdf_metadata_title(reader) -> str:
    """The PDF's own ``/Title``, or ``""``.

    The same rule ``_title`` follows for markdown: the document usually states its
    subject, and the path is the fallback, not the answer. A PDF states it in its
    metadata rather than in a heading.
    """
    try:
        meta = reader.metadata
        title = (getattr(meta, "title", None) or "") if meta else ""
    except Exception:  # noqa: BLE001 - malformed metadata must not lose the document
        return ""
    return str(title).strip()


def _extract_pdf_text(reader, max_bytes: int):
    """Extract the text layer page by page, bounded by ``max_bytes``.

    Returns ``(text, page_offsets, unreadable_pages, truncated)``. ``page_offsets[i]``
    is the character offset in ``text`` where page ``i + 1`` starts -- a PDF's page
    number is what a line number is to source, so it travels with the document
    instead of being flattened away.

    Bounded by the source's existing ``max_bytes``, not by a second knob: reading
    stops at the first page boundary past that many characters, and ``truncated``
    says so. Pages are pulled one at a time, so an enormous PDF costs the pages that
    fit and not the whole file.
    """
    parts: list[str] = []
    offsets: list[int] = []
    total = unreadable = 0
    truncated = False
    for page in reader.pages:
        if total >= max_bytes:
            truncated = True
            break
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one damaged page must not lose the document
            text, unreadable = "", unreadable + 1
        offsets.append(total)      # recorded for empty pages too: page numbers must
        parts.append(text)         # stay aligned with the PDF's own numbering
        total += len(text) + 1     # + the "\n" the join puts between pages
    return "\n".join(parts), offsets, unreadable, truncated


def _title(text: str, rel: str) -> str:
    """The document's own title, falling back to its path.

    The path was used for both the id and the title, so a page headed
    ``# Payments runbook`` was stored, listed and cited as ``runbook.md``. The id
    must stay the path (it is the stable identity a re-ingest matches on), but the
    title is what a reader sees, and the document already states it.

    Only a level-one ATX heading counts, and only in the first few lines. A deeper
    heading is a section rather than the document's subject, and a ``#`` found far
    down the file is more likely a Python comment or a fragment link than a title.
    Setext headings (underlined with ``===``) are not read: they are rare in the
    formats this source collects, and guessing at one risks promoting an ordinary
    line to a title. A file with no heading keeps its path, which is what every
    non-markdown document this source yields will do.
    """
    lines = text.lstrip().splitlines()
    # YAML front matter first: it opens and closes with `---`, and its body is
    # arbitrary keys that would otherwise read as the content before a heading and
    # stop the scan. Only a `---` on the very first line opens it, so a horizontal
    # rule further down is not mistaken for it.
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                lines = lines[i + 1:]
                break
        else:
            return rel  # unterminated front matter: do not guess past it
    for line in lines[:5]:
        line = line.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            if title:
                return title
            break  # `#` with nothing after it is not a title
        if line and not line.startswith("<!--"):
            break  # real content before any heading: there is no document title
    return rel
