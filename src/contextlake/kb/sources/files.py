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
            yield Document(id=rel, title=rel, text=text, uri=str(p.resolve()),
                           attrs={"chars": len(text)})
