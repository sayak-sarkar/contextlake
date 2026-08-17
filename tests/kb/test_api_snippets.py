"""Quoted call-site source: shown only when it can be proved to be what was indexed.

The reference names a file and a line for every call site, and quoting the line is what makes
an entry an example. It is also the only part of the document that can be confidently wrong,
because the graph's line numbers were recorded at index time and the working tree moves on.

The gate is `mtime_ns <= indexed_at`. These tests exist because a gate on freshness is
worthless unless it actually refuses: every one of them checks the NEGATIVE case, that nothing
is quoted, and each negative is produced by a different real cause. A test suite that only
proved quoting works would pass just as happily with the gate deleted.

The causes are kept separate because a fix that handles one and not the others still reads as
complete: the file changed after indexing; the repository has no `indexed_at` stamp to compare
against; the working tree is not on this machine; the file is binary, or the line is blank; and
the stored path names something outside the repository, by `..`, by an absolute path, or through
a symlink. That last one was a real bug, found here, that read the file.

The bounds check on the line number is a separate matter and labelled as such below: removing
it raises rather than misquotes, so it guards a crash and not correctness.

The second half of the file tests the RENDERER against a stand-in reader, because what the page
says when a line is quotable, when one site of several is not, and when none is, are three
different pages and driving them through a filesystem would only make them slower to write.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from contextlake.kb.docs.api import render_api_reference
from contextlake.kb.docs.snippets import MAX_LINE_CHARS, SnippetReader
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = "team/svc"
SRC = "driver.py"
# The call is on line 2. Line 1 is a different statement, so a test can tell "quoted the
# right line" from "quoted a line".
LINES = "import codec\nencode(payload)\n"
THE_CALL = "encode(payload)"
NOT_THE_CALL = "import codec"


# Both times are fixed and far apart rather than relative to "now", so the gate is being
# tested and not the filesystem's mtime resolution. A test whose passing case depends on two
# timestamps a few microseconds apart fails intermittently for a reason that has nothing to do
# with the code under test.
FILE_MTIME = 1_600_000_000            # 2020-09-13
INDEXED_AT = "2021-01-01T00:00:00+00:00"   # comfortably after it


def _aged(path):
    os.utime(path, (FILE_MTIME, FILE_MTIME))
    return path


def _tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / SRC).write_text(LINES, encoding="utf-8")
    _aged(root / SRC)
    return root


def _store(tmp_path, root, *, indexed=True):
    store = SqliteStore(tmp_path / "kb.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id=REPO, path=str(root)))
    if indexed:
        store.mark_indexed(REPO, "headsha", INDEXED_AT)
    return store


@pytest.fixture
def fresh(tmp_path):
    """A tree whose files predate the index stamp: the quotable case."""
    root = _tree(tmp_path)
    store = _store(tmp_path, root)
    yield store, root
    store.close()


def test_quotes_the_line_at_the_call_site(fresh):
    store, _root = fresh
    reader = SnippetReader(store, REPO)
    assert reader.reason is None
    assert reader.line(SRC, 2) == THE_CALL
    # And it is the line asked for, not merely a line from the file.
    assert reader.line(SRC, 1) == NOT_THE_CALL


def test_a_file_written_after_indexing_is_not_quoted(fresh):
    """The core refusal. Every line number the graph holds for this file may have moved."""
    store, root = fresh
    reader = SnippetReader(store, REPO)
    assert reader.line(SRC, 2) == THE_CALL  # provable before the edit

    os.utime(root / SRC, None)  # bump mtime to now, after the index stamp
    after = SnippetReader(store, REPO)
    # The reader is per-run, so a fresh one is what a later `kb docs` would build.
    assert after.reason is None  # the repo is still fine; this one FILE is not
    assert after.line(SRC, 2) is None


def test_no_index_stamp_means_nothing_is_quoted_anywhere(tmp_path):
    """No baseline is "cannot prove", which is the same as no.

    The failure this forbids is treating a missing stamp as "assume fresh", which would quote
    every line in every repository that was registered but never indexed.
    """
    root = _tree(tmp_path)
    store = _store(tmp_path, root, indexed=False)
    try:
        reader = SnippetReader(store, REPO)
        assert reader.reason and "indexed_at" in reader.reason
        assert reader.line(SRC, 2) is None
    finally:
        store.close()


def test_a_missing_working_tree_is_reported_not_treated_as_an_error(tmp_path):
    """A store outliving its checkout is ordinary. The graph is still fully usable."""
    store = SqliteStore(tmp_path / "kb.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id=REPO, path=str(tmp_path / "gone")))
    store.mark_indexed(REPO, "headsha", INDEXED_AT)
    try:
        reader = SnippetReader(store, REPO)
        assert reader.reason and "working tree" in reader.reason
        assert reader.line(SRC, 2) is None
    finally:
        store.close()


def test_a_line_past_the_end_of_the_file_is_not_quoted(fresh):
    """A shrunken file must not take the run down.

    Removing the bounds check makes this raise IndexError rather than quote a wrong line, so
    what it guards is a CRASH and not an incorrect quote. Stated plainly because the guard
    reads as though it were protecting correctness, and only the mtime gate does that.
    """
    store, _root = fresh
    reader = SnippetReader(store, REPO)
    assert reader.line(SRC, 999) is None


def test_a_blank_line_is_not_quoted(fresh):
    """An empty cell claiming to be the call site is worse than no cell."""
    store, root = fresh
    (root / "blank.py").write_text("\n\n", encoding="utf-8")
    _aged(root / "blank.py")
    reader = SnippetReader(store, REPO)
    assert reader.line("blank.py", 1) is None


def test_a_very_long_line_is_truncated(fresh):
    """A minified bundle is one enormous line and no document is improved by all of it."""
    store, root = fresh
    (root / "big.js").write_text("x=" + ("a" * 5000) + "\n", encoding="utf-8")
    _aged(root / "big.js")
    got = SnippetReader(store, REPO).line("big.js", 1)
    assert got is not None
    assert len(got) <= MAX_LINE_CHARS + 3
    assert got.endswith("...")


def test_a_binary_file_is_not_quoted(fresh):
    store, root = fresh
    (root / "blob.bin").write_bytes(b"\x00\xff\xfe binary \x00")
    _aged(root / "blob.bin")
    assert SnippetReader(store, REPO).line("blob.bin", 1) is None


class _Reader:
    """A stand-in for `SnippetReader`, so the RENDERER's behaviour can be tested alone.

    Deliberately not a real reader over a real tree: these tests are about what the page says
    when a line is quotable, when it is not, and when nothing is, and driving those three
    states through the filesystem would make them slow and indirect.
    """

    def __init__(self, lines=None, reason=None):
        self.reason = reason
        self._lines = lines or {}

    def line(self, path, lineno):
        return self._lines.get((path, lineno))


def _one_call_shard():
    """One documented symbol called once, from a real caller, at `driver.py:20`."""
    prov = Provenance(source_file="driver.py", source_line=20,
                      verified_at=date(2026, 8, 17))
    return GraphShard(
        repo=REPO, head_commit="h1",
        nodes=[Node(id="t", repo=REPO, kind="function", name="encode", file="codec.py",
                    line_start=1, line_end=3, qualified_name="encode"),
               Node(id="c", repo=REPO, kind="function", name="drive", file="driver.py",
                    line_start=10, line_end=30, qualified_name="drive")],
        edges=[Edge(src="c", dst="t", relation="calls", confidence=Confidence.EXTRACTED,
                    provenance=prov)])


def test_the_page_quotes_the_call_line_in_a_source_column():
    """The point of the whole feature: an entry carries the calling code, not just a pointer."""
    page = render_api_reference(_one_call_shard(), repo_id=REPO,
                               snippets=_Reader({("driver.py", 20): THE_CALL}))
    assert "| Caller | File | Line | Source |" in page
    assert f"`{THE_CALL}`" in page


def test_the_page_has_no_source_column_when_no_line_is_quotable():
    """A column of blanks would claim the feature ran and found nothing to show."""
    page = render_api_reference(_one_call_shard(), repo_id=REPO, snippets=_Reader({}))
    assert "| Caller | File | Line |" in page
    assert "Source" not in page


def test_the_page_states_why_nothing_is_quoted():
    """Silence would read as a repository with no call sites, which is a different fact."""
    page = render_api_reference(_one_call_shard(), repo_id=REPO,
                               snippets=_Reader(reason="the tree is not here"))
    assert "Call sites below are not quoted: the tree is not here." in page
    assert "Source" not in page


def test_one_unquotable_site_among_quotable_ones_is_marked_not_blank():
    """A blank cell in a Source column reads as "the call site is empty", which it is not."""
    shard = _one_call_shard()
    prov = Provenance(source_file="driver.py", source_line=21,
                      verified_at=date(2026, 8, 17))
    shard.edges.append(Edge(src="c", dst="t", relation="calls",
                            confidence=Confidence.EXTRACTED, provenance=prov))
    page = render_api_reference(shard, repo_id=REPO,
                               snippets=_Reader({("driver.py", 20): THE_CALL}))
    assert f"`{THE_CALL}`" in page
    assert "*changed since indexing*" in page


OUTSIDE_TEXT = "do not quote me"


@pytest.mark.parametrize("how", ["dotdot", "absolute", "symlink"])
def test_a_path_escaping_the_tree_is_not_quoted(fresh, tmp_path, how):
    """Three ways a stored path can name a file outside the repository. None may be quoted.

    Found by the `dotdot` case, which read the file: `root / "../secret.txt"` resolves upward.
    The other two are the same hole reached differently, and each is checked because a fix that
    handles one spelling and not the others reads as complete.
    """
    store, root = fresh
    outside = tmp_path / "secret.txt"
    outside.write_text(OUTSIDE_TEXT + "\n", encoding="utf-8")
    _aged(outside)

    if how == "dotdot":
        rel = "../secret.txt"
    elif how == "absolute":
        rel = str(outside)
    else:
        (root / "link.txt").symlink_to(outside)
        rel = "link.txt"

    assert SnippetReader(store, REPO).line(rel, 1) != OUTSIDE_TEXT
