"""Genericity guard (principle G1): no organization-specific data in the source.

Scans the published source tree (src/ + the top-level docs) for a denylist of
org-specific tokens and fails the build if any appear. Placeholders used in
examples/ and tests/ (e.g. "acme", "your-org") are intentionally NOT scanned.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Real organization tokens that must never ship in the published code/docs.
# (Lower-cased; matched case-insensitively.)
DENYLIST = [
    "examplecorp",
    "vendorco",
    "api-docs",
    "00000000-0000",  # a real cloudId fragment
    "example-group.atlassian",
]

# Published surface to scan. examples/ and tests/ are deliberately excluded.
SCAN_DIRS = ["src"]
SCAN_ROOT_FILES = ["README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "pyproject.toml"]


def _scan(root: Path) -> list[tuple[str, str]]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        files += [p for p in (root / d).rglob("*") if p.is_file()]
    files += [root / f for f in SCAN_ROOT_FILES if (root / f).is_file()]

    hits = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for token in DENYLIST:
            if token in text:
                hits.append((str(f.relative_to(root)), token))
    return hits


def test_source_has_no_org_tokens():
    hits = _scan(REPO)
    assert hits == [], f"organization-specific tokens found in published source: {hits}"


def test_guard_detects_a_planted_token(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "leak.py").write_text("SITE = 'examplecorp-example-group.atlassian.net'\n")
    assert _scan(tmp_path), "the guard must catch a planted org token"
