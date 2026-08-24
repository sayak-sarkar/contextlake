"""Guard: no em-dash in documentation prose.

The house style has always been "no em-dashes in copy", but the only thing
enforcing it was ``de_emdash`` in ``site/build_docs.py``, which rewrites them to
commas on the way *out*, at render time. That hid the problem rather than
preventing it: the built site looked correct, so every visual review passed,
while the markdown source accumulated 110 of them across 15 files. Those reach
readers everywhere the site is not: the repository on GitHub, the project page on
PyPI (rendered from ``README.md``), and ``llms-full.txt``, which is built from the
same raw source.

A rule enforced only at render time binds one consumer and no others. This test
moves it to the way in.

Fenced code blocks are exempt. They hold captured CLI output and shell snippets
whose bytes are meant to match what a terminal actually shows, so "fixing" an
em-dash there would make the documentation disagree with the program.
"""

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# DERIVED, not listed. This guard has now been re-broken three times in the part it did not
# cover: `docs/*.md` missed the nested pages and ROADMAP; the hand-list that replaced it then
# missed SECURITY.md and the two vendored-asset READMEs, which had quietly accumulated 31
# em-dashes between them while every run stayed green. Its own docstring already recorded the
# lesson twice. A file set somebody has to remember to extend is the defect, not the omission.
#
# Two exclusions, both because the file is not hand-written prose anybody is editing to style:
#   CHANGELOG.md      historical record, append-only. Rewriting a shipped entry is forbidden.
#   tests/**          generated wiki fixtures. Editing them breaks the tests that own them.
#
# `benchmarks/` is NOT excluded, and that is the point of asking git rather than the disk: the
# cloned corpora under it are untracked, so the listing returns only this project's own four
# files there. An exclusion written from a filesystem walk would have dropped them.
_EXCLUDED = {"CHANGELOG.md"}


def _prose_files() -> list[Path]:
    """Every TRACKED markdown file that is hand-written prose.

    From `git ls-files`, not the filesystem: `rglob` reaches `.venv/`, `.pytest_cache/` and
    the cloned benchmark corpora under `benchmarks/`, which together outnumber this project's
    own prose thirty to one.
    """
    try:
        listing = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.md"],
                                 capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):  # sdist, or no git on PATH
        pytest.skip("not a git checkout, so the tracked-file set cannot be derived")
    out = [REPO / rel for rel in listing
           if "tests" not in Path(rel).parts and rel not in _EXCLUDED]
    # A listing that silently comes back empty passes every parametrised case below.
    assert len(out) >= 30, f"only {len(out)} prose files discovered; the listing is wrong"
    return out


DOC_FILES = _prose_files()


def _prose_lines(text: str):
    """Yield (line_number, line) for lines outside fenced code blocks."""
    fenced = False
    for n, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield n, line


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.name)
def test_no_emdash_in_doc_prose(path):
    if not path.exists():  # docs/*.md is a glob; the two named files may move
        pytest.skip(f"{path.name} not present")
    offenders = [
        f"{path.relative_to(REPO)}:{n}: {line.strip()[:80]}"
        for n, line in _prose_lines(path.read_text(encoding="utf-8"))
        if "—" in line
    ]
    assert not offenders, (
        "em-dash in documentation prose (house style is to avoid it, and "
        "site/build_docs.py's de_emdash only hides it on the rendered site, not on "
        "GitHub, PyPI or in llms-full.txt):\n" + "\n".join(offenders)
    )
