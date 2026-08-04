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

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# The prose surfaces. `docs/*.md` builds the site; README is also the PyPI project
# page; QUICKSTART is linked from both.
DOC_FILES = sorted(REPO.glob("docs/*.md")) + [REPO / "README.md", REPO / "QUICKSTART.md"]


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
