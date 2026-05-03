"""Genericity guard (principle G1): no organization-specific data in the published repo.

ABSOLUTE policy: the published surface must contain ZERO org-specific tokens — and that
includes this guard itself. The real denylist of org tokens is therefore **not stored in
the repo**; it is supplied at check time via the ``CONTEXTLAKE_GENERICITY_DENYLIST`` env
var (comma-separated) or a local, git-ignored ``.genericity-denylist`` file (one token per
line, ``#`` comments allowed). CI provides it from a secret. With no denylist configured
the token scan skips loudly — but the always-on **structural** checks below still run, so
the guard is never fully toothless.

The scanned surface is **every git-tracked file** except the vendored minified library
(false positives) and ``tests/`` itself (fixtures legitimately carry *synthetic* markers).
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Never scanned: the vendored library (minified -> substring false positives).
_EXCLUDE_SUBSTR = ("static/cytoscape.min.js",)

# Structural check: only these exact emails (and *.example.com) may appear publicly.
_ALLOWED_EMAILS = {"sayak.bugsmith@gmail.com", "noreply@github.com"}
_EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)


def _load_denylist() -> list[str] | None:
    """Real org tokens, from outside the repo (env var, else git-ignored file)."""
    env = os.environ.get("CONTEXTLAKE_GENERICITY_DENYLIST")
    if env:
        return [t.strip().lower() for t in env.split(",") if t.strip()]
    f = REPO / ".genericity-denylist"
    if f.is_file():
        return [ln.strip().lower() for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    return None


def _tracked_files() -> list[Path]:
    """Every git-tracked file in the published surface (minus excludes + tests/)."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                             text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    files = []
    for rel in out.splitlines():
        if rel.startswith("tests/") or any(x in rel for x in _EXCLUDE_SUBSTR):
            continue
        files.append(REPO / rel)
    return files


def _scan(files, denylist) -> list[tuple[str, str]]:
    hits = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for token in denylist:
            if token in text:
                hits.append((Path(f).name, token))
    return hits


def test_source_has_no_org_tokens():
    denylist = _load_denylist()
    if not denylist:
        pytest.skip("no denylist configured — set CONTEXTLAKE_GENERICITY_DENYLIST "
                    "(comma-separated) or a local .genericity-denylist file; CI uses a secret")
    files = _tracked_files()
    if not files:
        pytest.skip("not a git checkout — the token scan needs `git ls-files`")
    hits = _scan(files, denylist)
    assert hits == [], f"organization-specific tokens found in published source: {hits}"


def test_no_foreign_emails_in_published_source():
    """Always-on (no denylist needed): no email but the maintainer's / examples."""
    files = _tracked_files()
    if not files:
        pytest.skip("not a git checkout")
    bad = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _EMAIL_RE.findall(text):
            low = m.lower()
            if low not in _ALLOWED_EMAILS and not low.endswith("example.com"):
                bad.append((f.name, m))
    assert bad == [], f"non-allowlisted email addresses in published source: {bad}"


# --- the guard's own self-tests use SYNTHETIC markers, never real org tokens ---

def test_guard_detects_a_planted_token(tmp_path):
    marker = "zz-synthetic-org-marker"
    leak = tmp_path / "leak.py"
    leak.write_text(f"SITE = '{marker}.internal'\n")
    assert _scan([leak], [marker]), "the guard must catch a planted org token"


def test_guard_scans_a_doc_file(tmp_path):
    marker = "zz-synthetic-org-marker"
    leak = tmp_path / "leak.md"
    leak.write_text(f"we use {marker} internally\n")
    assert _scan([leak], [marker]), "the guard must scan docs as well as source"
