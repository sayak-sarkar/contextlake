"""Repo-side association signals.

Scans a repository for the external items it references — issue keys (in branch
names and commit subjects) and links to external systems (Atlassian/Figma URLs in
docs) — so the knowledge layer can connect a repo to its tracker, docs, and
designs once a connector fetches those. All patterns are configured (generic);
nothing deployment-specific is hardcoded.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .. import style
from ..logging_setup import log

_DOC_SUFFIXES = {".md", ".txt", ".rst", ".adoc"}


def extract_issue_keys(repo_path: str, pattern: str, commit_limit: int = 500) -> list[str]:
    """Distinct issue keys matching ``pattern`` in branch names + commit subjects."""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        # A typo in a hand-written `[[rules]]` regex used to disable the rule with no
        # message at all, and `doctor` then confirmed the rule was loaded -- so the user
        # saw a configured rule that silently matched nothing, forever. The rule is still
        # skipped (one bad pattern must not abort the run); it is no longer silent.
        log(style.warn(f"rules: ignoring an invalid issue-key pattern {pattern!r} -- {e}"))
        return []
    blobs = []
    for args in (
        ["branch", "-a", "--format=%(refname:short)"],
        ["log", "--format=%s", "-n", str(commit_limit)],
    ):
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_path), *args],
                capture_output=True, text=True, errors="replace", timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            blobs.append(out.stdout)
    return sorted({m.group(0) for m in rx.finditer("\n".join(blobs))})


def scrape_links(repo_path: str, patterns: list[str], max_files: int = 500) -> list[str]:
    """Distinct URLs in docs matching any of ``patterns``."""
    compiled = []
    bad = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error as e:
            bad.append(f"{p!r} ({e})")
    if bad:
        log(style.warn(
            f"rules: ignoring {len(bad)} invalid link-scrape pattern(s): "
            f"{'; '.join(bad[:3])}"
            f"{f' (+{len(bad) - 3} more)' if len(bad) > 3 else ''}"))
    if not compiled:
        return []
    found: set[str] = set()
    scanned = 0
    for f in sorted(Path(repo_path).rglob("*")):
        if scanned >= max_files:
            break
        if not f.is_file() or f.suffix.lower() not in _DOC_SUFFIXES:
            continue
        scanned += 1
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx in compiled:
            found.update(m.group(0) for m in rx.finditer(text))
    return sorted(found)
