"""Guard: the dashboard screenshots the docs publish are real, distinct, and in both trees.

Three text sweeps passed over ``docs/img/dashboard/`` and reported it clean, because
``grep`` cannot read a PNG. Meanwhile ten of the sixteen images showed a demo fleet that
no longer exists, and two of them were byte-identical copies of two others: the page
promised an info popover under the alt text and published a second copy of the fleet
table instead. ``docs/img/dashboard/fleet-cards.png`` is also embedded in ``README.md``,
which is the PyPI long description, so it reaches every user of the package.

Nothing here reads pixels. These are the three properties a text test *can* hold:

1. **Distinct content.** Two images the docs introduce as different views must not be the
   same bytes. Stated as "no two files in the directory share a checksum" rather than as a
   named pair, because the duplicate that shipped was not predicted in advance.
2. **No dangling reference.** Every dashboard image a Markdown page points at exists on
   disk, and every light image has the dark sibling ``site/build_docs.py`` pairs it with.
3. **The two trees agree.** ``site/img/`` is a copy of ``docs/img/``, and an earlier round
   found the two can drift. A reader of the docs site and a reader of the repository must
   see the same picture.
"""

import hashlib
import re
from collections import defaultdict
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS_IMG = REPO / "docs" / "img" / "dashboard"
SITE_IMG = REPO / "site" / "img" / "dashboard"

# The Markdown pages that embed a dashboard screenshot. README.md is in the list because it
# is the PyPI long description: a broken reference there is the most widely seen of all.
_PAGES = ("README.md", "docs/using-the-dashboard.md", "docs/generating-the-wiki.md")

# Matches both spellings the docs use: the raw.githubusercontent URL (README and docs/*.md,
# which GitHub renders outside the site) and a site-relative path.
_IMG_REF = re.compile(r"[\w./-]*docs/img/dashboard/(?P<name>[a-z0-9-]+\.png)")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dashboard_pngs(root: Path) -> list[Path]:
    return sorted(root.glob("*.png"))


def test_no_two_dashboard_images_are_the_same_bytes():
    """The exact defect that shipped: ``info-popover.png`` was a byte-for-byte copy of
    ``fleet-table.png`` (and the same for the ``-dark`` pair), so the published page showed
    no popover under alt text promising one.

    Asserted over the whole directory, not over that one pair. Every file here is
    introduced in the docs as a *different* view, so any two sharing a checksum means one
    of them is not the view its caption claims. A test that named only the known pair would
    pass the next time a different pair collides.
    """
    by_digest: dict[str, list[str]] = defaultdict(list)
    for png in _dashboard_pngs(DOCS_IMG):
        by_digest[_digest(png)].append(png.name)

    duplicates = {d: names for d, names in by_digest.items() if len(names) > 1}
    assert not duplicates, (
        "these dashboard screenshots are byte-identical, so at least one of them is not the "
        f"view its alt text describes: {sorted(duplicates.values())}"
    )


def test_every_referenced_dashboard_image_exists():
    """A reference the docs make and the disk cannot answer renders as a broken image on
    GitHub, on PyPI and on the site at once."""
    missing = []
    for rel in _PAGES:
        page = REPO / rel
        assert page.is_file(), f"{rel} is missing, so this guard is not checking it"
        for match in _IMG_REF.finditer(page.read_text(encoding="utf-8")):
            name = match.group("name")
            if not (DOCS_IMG / name).is_file():
                missing.append(f"{rel} -> docs/img/dashboard/{name}")
    assert not missing, f"referenced dashboard images that do not exist: {missing}"


def test_every_light_dashboard_image_has_a_dark_sibling():
    """``site/build_docs.py`` turns ``NAME.png`` into a light+dark pair by looking for
    ``NAME-dark.png``. A light image with no dark sibling silently loses the dark variant on
    the built site, with no error at build time."""
    lights = [p for p in _dashboard_pngs(DOCS_IMG) if not p.stem.endswith("-dark")]
    assert lights, "no dashboard screenshots found at all, which is itself the failure"
    orphans = [p.name for p in lights if not (DOCS_IMG / f"{p.stem}-dark.png").is_file()]
    assert not orphans, f"dashboard images with no -dark sibling: {orphans}"


@pytest.mark.parametrize("name", [p.name for p in _dashboard_pngs(DOCS_IMG)])
def test_docs_and_site_image_trees_hold_the_same_bytes(name):
    """``site/img/`` is a copy, not a second source. When the two drift, the docs site and
    the repository show different pictures under the same caption, and only one of them was
    reviewed."""
    site_copy = SITE_IMG / name
    assert site_copy.is_file(), (
        f"docs/img/dashboard/{name} has no counterpart in site/img/dashboard/"
    )
    assert _digest(DOCS_IMG / name) == _digest(site_copy), (
        f"{name} differs between docs/img/dashboard/ and site/img/dashboard/"
    )
