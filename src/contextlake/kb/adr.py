"""Architecture decision record (ADR) surfacing.

Scans a repo's own checkout for decision records under common conventions
(``docs/adr/``, ``docs/decisions/``, ``decisions/``, ``adr/`` -- the
https://adr.github.io/ layout and its common variants) and turns each into a
first-class ``adr`` node in the repo's own shard: an authored decision, not an
LLM narration of one, matching the wiki's own "ground everything, never
speculate" rule.

Unlike :mod:`.connectors.enrich` (external, connector-sourced content in a
separate ``@enrich:<repo>`` partition), an ADR lives in the repo's own git
history -- it is genuinely part of the codebase, so it becomes a regular node
in the same shard as everything else :func:`.parse.index_repo_dir` extracts,
not a side-channel.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .ids import make_id
from .model import Node

_ADR_DIRS = {"adr", "adrs", "decisions"}
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
# Body stored on the node's `doc` attr, the same key every other extractor uses
# for docstrings -- node_text() (kb/embeddings/index.py) already reads it, so
# an ADR becomes semantically searchable with no extra embedding-pipeline wiring.
_MAX_DOC_CHARS = 2000


def is_adr_path(rel_path: str) -> bool:
    """Whether ``rel_path`` (posix-style, relative to the repo root) looks like
    an architecture decision record by directory convention."""
    if not rel_path.endswith(".md"):
        return False
    parts = {p.lower() for p in PurePosixPath(rel_path).parts[:-1]}
    return bool(parts & _ADR_DIRS)


def _title(text: str, rel_path: str) -> str:
    m = _TITLE_RE.search(text)
    if m:
        return m.group(1).strip()
    # No H1: fall back to the filename, minus a leading ADR number
    # (`0001-use-postgres.md` -> "use postgres").
    stem = PurePosixPath(rel_path).stem
    stem = re.sub(r"^\d+[-_.]?", "", stem)
    words = stem.replace("-", " ").replace("_", " ").strip()
    return words.capitalize() if words else PurePosixPath(rel_path).stem


def parse_adr(repo_id: str, rel_path: str, source: bytes) -> list[Node]:
    """One ``adr`` node for a decision-record markdown file, or ``[]`` for an
    empty file. No edges: an ADR mentioning a class/module by name is not a
    verified reference the way a real import or call site is, and inferring
    one would violate the never-speculate rule the rest of the graph holds to.
    """
    text = source.decode("utf-8", "replace").strip()
    if not text:
        return []
    title = _title(text, rel_path)
    body = " ".join(text.split())  # collapse whitespace/newlines for the doc attr
    nid = make_id(repo_id, rel_path, "adr", title)
    return [Node(id=nid, repo=repo_id, kind="adr", name=title,
                qualified_name=rel_path, file=rel_path,
                attrs={"doc": body[:_MAX_DOC_CHARS]})]
