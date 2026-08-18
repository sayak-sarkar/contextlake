"""`kb ingest` must not call a path it never looked at "reachable".

`FilesSource` had no `.failures` attribute at all, so `cmds/ingest.py`'s
`getattr(src, "failures", ())` was always empty and every empty run took the
"(source reachable, nothing to ingest)" branch. A typo in `--path` produced output
byte-for-byte identical to a real directory holding no matching files, and exit 0.
"""

from __future__ import annotations

from contextlake.kb.sources.files import FilesSource


def test_a_missing_root_is_recorded_as_a_failure(tmp_path):
    src = FilesSource(path=str(tmp_path / "no_such_dir"))
    assert list(src.iter_documents()) == []
    assert len(src.failures) == 1
    target, reason = src.failures[0]
    assert "no_such_dir" in target
    assert "no such file" in reason.lower()


def test_a_real_but_empty_directory_is_NOT_a_failure(tmp_path):
    """The whole point is telling these two apart, so both directions are asserted."""
    (tmp_path / "empty").mkdir()
    src = FilesSource(path=str(tmp_path / "empty"))
    assert list(src.iter_documents()) == []
    assert src.failures == []


def test_a_directory_with_documents_still_yields_them(tmp_path):
    (tmp_path / "notes.md").write_text("# Billing\n\ncharge_card runs first.\n")
    src = FilesSource(path=str(tmp_path))
    docs = list(src.iter_documents())
    assert [d.id for d in docs] == ["notes.md"]
    assert src.failures == []


def test_a_single_file_path_still_works(tmp_path):
    f = tmp_path / "one.md"
    f.write_text("# One\n\nbody\n")
    src = FilesSource(path=str(f))
    assert [d.id for d in src.iter_documents()] == ["one.md"]
    assert src.failures == []


def test_failures_are_reset_between_runs(tmp_path):
    """A stale failure from a previous run would report a fixed path as still broken."""
    missing = tmp_path / "gone"
    src = FilesSource(path=str(missing))
    list(src.iter_documents())
    assert len(src.failures) == 1
    missing.mkdir()
    list(src.iter_documents())
    assert src.failures == []
