"""INV-1 enforcement: contextlake must NEVER write inside a synced repo working tree.

All generated artifacts belong under the store (`~/.contextlake/kb` by default), never
inside the mirrored repos. This drives the generating commands over a temp 2-repo mirror
and asserts each repo's working tree is byte-identical before and after — the durable
guard against the default-pollution class the product plan calls out.
"""

import hashlib
import subprocess
from pathlib import Path

import pytest

from contextlake.cli import main


def _git_repo(path: Path, fname: str, content: str) -> None:
    path.mkdir(parents=True)
    (path / fname).write_text(content)
    env = ["-c", "user.email=t@t.dev", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", *env, "add", "."], cwd=path, check=True)
    subprocess.run(["git", *env, "commit", "-qm", "init"], cwd=path, check=True)


def _snapshot(root: Path) -> dict:
    """Map every working-tree file (excluding .git internals) to its content hash."""
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            snap[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def _run(argv) -> int:
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def test_no_command_pollutes_synced_repo_trees(tmp_path):
    mirror = tmp_path / "mirror"
    repo_a = mirror / "team" / "a"
    repo_b = mirror / "team" / "b"
    _git_repo(repo_a, "a.py", "def f():\n    return g()\n\n\ndef g():\n    return 1\n")
    _git_repo(repo_b, "b.py", "class C:\n    def m(self):\n        return 2\n")
    before = {repo_a: _snapshot(repo_a), repo_b: _snapshot(repo_b)}

    # the store (everything generated) lives OUTSIDE the mirror
    store_dir = tmp_path / "store"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')

    # drive every generating verb that walks the mirror
    assert _run(["index", "--config", str(cfg), "--workspace", str(mirror)]) == 0
    assert _run(["graph", "--config", str(cfg), "--overview"]) == 0
    assert _run(["query", "f", "--config", str(cfg)]) == 0
    # steering at the mirror ROOT is the deliberate carve-out (root is not a repo);
    # it must still never leak into an individual repo tree below it
    assert _run(["steer", "--config", str(cfg), "--out", str(mirror)]) == 0

    after = {repo_a: _snapshot(repo_a), repo_b: _snapshot(repo_b)}
    assert after == before, "a command wrote inside a synced repo tree — INV-1 violated"

    # the store materialised, and it is not nested inside the mirror
    assert store_dir.exists()
    assert mirror not in store_dir.resolve().parents
