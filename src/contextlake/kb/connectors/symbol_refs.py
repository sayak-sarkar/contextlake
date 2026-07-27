"""Per-symbol issue-key candidates: docstrings and git blame.

Today's ``tracked_by`` edges only ever originate from the *repo* node (a
branch name or a doc-link references an issue somewhere in the repo, but not
which symbol). This module finds candidates that DO know which symbol: an
issue key written in a symbol's own docstring (an explicit signal, the same
trust tier as a doc-link reference), or an issue key in the commit message
that last touched the symbol's defining line (an implicit signal, the same
trust tier as a branch-name-derived key).

Both are bare regex matches, not URLs, so both are AMBIGUOUS candidates,
fed through the exact same live-JQL ``verify_issues``/``reconcile`` pipeline
that already promotes repo-level branch-derived keys to INFERRED -- this is
a new candidate *source*, not a new trust model.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict

from ..embeddings.index import EMBEDDABLE_KINDS
from ..model import Node

__all__ = ["keys_from_blame", "keys_from_docstrings"]

_BLAME_HEADER_RX = re.compile(r"^[0-9a-f]{40} \d+ (\d+)")


def keys_from_docstrings(symbols: list[Node], pattern: str) -> dict[str, str]:
    """``{symbol_id: issue_key}`` for symbols whose own docstring/doc attr
    contains a match for ``pattern`` (no network, no subprocess)."""
    rx = re.compile(pattern)
    out: dict[str, str] = {}
    for n in symbols:
        if n.kind not in EMBEDDABLE_KINDS:
            continue
        doc = (n.attrs or {}).get("doc") or ""
        m = rx.search(doc)
        if m:
            out[n.id] = m.group(0)
    return out


def _parse_blame_porcelain(output: str) -> dict[int, str]:
    """``{line_number: commit_subject}`` out of ``git blame --line-porcelain``."""
    line_no: int | None = None
    out: dict[int, str] = {}
    for raw in output.splitlines():
        m = _BLAME_HEADER_RX.match(raw)
        if m:
            line_no = int(m.group(1))
        elif raw.startswith("summary ") and line_no is not None:
            out[line_no] = raw[len("summary "):]
    return out


def keys_from_blame(repo_path: str, symbols: list[Node], pattern: str, *,
                    timeout: float = 30) -> dict[str, str]:
    """``{symbol_id: issue_key}`` via one batched ``git blame`` per file: the
    commit message that last touched each symbol's defining line, matched
    against ``pattern``. Never raises -- a missing repo/file/git failure
    just yields no candidates for that file, never aborts the whole repo."""
    rx = re.compile(pattern)
    by_file: dict[str, list[Node]] = defaultdict(list)
    for n in symbols:
        if n.kind in EMBEDDABLE_KINDS and n.file and n.line_start:
            by_file[n.file].append(n)

    out: dict[str, str] = {}
    for file, nodes in by_file.items():
        try:
            res = subprocess.run(
                ["git", "blame", "--line-porcelain", "--", file],
                cwd=repo_path, capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode != 0:
            continue
        by_line = _parse_blame_porcelain(res.stdout)
        for n in nodes:
            subject = by_line.get(n.line_start)
            if not subject:
                continue
            m = rx.search(subject)
            if m:
                out[n.id] = m.group(0)
    return out
