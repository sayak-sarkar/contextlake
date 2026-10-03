"""Readers of a clone's own files must not follow a symlink out of the clone.

A mirrored clone is untrusted: anyone who can commit to the repository can commit a symlink.
`read_repo_file` (9.3.0) closed this for the README readers. Two more readers still did
`f.is_file()` then `f.read_text()`, and both follow links:

- `references.scrape_links` read every `.md/.txt/.rst/.adoc` file under the clone, so a doc
  symlinked to a file elsewhere on the machine had its text scanned and any URL in it
  became a link on the repo's node.
- `parse.load_ignore_patterns` read `.contextlakeignore`, so a link there put the target's
  lines into the ignore patterns that decide which files are indexed.

Every fixture file outside the clone sits in `tmp_path`. Each reader has a symlink-LOOP
case: the loop has to be skipped, not raised on (`Path.resolve` raises RuntimeError for a
loop on Python 3.10 to 3.12).
"""

from __future__ import annotations

import pytest

from contextlake.kb.parse import load_ignore_patterns
from contextlake.kb.references import scrape_links

LINK = r"https://example\.invalid/\S+"
INSIDE = "https://example.invalid/INSIDE-LINK"


def _symlink(link, target, **kw):
    try:
        link.symlink_to(target, **kw)
    except (OSError, NotImplementedError):          # Windows without the privilege
        pytest.skip("symlinks are not available here")


@pytest.fixture
def world(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "doc.md").write_text("see https://example.invalid/OUTSIDE-LINK\n")
    (outside / "ignore.txt").write_text("OUTSIDE-IGNORE-*\n")
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "inside.md").write_text(f"see {INSIDE}\n")
    return clone, outside


# --- scrape_links ----------------------------------------------------------------------

def test_a_doc_symlinked_outside_the_clone_is_not_scraped(world):
    clone, outside = world
    _symlink(clone / "linked.md", outside / "doc.md")

    assert scrape_links(str(clone), [LINK]) == [INSIDE]


def test_a_doc_in_a_symlinked_directory_outside_the_clone_is_not_scraped(world):
    clone, outside = world
    _symlink(clone / "docs", outside, target_is_directory=True)

    assert scrape_links(str(clone), [LINK]) == [INSIDE]


def test_scrape_links_survives_symlink_loops(world):
    clone, _ = world
    _symlink(clone / "self.md", clone / "self.md")             # a file that links to itself
    (clone / "d").mkdir()
    _symlink(clone / "d" / "up", clone / "d", target_is_directory=True)   # a directory loop

    assert scrape_links(str(clone), [LINK]) == [INSIDE]


def test_scrape_links_still_reads_a_clone_reached_through_a_symlink(world, tmp_path):
    """The root itself may be a link (a workspace that links its clones in). Both sides are
    resolved, so the files under it are inside it."""
    clone, _ = world
    root_link = tmp_path / "root_link"
    _symlink(root_link, clone, target_is_directory=True)

    assert scrape_links(str(root_link), [LINK]) == [INSIDE]


def test_scrape_links_follows_a_link_that_stays_inside_the_clone(world):
    """The target has a suffix scrape_links skips, so the link is the only route to it."""
    clone, _ = world
    (clone / "real").mkdir()
    (clone / "real" / "notes.data").write_text("https://example.invalid/ONLY-VIA-LINK\n")
    _symlink(clone / "alias.md", clone / "real" / "notes.data")

    assert scrape_links(str(clone), [LINK]) == [
        INSIDE, "https://example.invalid/ONLY-VIA-LINK"]


# --- load_ignore_patterns --------------------------------------------------------------

def test_an_ignore_file_symlinked_outside_the_clone_is_not_read(world):
    clone, outside = world
    _symlink(clone / ".contextlakeignore", outside / "ignore.txt")

    assert load_ignore_patterns(clone) == []


def test_an_ignore_file_symlink_loop_is_skipped(world):
    clone, _ = world
    _symlink(clone / ".contextlakeignore", clone / ".contextlakeignore")

    assert load_ignore_patterns(clone) == []


def test_an_ignore_file_symlinked_inside_the_clone_is_read(world):
    clone, _ = world
    (clone / "config").mkdir()
    (clone / "config" / "ignore").write_text("# comment\n\n*.lock\nvendor/\n")
    _symlink(clone / ".contextlakeignore", clone / "config" / "ignore")

    assert load_ignore_patterns(clone) == ["*.lock", "vendor/"]


def test_a_plain_ignore_file_is_unchanged(world):
    clone, _ = world
    (clone / ".contextlakeignore").write_text("# c\n  build  \n\n*.min.js\n")

    assert load_ignore_patterns(clone) == ["build", "*.min.js"]


def test_a_missing_ignore_file_is_no_patterns(world):
    clone, _ = world

    assert load_ignore_patterns(clone) == []
