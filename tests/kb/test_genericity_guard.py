"""Genericity guard (principle G1): no organization-specific data in the source.

Scans the whole published surface — src/, docs/, examples/, .github/, and the
top-level docs — for a denylist of org-specific tokens and fails the build if any
appear. tests/ is intentionally NOT scanned: this guard (and a few fixtures)
legitimately contain the denylist tokens to prove the check works.
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

# Published surface to scan. tests/ is excluded (it contains the tokens by design);
# .git and local-only .notes/ never reach the published repo.
SCAN_DIRS = ["src", "docs", "examples", ".github"]
SCAN_ROOT_FILES = [
    "README.md", "QUICKSTART.md", "CHANGELOG.md", "ROADMAP.md",
    "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "pyproject.toml",
]


def _scan(root: Path) -> list[tuple[str, str]]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        base = root / d
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file()]
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


def test_guard_scans_docs_too(tmp_path):
    # regression: docs/ is part of the published surface and must be covered
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "leak.md").write_text("we use vendorco internally\n")
    assert _scan(tmp_path), "the guard must scan docs/ as well as src/"
