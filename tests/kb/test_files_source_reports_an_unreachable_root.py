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


def test_a_tilde_path_is_read_from_the_home_directory(tmp_path, monkeypatch):
    """The documented example is `path = "~/notes"`. `Path("~/notes")` is a relative path
    whose first part is a directory named `~`, so ingest failed with "could not read
    ~/notes -- no such file or directory" for a directory that exists."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "runbook.md").write_text("# Runbook\n\nrestart the gateway\n")

    src = FilesSource(path="~/notes")
    docs = list(src.iter_documents())

    assert [d.id for d in docs] == ["runbook.md"]
    assert src.failures == [], src.failures


def test_a_text_file_that_is_skipped_is_named(tmp_path, gls_logs):
    """A text file over `max_bytes`, or not UTF-8, was skipped with no message: its
    document left the index and the run printed "Ingest complete". The PDF and image
    paths already name what they skip. Neither is a failure, because `include` can be
    widened to globs that match binaries."""
    (tmp_path / "ok.md").write_text("# Ok\n\nbody\n")
    (tmp_path / "huge.md").write_text("# Huge\n\n" + "x" * 500)
    (tmp_path / "latin1.md").write_bytes("# Caf\xe9\n".encode("latin-1"))

    src = FilesSource(path=str(tmp_path), max_bytes=100)
    assert [d.id for d in src.iter_documents()] == ["ok.md"]

    assert src.failures == [], "a skipped file is not a failed run"
    text = gls_logs.text
    assert "over max_bytes (100)" in text and "huge.md" in text
    assert "not valid UTF-8" in text and "latin1.md" in text
    assert "ok.md" not in text


def test_a_text_file_that_cannot_be_opened_is_a_failure(tmp_path, monkeypatch):
    """A file that is there and cannot be opened is a miss, so `kb ingest` keeps the
    document an earlier run stored for it. One that vanished mid-run is not."""
    from pathlib import Path

    (tmp_path / "ok.md").write_text("# Ok\n\nbody\n")
    (tmp_path / "locked.md").write_text("# Locked\n\nbody\n")
    (tmp_path / "gone.md").write_text("# Gone\n\nbody\n")
    real = Path.read_text

    def read_text(self, *a, **k):
        if self.name == "locked.md":
            raise PermissionError(13, "Permission denied")
        if self.name == "gone.md":
            raise FileNotFoundError(2, "No such file or directory")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    src = FilesSource(path=str(tmp_path))

    assert [d.id for d in src.iter_documents()] == ["ok.md"]
    assert [t for t, _ in src.failures] == [str(tmp_path / "locked.md")]
    assert "PermissionError" in src.failures[0][1]
