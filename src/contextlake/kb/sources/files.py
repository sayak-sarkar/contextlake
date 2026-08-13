"""Built-in source: local text / markdown files, plus the text layer of local PDFs.

Text and markdown need nothing but the standard library. PDFs need one optional
extra (``kb-pdf`` -> ``pypdf``), imported lazily, so the core stays as it was for
everyone who never ingests a PDF.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ...logging_setup import log
from .base import Document

_DEFAULT_GLOBS = ("*.md", "*.markdown", "*.mdx", "*.rst", "*.txt", "*.pdf")
_MAX_BYTES = 1_000_000
# The extra that carries pypdf. Named alongside `kb-vec` / `kb-local` /
# `kb-fastembed`: an optional add-on to the knowledge layer, not to the mirror.
_PDF_EXTRA = "kb-pdf"


class FilesSource:
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

    def iter_documents(self):
        root = Path(self.path)
        if root.is_file():
            files, base = [root], root.parent
        else:
            base = root
            files = sorted({p for g in self.include for p in root.rglob(g)})
        needs_extra: list[str] = []   # PDFs skipped because pypdf is not installed
        for p in files:
            if not p.is_file():
                continue
            rel = os.path.relpath(p, base)
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
