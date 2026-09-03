"""The single-repo path writes documents too, under the CANONICAL repo id.

`--workspace` is not the hot path. `kb hook` installs `kb index "<path>" --repo "<id>"`
(`kb/git_hook.py:74`) and `kb refresh` spawns `kb index --source <repo-id>`
(`cmds/refresh.py:73`); both land on the single-source branch. Testing only the workspace
path would leave the path that runs after every commit unproven.

The id matters as much as the count. `_connect_targets` (`cmds/_common.py:148-150`) maps a
`--source <dir>` to `Path(source).name`, not to the canonical id `_cmd_index_once` derives
from the origin remote, and that same non-canonical-id defect once made every connector
match nothing. So the fixture gives the clone a directory name that differs from its remote
id, and the assertion is on the FILENAME rather than on "one page exists".

Precondition: HOME is monkeypatched and the config names a store under `tmp_path`.
"""

from __future__ import annotations

import os
import subprocess

from contextlake import cli
from contextlake.kb.visualize import repo_slug

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

REMOTE = "https://gitlab.example.invalid/acme/widgets.git"
CANONICAL = "gitlab.example.invalid/acme/widgets"
SOURCE = "def helper(x):\n    return x + 1\n\n\ndef run():\n    return helper(1)\n"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                   capture_output=True, text=True)


def _clone(path):
    """A repo whose DIRECTORY name ('target') is not its canonical id."""
    path.mkdir(parents=True)
    (path / "m.py").write_text(SOURCE, encoding="utf-8")
    _git(["init", "-q", "-b", "main"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "c1"], path)
    _git(["remote", "add", "origin", REMOTE], path)


def _index(cfg, src):
    from contextlake.kb.commands import cmd_index

    return cmd_index(cli.build_parser().parse_args(
        ["kb", "index", "--config", str(cfg), "--source", str(src)]))


def test_the_source_path_writes_documents_under_the_canonical_repo_id(
        tmp_path, monkeypatch, gls_logs):
    monkeypatch.setenv("HOME", str(tmp_path))
    src = tmp_path / "target"
    _clone(src)
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n[embeddings]\nenabled = false\n')

    assert _index(cfg, src) == 0
    api = sorted(p.name for p in (store_dir / "docs" / "api").glob("*.md"))
    design = sorted(p.name for p in (store_dir / "docs" / "design").glob("*.md"))
    assert api == [repo_slug(CANONICAL) + ".md"]
    assert design == [repo_slug(CANONICAL) + ".md"]
    assert "target.md" not in api, "filed under the directory name, not the remote id"

    # Second run, no new commit: the source path's own skip, proved separately from the
    # workspace path's rather than assumed alike.
    page = store_dir / "docs" / "api" / (repo_slug(CANONICAL) + ".md")
    marked = page.read_text(encoding="utf-8") + "\n<!-- SENTINEL -->\n"
    page.write_text(marked, encoding="utf-8")

    gls_logs.clear()
    assert _index(cfg, src) == 0
    assert page.read_text(encoding="utf-8") == marked
    assert "documents already describe" in gls_logs.text
    assert "0 written, 1 unchanged, documents failed for 0 repo(s)" in gls_logs.text
    # And the unchanged gate no longer claims there was nothing to do.
    assert "the documents were checked" in gls_logs.text


def _pages(store_dir):
    api = store_dir / "docs" / "api"
    design = store_dir / "docs" / "design"
    return (len(list(api.glob("*.md"))) if api.is_dir() else 0,
            len(list(design.glob("*.md"))) if design.is_dir() else 0)


def _store_write_that_fails(monkeypatch):
    """Let the real store write run, then report it as failed.

    Reporting failure without doing the write is the shape that cannot discriminate: the
    shard never lands, `read_shard` returns None, and the docs step writes 0 pages on
    unmodified code too. Doing the write and returning non-zero is the only version where
    unguarded code writes both pages and guarded code writes neither.
    """
    from contextlake.kb.cmds import index as index_cmd

    real = index_cmd._store_and_index

    def _failing(*a, **k):
        real(*a, **k)
        return 1

    monkeypatch.setattr(index_cmd, "_store_and_index", _failing)


def test_force_on_the_source_path_rewrites_a_current_document(
        tmp_path, monkeypatch, gls_logs):
    """`--force` is the flag that means redo it, on the path `kb hook` runs.

    The source path is what a post-commit hook and `kb refresh` land on, so a reader who
    reaches for `--force` after a page looks wrong reaches it here. The two counts are the
    test: 0 written on a plain re-run, 1 written with the flag.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    src = tmp_path / "target"
    _clone(src)
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n[embeddings]\nenabled = false\n')

    assert _index(cfg, src) == 0
    page = store_dir / "docs" / "api" / (repo_slug(CANONICAL) + ".md")
    marked = page.read_text(encoding="utf-8") + "\n<!-- SENTINEL -->\n"
    page.write_text(marked, encoding="utf-8")

    gls_logs.clear()
    assert _index(cfg, src) == 0
    assert page.read_text(encoding="utf-8") == marked
    assert "0 written, 1 unchanged, documents failed for 0 repo(s)" in gls_logs.text

    from contextlake.kb.commands import cmd_index

    gls_logs.clear()
    assert cmd_index(cli.build_parser().parse_args(
        ["kb", "index", "--config", str(cfg), "--source", str(src), "--force"])) == 0
    assert "1 written, 0 unchanged, documents failed for 0 repo(s)" in gls_logs.text
    assert "SENTINEL" not in page.read_text(encoding="utf-8")


def test_a_failed_store_write_hands_no_repo_to_the_docs_step(tmp_path, monkeypatch):
    """The source path states the invariant the workspace path states in `_persist`.

    A document is rendered from the shard the store write was meant to leave on disk, so a
    repo whose write failed has no shard this run can claim to describe. `_write_docs_for`
    ran here whatever the return code said, which is the two paths disagreeing about the
    same rule -- the divergence that once made every connector match nothing.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    src = tmp_path / "target"
    _clone(src)
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n[embeddings]\nenabled = false\n')

    _store_write_that_fails(monkeypatch)
    assert _index(cfg, src) == 1
    assert _pages(store_dir) == (0, 0)


def test_a_failed_store_write_on_the_shard_json_path_hands_no_repo_to_the_docs_step(
        tmp_path, monkeypatch):
    """The same rule on `--source <graph-shard.json>`, which no test reached before.

    Three branches write documents on this command and each one needs its own row: a guard
    added to two of three reads as done and leaves the third writing pages for a repo the
    store refused.
    """
    from contextlake.kb.commands import cmd_index
    from contextlake.kb.store.shards import shard_path

    monkeypatch.setenv("HOME", str(tmp_path))
    src = tmp_path / "target"
    _clone(src)
    first = tmp_path / "kb1"
    cfg1 = tmp_path / "kb1.toml"
    cfg1.write_text(f'[kb]\nstore_dir = "{first}"\n[embeddings]\nenabled = false\n')
    assert _index(cfg1, src) == 0

    shard_json = tmp_path / "shard.json"
    shard_json.write_text(shard_path(first, CANONICAL).read_text(encoding="utf-8"),
                          encoding="utf-8")

    second = tmp_path / "kb2"
    cfg2 = tmp_path / "kb2.toml"
    cfg2.write_text(f'[kb]\nstore_dir = "{second}"\n[embeddings]\nenabled = false\n')

    _store_write_that_fails(monkeypatch)
    assert cmd_index(cli.build_parser().parse_args(
        ["kb", "index", "--config", str(cfg2), "--source", str(shard_json)])) == 1
    assert _pages(second) == (0, 0)
