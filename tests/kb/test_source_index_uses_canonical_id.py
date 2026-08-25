"""`kb index --source` must file a repo under its REMOTE id, like `--workspace` does.

Found by the stability campaign, 2026-08-25, against a live GitLab group.

`_index_workspace` has always used `resolve_repo_id`, which reads the `origin` remote. The
single-source path used `src.resolve().name`, so a clone of `gitlab.com/ns/proj` sitting in a
directory called `target` was stored as `target`.

That is not cosmetic. Every connector matches on the repo id, and the GitLab connector's
`_project_path` returns None for an id with no `/` in it, so it skips the repo and stores
nothing. Measured on one real repository with 100 open merge requests, same clone and same
store both times:

    indexed with --source     -> repo_id "target"              -> 0 external links
    indexed with --workspace  -> repo_id "gitlab.com/ns/proj"  -> 285 external links

The whole enrichment tier was silently empty for anything indexed the zero-config way, which
is the way the tool advertises: `cd my-repo && contextlake kb index`.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REMOTE = "https://gitlab.example.com/acme/widgets.git"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_repo(root: Path, *, remote: str | None) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", ".")
    (root / "a.py").write_text("def hello():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    if remote:
        _git(root, "remote", "add", "origin", remote)
    return root


def _index(src: Path, store: Path, *extra: str) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    cfg = store.parent / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store}"\n')
    r = subprocess.run(
        [sys.executable, "-m", "contextlake", "kb", "index", "--source", str(src),
         *extra, "--config", str(cfg), "--plain"],
        capture_output=True, text=True, timeout=300, cwd=REPO, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr


def _repo_ids(store: Path) -> list[str]:
    con = sqlite3.connect(store / "index.sqlite")
    try:
        return [row[0] for row in con.execute("select repo_id from repos")]
    finally:
        con.close()


@pytest.mark.slow
def test_source_index_files_under_the_remote_id(tmp_path):
    src = _make_repo(tmp_path / "wildly-different-dirname", remote=REMOTE)
    store = tmp_path / "s1" / "store"
    _index(src, store)
    ids = _repo_ids(store)
    assert ids == ["gitlab.example.com/acme/widgets"], (
        f"expected the canonical remote id, got {ids}. A directory-name id is invisible "
        "to every connector.")


@pytest.mark.slow
def test_repo_without_a_remote_gets_the_stable_fallback(tmp_path):
    src = _make_repo(tmp_path / "loner", remote=None)
    store = tmp_path / "s2" / "store"
    _index(src, store)
    ids = _repo_ids(store)
    assert len(ids) == 1
    # name@root-commit, so two clones of one history collide and two unrelated `api`s do not
    assert ids[0].startswith("loner@"), ids


@pytest.mark.slow
def test_explicit_repo_flag_still_wins(tmp_path):
    src = _make_repo(tmp_path / "named", remote=REMOTE)
    store = tmp_path / "s3" / "store"
    _index(src, store, "--repo", "chosen/by-hand")
    assert _repo_ids(store) == ["chosen/by-hand"]
