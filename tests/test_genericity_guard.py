"""Guard: no organisation-specific token may appear in this public repository.

contextlake is developed against a private fleet and published openly. On
2026-07-25 employer identifiers reached this repo and its history and had to be
removed with a rewrite and a force-push. This test is the standing check that
stopped that being possible twice.

**The denylist is deliberately not committed.** Publishing the list of words you
are hiding defeats the point, so the tokens arrive from outside:

* ``CONTEXTLAKE_GENERICITY_DENYLIST`` -- comma-separated tokens. CI supplies this
  from a repository secret.
* ``CONTEXTLAKE_GENERICITY_DENYLIST_FILE`` -- path to a file with one token per
  line, ``#`` comments allowed. Convenient locally.

With neither set the test **skips** rather than fails. That is deliberate: an
earlier version hard-failed when it could not find a local list, which made every
clean clone and every CI cell red for a reason unrelated to the change under
test, and it was deleted rather than fixed. A guard nobody can run is worth less
than one that says clearly when it is inactive.

It scans what is *tracked right now* rather than a diff, because the question is
"is the published tree clean", not "did this commit add something". A token
introduced earlier and never removed must still fail.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Scanning our own source would match every token we are looking for.
SELF = "tests/test_genericity_guard.py"

# Binary and vendored payloads: matching inside a minified bundle or a PNG is
# noise, and these carry no prose an author could leak into.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".pdf", ".zip", ".gz",
    ".woff", ".woff2", ".ttf", ".otf", ".min.js", ".min.css", ".pyc",
}


def _denylist() -> list[str]:
    """Tokens to refuse, from the environment. Empty when unconfigured."""
    raw = os.environ.get("CONTEXTLAKE_GENERICITY_DENYLIST", "")
    if raw.strip():
        return [t.strip().lower() for t in raw.split(",") if t.strip()]

    path = os.environ.get("CONTEXTLAKE_GENERICITY_DENYLIST_FILE", "")
    if path and Path(path).expanduser().is_file():
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
        return [ln.strip().lower() for ln in lines
                if ln.strip() and not ln.lstrip().startswith("#")]
    return []


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True)
    return [ln for ln in out.stdout.splitlines() if ln]


def _should_scan(rel: str) -> bool:
    if rel == SELF:
        return False
    lower = rel.lower()
    return not any(lower.endswith(suffix) for suffix in SKIP_SUFFIXES)


def test_no_org_specific_tokens_in_tracked_files():
    tokens = _denylist()
    if not tokens:
        pytest.skip(
            "genericity guard inactive: set CONTEXTLAKE_GENERICITY_DENYLIST "
            "(comma-separated) or CONTEXTLAKE_GENERICITY_DENYLIST_FILE (path). "
            "CI supplies the former from a secret."
        )

    findings: list[str] = []
    for rel in _tracked_files():
        if not _should_scan(rel):
            continue
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        lowered = text.lower()
        for token in tokens:
            if token in lowered:
                # Name the line so the fix is obvious; never echo the token
                # itself into CI logs, which are public on a public repo.
                for n, line in enumerate(text.splitlines(), 1):
                    if token in line.lower():
                        findings.append(f"{rel}:{n} (denylisted token #{tokens.index(token)})")
                        break

    assert not findings, (
        "organisation-specific tokens found in tracked files. This repository is "
        "public; remove them and, if any were ever committed, scrub the history "
        "rather than only the tip:\n  " + "\n  ".join(findings)
    )
