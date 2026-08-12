"""Is the knowledge graph still describing the code that is on disk?

This exists because staleness here is **silent by construction**. `index` skips a repo
whose head commit has not moved, which is the right default and also means a store can
be arbitrarily far behind while every command it serves looks healthy: `doctor` counts
rows, `query` returns hits, and the hits cite lines from a commit nobody has today. The
project has shipped that bug twice (a stale index invisible after an upgrade, and derived
artefacts reporting themselves fresh across a parser-version change).

So this module answers the question cheaply enough to ask at the start of every coding
session: which repos moved, which were built by an older parser, and are the vectors
built from an older text format. It reads, it never writes, and it never parses -- one
`git rev-parse` per repo and two indexed lookups.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

_HEAD_TIMEOUT = 5
"""Seconds for one `git rev-parse`. A repo on a dead network mount must not be able to
hold up a session start."""

DEFAULT_BUDGET = 3.0
"""Wall-clock seconds this check may spend before it stops and says so.

A `git rev-parse` costs a few milliseconds, which is nothing for ten repositories and
seconds for several hundred -- and this runs while somebody waits for their session to
open. The budget is what turns "it depends how big your fleet is" into a bounded cost.
Anything unchecked is reported as unchecked: a cap nobody is told about reads as a clean
bill of health for work that never happened."""


@dataclass
class Freshness:
    repos: int = 0
    checked: int = 0
    moved: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    stale_parser: list[str] = field(default_factory=list)
    unchecked: int = 0
    vectors_stale: bool = False
    vectors_missing: bool = False
    elapsed: float = 0.0

    @property
    def is_stale(self) -> bool:
        """Whether anything worth re-indexing was found.

        `unreadable` is deliberately excluded: a repo whose clone is gone is a mirror
        problem that re-indexing cannot fix, and treating it as staleness would mean
        every session start proposes work that changes nothing."""
        return bool(self.moved or self.stale_parser or self.vectors_stale)

    def summary(self) -> str:
        """One line, aimed at whoever (or whatever) is about to start work."""
        if not self.repos:
            return "contextlake: the knowledge store holds no repositories yet."
        bits = []
        if self.moved:
            bits.append(f"{len(self.moved)} repo(s) moved since indexing")
        if self.stale_parser:
            bits.append(f"{len(self.stale_parser)} indexed by an older parser")
        if self.vectors_stale:
            bits.append("vectors built from an older text format")
        if self.vectors_missing:
            bits.append("no vectors built")
        if self.unreadable:
            bits.append(f"{len(self.unreadable)} clone(s) unreadable")
        if self.unchecked:
            bits.append(f"{self.unchecked} not checked (time budget)")
        if not bits:
            return f"contextlake: graph is current for all {self.repos} repositories."
        return f"contextlake: {self.repos} repositories, " + "; ".join(bits) + "."


def _head(path: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                           capture_output=True, text=True, errors="replace",
                           timeout=_HEAD_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def check(store, store_dir, *, budget: float = DEFAULT_BUDGET) -> Freshness:
    """Compare the store against the checkouts and the current code versions."""
    from .embeddings.index import EMBED_CONTENT_VERSION
    from .parse import PARSER_VERSION
    from .state import indexed_parser_version

    f = Freshness()
    repos = store.list_repos()
    f.repos = len(repos)
    t0 = time.monotonic()
    for r in repos:
        if time.monotonic() - t0 > budget:
            f.unchecked = len(repos) - f.checked
            break
        f.checked += 1
        if not r.path or not Path(r.path).is_dir():
            f.unreadable.append(r.id)
            continue
        head = _head(r.path)
        if head is None:
            f.unreadable.append(r.id)
        elif r.head_commit and head != r.head_commit:
            f.moved.append(r.id)
        was = indexed_parser_version(store, store_dir, r.id)
        if was is not None and was != PARSER_VERSION:
            f.stale_parser.append(r.id)
    # Vectors are optional, so their absence is a fact to report rather than a problem
    # to flag -- plenty of stores never enable embeddings.
    vec = Path(store_dir) / "embeddings.sqlite"
    if not vec.exists():
        f.vectors_missing = True
    else:
        try:
            from .embeddings.store import build_vector_store, get_content_version
            vs = build_vector_store(vec)
            try:
                f.vectors_stale = (vs.count() > 0
                                   and get_content_version(vs) != EMBED_CONTENT_VERSION)
            finally:
                vs.close()
        except Exception:  # noqa: BLE001 - a freshness check must never be the thing
            # that fails a session start; an unopenable vector store is doctor's job.
            f.vectors_stale = False
    f.elapsed = round(time.monotonic() - t0, 3)
    return f
