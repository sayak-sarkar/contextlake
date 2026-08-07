"""Built-in source: local text / markdown files. Zero-config, no dependencies."""

from __future__ import annotations

import os
from pathlib import Path

from .base import Document

_DEFAULT_GLOBS = ("*.md", "*.markdown", "*.mdx", "*.rst", "*.txt")
_MAX_BYTES = 1_000_000


class FilesSource:
    """Yield a :class:`Document` per text file under ``path`` (a directory tree or a
    single file).

    Config keys (``[[sources]] type="files"`` or ``ingest --path``):
      - ``path``: directory or file (default ``"."``)
      - ``include``: list of globs (default common text/markdown extensions)
      - ``max_bytes``: skip files larger than this (default 1 MB)

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
        for p in files:
            if not p.is_file():
                continue
            try:
                if p.stat().st_size > self.max_bytes:
                    continue
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # skip binaries / unreadable / vanished files
            if not text.strip():
                continue
            rel = os.path.relpath(p, base)
            yield Document(id=rel, title=_title(text, rel), text=text,
                           uri=str(p.resolve()), attrs={"chars": len(text)})


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
