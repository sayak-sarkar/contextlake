"""`kb index` records the branch each repo was indexed from.

`list_repos` promises "the branch" per repo and the dashboard shows it, but no writer
ever set the repo row's `default_branch`, so every repo showed none. Both index paths
(`--workspace`, `--source`) now record the checked-out branch; a detached HEAD records
none rather than a guess.
"""

from __future__ import annotations

import os
import subprocess

from contextlake.kb.store.sqlite_store import SqliteStore

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _git_repo(path, branch):
    path.mkdir(parents=True, exist_ok=True)
    (path / "m.py").write_text("def f():\n    return 1\n")
    _git(["init", "-q", "-b", branch], path)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "c"], path)


def _args(*argv: str):
    from contextlake import cli

    return cli.build_parser().parse_args(argv)


def _branches(store_dir):
    s = SqliteStore(store_dir / "index.sqlite")
    try:
        return {r.path.rsplit("/", 1)[-1]: r.default_branch for r in s.list_repos()}
    finally:
        s.close()


def test_workspace_index_records_each_repo_branch(tmp_path):
    from contextlake.kb.commands import cmd_index

    ws = tmp_path / "ws"
    _git_repo(ws / "trunkrepo", "trunk")
    _git_repo(ws / "detached", "main")
    _git(["checkout", "-q", "--detach"], ws / "detached")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')

    assert cmd_index(_args("kb", "index", "--config", str(cfg), "--workspace", str(ws),
                           "--workers", "1")) == 0
    assert _branches(tmp_path / "kb") == {"trunkrepo": "trunk", "detached": None}


def test_source_index_records_the_branch(tmp_path):
    from contextlake.kb.commands import cmd_index

    repo = tmp_path / "solo"
    _git_repo(repo, "release-2")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')

    assert cmd_index(_args("kb", "index", "--config", str(cfg), "--source", str(repo))) == 0
    assert _branches(tmp_path / "kb") == {"solo": "release-2"}
