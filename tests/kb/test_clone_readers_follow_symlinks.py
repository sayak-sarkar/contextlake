"""Readers of a clone's own files follow a symlink wherever it points.

contextlake reads data wherever a configured source points (owner, 2026-10-15). 9.3.0 and
9.4.0 made these readers refuse a link out of the clone; that limit is removed:

- `references.scrape_links` scans the text of a `.md/.txt/.rst/.adoc` file linked from
  anywhere. `rglob` does not descend into a symlinked DIRECTORY on any Python CI runs, so a
  linked directory's files are not scanned; that is the walk's rule, not a containment
  check.
- `parse.load_ignore_patterns` reads a `.contextlakeignore` linked from anywhere.
- the source walk indexes a source file linked from anywhere.
- the snippet reader quotes a line from a file linked from anywhere. It still refuses a
  stored path whose NAME leaves the repository (`..`, absolute).

What a followed link may not do is hang or exhaust memory: a link to `/dev/zero` or to a
FIFO is skipped, because only a regular file is read. The tests link to `/dev/null`, the same
file type as `/dev/zero`: a regression then fails an assertion instead of exhausting memory.
A FIFO regression blocks until the suite's 300 s per-test timeout.

Each reader has a symlink-LOOP case: the loop is skipped, not raised on (`Path.resolve`
raises RuntimeError for a loop on Python 3.10 to 3.12).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from contextlake.cli import main
from contextlake.kb.parse import load_ignore_patterns
from contextlake.kb.references import scrape_links
from contextlake.kb.store.sqlite_store import SqliteStore

LINK = r"https://example\.invalid/\S+"
INSIDE = "https://example.invalid/INSIDE-LINK"
OUTSIDE = "https://example.invalid/OUTSIDE-LINK"
CANARY = "followed_link_canary_outside_the_clone"

needs_special_files = pytest.mark.skipif(
    not os.path.exists("/dev/null") or not hasattr(os, "mkfifo"),
    reason="needs /dev/null and FIFOs")


def _symlink(link, target, **kw):
    try:
        link.symlink_to(target, **kw)
    except (OSError, NotImplementedError):          # Windows without the privilege
        pytest.skip("symlinks are not available here")


def _special(tmp_path, kind):
    """A path to a device or a FIFO, outside the clone."""
    if kind == "fifo":
        os.mkfifo(tmp_path / "the.fifo")
        return tmp_path / "the.fifo"
    return "/dev/null"


@pytest.fixture
def world(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "doc.md").write_text(f"see {OUTSIDE}\n")
    (outside / "ignore.txt").write_text("OUTSIDE-IGNORE-*\n")
    (outside / "canary.py").write_text(f"def {CANARY}():\n    return 1\n")
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "inside.md").write_text(f"see {INSIDE}\n")
    return clone, outside


# --- scrape_links ----------------------------------------------------------------------

def test_a_doc_symlinked_outside_the_clone_is_scraped(world):
    clone, outside = world
    _symlink(clone / "linked.md", outside / "doc.md")

    assert scrape_links(str(clone), [LINK]) == [INSIDE, OUTSIDE]


def test_scrape_links_survives_symlink_loops(world):
    clone, _ = world
    _symlink(clone / "self.md", clone / "self.md")             # a file that links to itself
    (clone / "d").mkdir()
    _symlink(clone / "d" / "up", clone / "d", target_is_directory=True)   # a directory loop

    assert scrape_links(str(clone), [LINK]) == [INSIDE]


@needs_special_files
def test_scrape_links_skips_a_doc_linked_to_a_fifo(world, tmp_path):
    """Only the FIFO case: an empty device reads as no links, the same answer as a skip."""
    clone, _ = world
    _symlink(clone / "special.md", _special(tmp_path, "fifo"))

    assert scrape_links(str(clone), [LINK]) == [INSIDE]


def test_scrape_links_still_reads_a_clone_reached_through_a_symlink(world, tmp_path):
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

def test_an_ignore_file_symlinked_outside_the_clone_is_read(world):
    clone, outside = world
    _symlink(clone / ".contextlakeignore", outside / "ignore.txt")

    assert load_ignore_patterns(clone) == ["OUTSIDE-IGNORE-*"]


def test_an_ignore_file_symlink_loop_is_skipped(world):
    clone, _ = world
    _symlink(clone / ".contextlakeignore", clone / ".contextlakeignore")

    assert load_ignore_patterns(clone) == []


@needs_special_files
def test_an_ignore_file_linked_to_a_fifo_is_skipped(world, tmp_path):
    """Only the FIFO case: an empty device reads as no patterns, the same answer as a skip."""
    clone, _ = world
    _symlink(clone / ".contextlakeignore", _special(tmp_path, "fifo"))

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


# --- the source walk -------------------------------------------------------------------

def _index(tmp_path, clone):
    subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True, capture_output=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        main(["kb", "index", "--config", str(cfg), "--source", str(clone)])
    assert e.value.code == 0
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    try:
        return {n.name: n.file for n in store.nodes_by_name(CANARY)}
    finally:
        store.close()


def test_the_walk_indexes_a_source_file_linked_from_outside_the_clone(world, tmp_path):
    clone, outside = world
    _symlink(clone / "linked.py", outside / "canary.py")

    assert _index(tmp_path, clone) == {CANARY: "linked.py"}


@needs_special_files
@pytest.mark.parametrize("kind", ["device", "fifo"])
def test_the_walk_skips_a_source_file_linked_to_a_device_or_fifo(world, tmp_path, capsys,
                                                                 kind):
    """Before the fix a link to `/dev/zero` read until a 1 GB cap killed the index (2 s),
    and a link to a FIFO blocked it until a 60 s timeout. The rest is still indexed."""
    clone, outside = world
    _symlink(clone / "special.py", _special(tmp_path, kind))
    _symlink(clone / "linked.py", outside / "canary.py")

    assert _index(tmp_path, clone) == {CANARY: "linked.py"}
    assert "skip special.py: not a regular file" in capsys.readouterr().out


# --- the snippet reader ----------------------------------------------------------------

def _reader(tmp_path, clone):
    from contextlake.kb.docs.snippets import SnippetReader
    from contextlake.kb.model import Repo
    from contextlake.kb.state import check_schema

    store = SqliteStore(tmp_path / "snip.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="team/r", path=str(clone)))
    # Stamped in the future, so every file is older than the index: the freshness check
    # passes and cannot be what refuses a line.
    store.mark_indexed("team/r", "headsha", "2099-01-01T00:00:00+00:00")
    return store, SnippetReader(store, "team/r")


def test_the_snippet_reader_quotes_a_file_linked_from_outside_the_clone(world, tmp_path):
    clone, outside = world
    _symlink(clone / "linked.py", outside / "canary.py")
    (clone / "plain.py").write_text("x = 1\n")
    store, reader = _reader(tmp_path, clone)
    try:
        assert reader.line("plain.py", 1) == "x = 1"
        assert reader.line("linked.py", 1) == f"def {CANARY}():"
    finally:
        store.close()


@pytest.mark.parametrize("stored", ["../outside/canary.py", "./plain.py", "/etc/hostname"])
def test_the_snippet_reader_refuses_a_stored_path_whose_name_leaves_the_repo(world, tmp_path,
                                                                           stored):
    """A `..`, a `.` or an absolute path is refused by NAME. Only a link is followed."""
    clone, outside = world
    (clone / "plain.py").write_text("x = 1\n")
    store, reader = _reader(tmp_path, clone)
    try:
        assert reader.line(stored, 1) is None
        assert reader.line("plain.py", 1) == "x = 1"            # the positive control
    finally:
        store.close()


@needs_special_files
def test_the_snippet_reader_skips_a_file_linked_to_a_fifo(world, tmp_path):
    """Only the FIFO case: an empty device has no line 1, the same answer as a skip."""
    clone, _ = world
    _symlink(clone / "special.py", _special(tmp_path, "fifo"))
    store, reader = _reader(tmp_path, clone)
    try:
        assert reader.line("special.py", 1) is None
    finally:
        store.close()
