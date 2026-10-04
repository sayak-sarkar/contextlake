"""W02: a relative path built with the OS separator broke ADRs and nested manifests.

On Windows the walker built ``rel`` with ``str(path.relative_to(root))``, which
gives ``docs\\adr\\0001-x.md``. Everything downstream splits on ``/``:

- ``is_adr_path`` found no ``adr`` directory, so no ADR node was made.
- ``parse_manifest`` took the whole string as the file name, so a manifest below
  the repository root gave no nodes and no dependency edges.
- every nested file got backslashes in its ``file`` attribute and qualified name, and
  each symbol keyed on its file (Python, JavaScript and others) got a different id than
  on Linux. File node ids already matched, because ``make_id`` slugs the separators.

CI runs on Linux only, so these tests simulate the one thing that differs: the
path class answers ``relative_to`` the way ``WindowsPath`` does. On a real Windows
machine they run unpatched.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import pytest

from contextlake.kb import parse

_PosixPath = type(Path())


class _WindowsRelPath(_PosixPath):
    """A path that reads the real disk but reports relative paths like Windows."""

    def relative_to(self, *args, **kwargs):
        posix = str(super().relative_to(*args, **kwargs))
        return PureWindowsPath(posix.replace("/", "\\"))


class _WindowsPathModule:
    """``os.path``, except ``relpath`` answers with backslashes as ``ntpath`` does."""

    def __getattr__(self, name):
        return getattr(os.path, name)

    @staticmethod
    def relpath(path, start=None):
        return os.path.relpath(path, start).replace("/", "\\")


class _WindowsOs:
    """``os`` as ``parse`` sees it on Windows: a backslash separator and ``relpath``."""

    sep = "\\"
    path = _WindowsPathModule()

    def __getattr__(self, name):
        return getattr(os, name)


def _simulate_windows(monkeypatch):
    """Make ``parse`` build relative paths the way Windows does."""
    if os.name != "nt":
        monkeypatch.setattr(parse, "Path", _WindowsRelPath)
        monkeypatch.setattr(parse, "os", _WindowsOs())


@pytest.fixture
def windows_paths(monkeypatch):
    _simulate_windows(monkeypatch)


def _make_repo(root: Path) -> Path:
    files = {
        "docs/adr/0001-use-postgres.md": "# Use Postgres\n\nWe store orders in it.\n",
        "package.json": '{"name": "root-pkg"}',
        "services/api/package.json":
            '{"name": "svc-api", "dependencies": {"left-pad": "^1.0.0"}}',
        "services/worker/pyproject.toml":
            '[project]\nname = "svc-worker"\ndependencies = ["requests>=2"]\n',
        "services/api/app.py": "def handler():\n    return 1\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _shape(shard):
    nodes = [(n.id, n.kind, n.name, n.file, n.qualified_name) for n in shard.nodes]
    edges = [(e.src, e.dst, e.relation) for e in shard.edges]
    return nodes, edges


def test_a_windows_style_walk_indexes_adrs_and_nested_manifests(tmp_path, windows_paths):
    repo = _make_repo(tmp_path / "repo")
    shard = parse.index_repo_dir(str(repo), "r")

    kinds = {n.kind for n in shard.nodes}
    assert "adr" in kinds, "the ADR under docs/adr was not indexed"
    packages = {n.name for n in shard.nodes if n.kind == "package"}
    assert {"svc-api", "left-pad", "svc-worker", "requests"} <= packages, packages
    assert any(e.relation == "depends_on" for e in shard.edges)


def test_a_windows_style_walk_gives_the_same_ids_as_linux(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    baseline = _shape(parse.index_repo_dir(str(repo), "r"))

    _simulate_windows(monkeypatch)
    got = _shape(parse.index_repo_dir(str(repo), "r"))

    assert got == baseline
    assert not any("\\" in text for node in got[0] for text in node[2:] if text)


def test_the_walker_yields_slash_separated_rel_paths(tmp_path, windows_paths):
    repo = _make_repo(tmp_path / "repo")
    allowed, names, hcl, sql = parse._source_filter(None)
    rels = [sf.rel for sf in parse._walk_source_files(
        Path(repo), allowed_exts=allowed, allowed_names=names, index_hcl=hcl,
        index_sql=sql, max_file_bytes=5_000_000, skip_generated=True,
        counts=parse.WalkCounts())]

    assert "services/api/package.json" in rels
    assert "docs/adr/0001-use-postgres.md" in rels
    assert not [r for r in rels if "\\" in r]


def test_the_cost_estimate_counts_an_adr_under_windows_paths(tmp_path, windows_paths):
    repo = tmp_path / "repo"
    adr = repo / "docs" / "adr" / "0001-x.md"
    adr.parent.mkdir(parents=True)
    adr.write_text("# X\n" + "x" * 1000, encoding="utf-8")
    allowed, names, hcl, sql = parse._source_filter(None)

    _estimate, by_kind = parse.estimate_repo_cost(
        repo, allowed_exts=allowed, allowed_names=names, index_hcl=hcl,
        index_sql=sql, max_file_bytes=5_000_000)

    assert parse._ADR in by_kind, by_kind


def test_the_bundling_count_sees_an_adr_under_windows_paths(tmp_path, windows_paths):
    repo = tmp_path / "repo"
    adr = repo / "docs" / "adr" / "0001-x.md"
    adr.parent.mkdir(parents=True)
    adr.write_text("# X\n", encoding="utf-8")

    assert parse.count_indexable_files(repo) == 1
