"""`kb index`'s parallel path used to keep every completed repository's Future
-- and the whole GraphShard cached inside it -- reachable for the entire run.

`futs` (src/contextlake/kb/cmds/index.py) is a dict keyed by `Future`. Calling
`fut.result()` does not clear the future's cached result, so nothing released
a repository once it was done with unless the dict entry itself was dropped. A
prior investigation (.superpowers/sdd/kb-index-memory-investigation.md) proved
this by A/B: the same 45 repositories through the same pool at the same worker
count retained 2,130 MB with the dict entries kept versus 95 MB released --
22x apart on identical work, growing with repos done rather than concurrency.

The second test in this file covers a related guard added in the same change:
a repository whose parsed shard is implausibly large is skipped rather than
persisted, so one pathological repository cannot take the whole run down.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import subprocess

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                   capture_output=True, text=True)


def _git_repo(path, body="def f():\n    return 1\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "m.py").write_text(body)
    _git(["init", "-q", "-b", "main"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "c"], path)


def _kb(tmp_path):
    """A config naming this test's own store, never the user's real one."""
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    return cfg


def _args(*argv: str):
    from contextlake import cli

    return cli.build_parser().parse_args(argv)


# --- future release ---------------------------------------------------------

def test_completed_futures_are_released_as_repos_are_persisted(tmp_path, monkeypatch):
    """Spies on the exact dict `_index_workspace` iterates over, recording its
    size right before each completed future is handed to the loop body.

    If the loop releases each future once it has read `(repo_id, path, head)`
    off it (`del futs[fut]`), the recorded size strictly decreases across the
    run: with 3 repos, [3, 2, 1]. If that release is missing, the dict never
    shrinks and every recorded size is 3. This fails if the `del` is removed,
    which is the point: it proves the mechanism, not just a memory number.
    """
    from contextlake.kb.commands import cmd_index

    ws = tmp_path / "ws"
    for name in ("one", "two", "three"):
        _git_repo(ws / name)
    cfg = _kb(tmp_path)

    sizes: list[int] = []
    real_as_completed = cf.as_completed

    def spying_as_completed(fs, timeout=None):
        for fut in real_as_completed(fs, timeout=timeout):
            sizes.append(len(fs))
            yield fut

    monkeypatch.setattr(cf, "as_completed", spying_as_completed)

    rc = cmd_index(_args("kb", "index", "--config", str(cfg),
                         "--workspace", str(ws), "--workers", "2"))

    assert rc == 0
    assert len(sizes) == 3, sizes
    assert sizes == [3, 2, 1], (
        "the retained-future dict did not shrink as repos completed -- "
        f"got {sizes}, expected a strictly decreasing [3, 2, 1]"
    )


# --- memory-budget guard -----------------------------------------------------

def test_a_shard_over_the_item_budget_is_skipped_not_persisted(tmp_path, monkeypatch, gls_logs):
    """One repository's shard can be too large to persist safely on its own --
    the investigation measured a real outlier that needed more than 9.3 GB in
    a single worker and never finished. Rather than build a shard that size in
    a test, the budget constant is lowered to a value one ordinary repo's
    shard already exceeds, so the guard trips deterministically and cheaply.
    """
    from contextlake.kb import parse as kb_parse
    from contextlake.kb.cmds import index as index_module
    from contextlake.kb.commands import cmd_index
    from contextlake.kb.store.shards import shard_path

    ws = tmp_path / "ws"
    _git_repo(ws / "huge")
    cfg = _kb(tmp_path)

    real_index_repo_dir = kb_parse.index_repo_dir
    monkeypatch.setattr(index_module, "_SHARD_ITEM_BUDGET", 1)

    calls = []

    def spying_index_repo_dir(*args, **kwargs):
        shard = real_index_repo_dir(*args, **kwargs)
        calls.append(shard.repo)
        return shard

    monkeypatch.setattr(kb_parse, "index_repo_dir", spying_index_repo_dir)

    rc = cmd_index(_args("kb", "index", "--config", str(cfg),
                         "--workspace", str(ws), "--workers", "1"))

    assert calls, "the guard must not stop the repository from being parsed at all"
    assert rc == 1, "a run that skipped a repo on the guard must not read as clean"
    assert "over budget" in gls_logs.text
    assert "memory-budget guard" in gls_logs.text

    store_dir = str(tmp_path / "kb")
    repo_id = calls[0]
    assert not shard_path(store_dir, repo_id).exists(), (
        "an over-budget shard must not be written to disk"
    )
