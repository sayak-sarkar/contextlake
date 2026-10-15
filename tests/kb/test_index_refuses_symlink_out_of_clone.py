"""No reader of a clone follows a symlink out of it, or into its `.git` directory.

A clone is untrusted, and a symlink is one commit away. The source walk read a symlinked
file, so a committed `leak.py` linked to a file outside the clone put that file into the
graph and every answer built on it. A link that stayed inside the clone but pointed at
`.git/config` did the same with the remote URL and any credentials in it, and the README,
steering and snippet readers accepted that link too (2026-10-15, probe B4 of the stability
campaign; the README readers had the outside-the-clone case, S01). A link to another file of
the clone is still read.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from contextlake.cli import main
from contextlake.kb.paths import inside_clone, read_repo_file
from contextlake.kb.store.sqlite_store import SqliteStore

OUTSIDE = "b4_canary_outside_the_clone"
DOTGIT = "B4DOTGITCANARYTOKEN"


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def _link(target, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create a symlink here")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   text=True)


@pytest.fixture
def layout(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "canary.py").write_text(f"def {OUTSIDE}():\n    return 1\n", encoding="utf-8")
    repo = tmp_path / "ws" / "repo"
    (repo / "lib").mkdir(parents=True)
    (repo / "inside.py").write_text("def b4_inside():\n    return 1\n", encoding="utf-8")
    (repo / "lib" / "real.py").write_text("def b4_in_lib():\n    return 2\n", encoding="utf-8")
    _git(repo, "init", "-q")
    # Credentials in the remote URL are what `.git/config` can carry.
    _git(repo, "config", "remote.origin.url", f"https://u:{DOTGIT}@example.invalid/b4.git")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n', encoding="utf-8")
    return tmp_path, outside, repo, cfg


def _index(cfg, repo, mode):
    target = ["--source", str(repo)] if mode == "source" else ["--workspace", str(repo.parent)]
    return _run(["kb", "index", "--config", str(cfg), *target])


def _store_holds(tmp_path, text: str) -> list[str]:
    """Every file of the store whose bytes contain ``text``: nodes, attrs, shards, history."""
    hits = []
    for p in (tmp_path / "kb").rglob("*"):
        if p.is_file() and text.encode() in p.read_bytes():
            hits.append(str(p.relative_to(tmp_path)))
    return hits


def _files_defining(tmp_path, name: str) -> set[str]:
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    try:
        return {n.file for n in store.nodes_by_name(name)}
    finally:
        store.close()


# --- the source walk --------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["source", "workspace"])
@pytest.mark.parametrize("leak", ["outside", "dotgit"])
def test_a_link_out_of_the_clone_or_into_dot_git_reaches_no_store_file(layout, capsys,
                                                                        mode, leak):
    tmp_path, outside, repo, cfg = layout
    canary = OUTSIDE if leak == "outside" else DOTGIT
    target = outside / "canary.py" if leak == "outside" else Path(".git") / "config"
    for name in ("leak.py", "leak.md", "README.md"):
        _link(target, repo / name)
    assert _index(cfg, repo, mode) == 0
    out = capsys.readouterr().out
    assert _store_holds(tmp_path, canary) == []
    assert _files_defining(tmp_path, "b4_inside") == {"inside.py"}   # the rest was indexed
    assert "skip leak.py: a symlink to a file outside the repository or in .git" in out


@pytest.mark.parametrize("mode", ["source", "workspace"])
def test_a_link_to_another_file_of_the_clone_is_indexed(layout, mode):
    tmp_path, _outside, repo, cfg = layout
    _link(Path("lib") / "real.py", repo / "alias.py")
    assert _index(cfg, repo, mode) == 0
    # From the link itself: `lib/real.py` defines the same function, so the name alone
    # would be found whether or not the link was read.
    assert _files_defining(tmp_path, "b4_in_lib") == {"lib/real.py", "alias.py"}


def test_the_same_file_copied_into_the_clone_is_indexed(layout):
    """The positive control: the canary is found when it is in the repository."""
    tmp_path, outside, repo, cfg = layout
    (repo / "copied.py").write_text((outside / "canary.py").read_text(encoding="utf-8"),
                                    encoding="utf-8")
    assert _index(cfg, repo, "source") == 0
    assert _store_holds(tmp_path, OUTSIDE)


def test_a_symlink_loop_is_skipped_and_the_index_completes(layout):
    tmp_path, _outside, repo, cfg = layout
    _link(repo / "loop_b.py", repo / "loop_a.py")
    _link(repo / "loop_a.py", repo / "loop_b.py")
    assert _index(cfg, repo, "source") == 0
    assert _files_defining(tmp_path, "b4_inside") == {"inside.py"}


# --- the other clone readers ------------------------------------------------------------

def test_inside_clone_refuses_dot_git_in_any_case(layout):
    _tmp_path, _outside, repo, _cfg = layout
    assert inside_clone(repo, repo / "inside.py")
    assert not inside_clone(repo, repo / ".git" / "config")
    assert not inside_clone(repo, repo / ".GIT" / "config")
    assert not inside_clone(repo, repo / ".." / "outside" / "canary.py")


def test_the_readme_reader_refuses_a_readme_linked_into_dot_git(layout):
    _tmp_path, _outside, repo, _cfg = layout
    _link(Path(".git") / "config", repo / "README.md")
    (repo / "README.rst").write_text("the real readme\n", encoding="utf-8")
    assert read_repo_file(repo, ("README.md", "README.rst")) == ("README.rst",
                                                                  "the real readme\n")


def test_the_snippet_reader_refuses_a_file_linked_into_dot_git(layout):
    from contextlake.kb.docs.snippets import SnippetReader
    from contextlake.kb.model import Repo
    from contextlake.kb.state import check_schema

    tmp_path, _outside, repo, _cfg = layout
    _link(Path(".git") / "config", repo / "driver.py")
    (repo / "plain.py").write_text("x = 1\n", encoding="utf-8")
    for f in (repo / "plain.py", repo / ".git" / "config"):
        os.utime(f, (1_000_000_000, 1_000_000_000))     # older than the index stamp
    store = SqliteStore(tmp_path / "snip.sqlite")
    try:
        check_schema(store)
        store.upsert_repo(Repo(id="team/b4", path=str(repo)))
        store.mark_indexed("team/b4", "headsha", "2021-01-01T00:00:00+00:00")
        reader = SnippetReader(store, "team/b4")
        assert reader.line("plain.py", 1) == "x = 1"            # the positive control
        assert reader.line("driver.py", 1) is None
    finally:
        store.close()
