"""`kb index` must refuse a repo whose id names no path of its own, and carry on.

The id is the normalized `origin` URL, so a clone's owner picks it. Three origins matter:

- `https://example.com/team/../other` gives `example.com/team/../other`, which `shard_path`
  resolved to repo `other`'s shard. Indexing it replaced `other`'s graph with its own.
- `https://evil.example/../..` gives `evil.example/../..`. Its history snapshot went to
  `history/evil.example/../../<commit>.json`: the store root.
- `file:///dir` gives the absolute id `/dir`. `write_shard` already refused it, but the repo
  row was registered first and stayed behind.

Each test builds scratch repos under `tmp_path`, runs the real CLI with HOME moved there, and
reads the store it wrote. The `--workspace` path reports a repo that fails to persist and
continues. The single-path `kb index PATH` had no handler, so the refusal would have escaped
as a traceback.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_repo(root: Path, origin: str, fn: str) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", ".")
    (root / "a.py").write_text(f"def {fn}():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    _git(root, "remote", "add", "origin", origin)
    return root


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    store = tmp_path / "work" / "store"
    store.mkdir(parents=True)
    cfg = tmp_path / "work" / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store.as_posix()}"\n\n[embeddings]\nenabled = false\n')
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()

    def index(*args: str):
        return subprocess.run(
            [sys.executable, "-m", "contextlake", "--config", str(cfg), "kb", "index", *args,
             "--no-docs", "--plain"],
            capture_output=True, text=True, timeout=300, cwd=REPO, stdin=subprocess.DEVNULL)

    class W:
        pass

    w = W()
    w.tmp, w.ws, w.store, w.index = tmp_path, ws, store, index
    return w


def _repo_ids(store: Path) -> list[str]:
    con = sqlite3.connect(store / "index.sqlite")
    try:
        return sorted(row[0] for row in con.execute("select repo_id from repos"))
    finally:
        con.close()


def _function_names(shard: Path) -> list[str]:
    data = json.loads(shard.read_text())
    return sorted(n["name"] for n in data["nodes"] if n["kind"] == "function")


@pytest.mark.slow
def test_a_workspace_run_refuses_the_hostile_repos_by_name_and_indexes_the_rest(world):
    _make_repo(world.ws / "other", "https://example.com/other", "real_other")
    _make_repo(world.ws / "fine", "https://example.com/fine", "real_fine")
    # `other` is indexed in a run of its own first. Indexed in the same run as the alias, the
    # final content would depend on which of the two the walk reaches last.
    assert world.index("--workspace", str(world.ws), "--workers", "1").returncode == 0
    _make_repo(world.ws / "alias", "https://example.com/team/../other", "from_alias")
    _make_repo(world.ws / "dotdot", "https://evil.example/../..", "from_dotdot")

    r = world.index("--workspace", str(world.ws), "--workers", "1")
    out = r.stdout + r.stderr

    assert "Traceback" not in out, out
    assert r.returncode == 1, out                       # a refused repo is not a clean run
    # Named, one line each, so the reader knows which repos were not indexed.
    assert "example.com/team/../other" in out and "evil.example/../.." in out, out
    assert "2 failed" in out, out
    # The others carry on.
    assert _function_names(world.store / "graph" / "example.com" / "fine.json") == ["real_fine"]
    # `other`'s graph is its own, not the alias's.
    shard = json.loads((world.store / "graph" / "example.com" / "other.json").read_text())
    assert shard["repo"] == "example.com/other"
    assert _function_names(world.store / "graph" / "example.com" / "other.json") == ["real_other"]
    # Nothing was written outside graph/ and history/.
    stray = [p.name for p in world.store.iterdir() if p.is_file() and p.suffix == ".json"]
    assert stray == []
    assert sorted(p.name for p in world.store.parent.iterdir()) == ["kb.toml", "store"]
    # No row was registered for a repo that was refused.
    assert _repo_ids(world.store) == ["example.com/fine", "example.com/other"]


@pytest.mark.slow
def test_an_absolute_id_leaves_no_repo_row_and_writes_nothing(world):
    target = world.tmp / "victim_dir"           # the id is lowercased, so keep the path lowercase
    target.mkdir()
    _make_repo(world.ws / "abs", target.as_uri(), "from_abs")

    r = world.index("--workspace", str(world.ws), "--workers", "1")

    assert r.returncode == 1, r.stdout + r.stderr
    assert str(target).lower() in (r.stdout + r.stderr).lower()
    assert list(target.iterdir()) == []
    assert _repo_ids(world.store) == []


@pytest.mark.slow
def test_a_single_path_index_refuses_without_a_traceback(world):
    other = _make_repo(world.ws / "other", "https://example.com/other", "real_other")
    alias = _make_repo(world.ws / "alias", "https://example.com/team/../other", "from_alias")
    assert world.index(str(other)).returncode == 0
    before = (world.store / "graph" / "example.com" / "other.json").read_text()

    r = world.index(str(alias))
    out = r.stdout + r.stderr

    assert "Traceback" not in out, out
    assert r.returncode == 1, out
    assert "example.com/team/../other" in out, out
    assert "refused, not indexed" in out, out            # the call site's own line
    assert (world.store / "graph" / "example.com" / "other.json").read_text() == before
    assert _repo_ids(world.store) == ["example.com/other"]


@pytest.mark.slow
def test_an_explicit_repo_flag_is_checked_too(world):
    """`--repo` is the other way to pick an id."""
    other = _make_repo(world.ws / "other", "https://example.com/other", "real_other")
    assert world.index(str(other)).returncode == 0
    before = (world.store / "graph" / "example.com" / "other.json").read_text()
    fine = _make_repo(world.ws / "fine", "https://example.com/fine", "real_fine")

    r = world.index(str(fine), "--repo", "x/../example.com/other")

    assert "Traceback" not in r.stdout + r.stderr
    assert r.returncode == 1
    assert (world.store / "graph" / "example.com" / "other.json").read_text() == before


def test_migration_does_not_delete_the_store_for_a_stale_dot_id(tmp_path):
    """`migrate_stale_repo_ids` clears a stale row's shard and then removes its history
    directory. For the stored id `evil.example/../..` that directory was
    `history/evil.example/../..`: the store root. The store here sits at `tmp_path/a/kb`."""
    from contextlake.kb.model import Repo
    from contextlake.kb.repo_migrate import migrate_stale_repo_ids
    from contextlake.kb.store.sqlite_store import SqliteStore

    clone = _make_repo(tmp_path / "clone", "https://example.com/acme/widgets", "f")
    store_dir = tmp_path / "a" / "kb"
    (store_dir / "history" / "evil.example").mkdir(parents=True)
    (store_dir / "keep.txt").write_text("keep")
    (store_dir.parent / "precious.txt").write_text("precious")
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        stale = "evil.example/../.."
        store.upsert_repo(Repo(id=stale, path=str(clone)))
        result = migrate_stale_repo_ids(store, store_dir)
        assert result.cleared == [(stale, "example.com/acme/widgets")]
    finally:
        store.close()

    assert (store_dir / "keep.txt").read_text() == "keep"
    assert (store_dir / "index.sqlite").exists()
    assert (store_dir.parent / "precious.txt").read_text() == "precious"
