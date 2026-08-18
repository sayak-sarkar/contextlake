"""`kb wiki <repo>` must write that repo's page and no other.

A one-day-old regression, found independently by two reviewers. `_structural_stage` took
`args` and never read it, so it looped over every repository in the store. Two consequences,
one root cause:

- `kb wiki <a-repo-that-exists>` rewrote EVERY repository's structural page, not just that
  one. The scoping was not merely loose, it was absent.
- `kb wiki <a-repo-that-does-not-exist>` regenerated everything and exited 0, where
  `kb docs` on identical input reports no match and exits 1. Two commands taking the same
  argument gave opposite verdicts, so a script could trust neither.

The local-first default is what made it invisible: with no LLM configured `cmd_wiki` returns
right after this stage, so the correctly-filtered `_connect_targets` call further down was
never reached at all.
"""

from __future__ import annotations

import os
import subprocess
from argparse import Namespace

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _repo(path, body):
    path.mkdir(parents=True, exist_ok=True)
    (path / "m.py").write_text(body)
    for args in (["init", "-q", "."], ["add", "-A"], ["commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=path, env=_ENV, check=True, capture_output=True)


def _two_repo_store(tmp_path):
    from contextlake.kb.cmds.index import cmd_index

    ws = tmp_path / "ws"
    _repo(ws / "repoA", "def a():\n    return 1\n")
    _repo(ws / "repoB", "def b():\n    return 2\n")
    store_dir = tmp_path / "store"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    rc = cmd_index(Namespace(config=str(cfg), workspace=str(ws), source=None, args=[],
                             repos=None, repo=None, force=False, bundle=False,
                             watch=False, interval=0, path=None))
    assert rc == 0
    return store_dir, cfg


def _wiki(cfg, *positional):
    from contextlake.kb.cmds.wiki import cmd_wiki
    return cmd_wiki(Namespace(config=str(cfg), args=list(positional), namespace=None,
                              namespaces=False, llm=None, workspace=None, source=None,
                              repos=None, repo=None, force=False, anonymize=None,
                              max_symbols=None))


def _pages(store_dir):
    return sorted((store_dir / "wiki").glob("*.md"))


def test_a_filter_matching_nothing_exits_1_and_writes_nothing(tmp_path):
    """`kb docs` gives this verdict on the same input; the two must agree."""
    store_dir, cfg = _two_repo_store(tmp_path)
    assert _wiki(cfg) == 0
    before = {p: p.read_bytes() for p in _pages(store_dir)}
    assert len(before) == 2, "fixture must have two pages, or the guard proves nothing"

    assert _wiki(cfg, "no-such-repo") == 1
    assert {p: p.read_bytes() for p in _pages(store_dir)} == before, (
        "a filter matching nothing rewrote pages anyway")


def test_a_matching_filter_touches_only_that_repository(tmp_path):
    """The half a non-matching id cannot catch.

    A no-match filter could be handled by an early return while the loop stayed unfiltered,
    and only this test would notice.

    The observable is which page is RECREATED after both are deleted, not which file's bytes
    changed: `kb wiki` skips a page that is already current, so a "did the mtime move" check
    would fail against correct behaviour and prove nothing about scoping.
    """
    store_dir, cfg = _two_repo_store(tmp_path)
    assert _wiki(cfg) == 0
    pages = _pages(store_dir)
    assert len(pages) == 2, "fixture must have two pages, or the guard proves nothing"
    a_name = next(p.name for p in pages if p.name.startswith("repoA"))
    b_name = next(p.name for p in pages if p.name.startswith("repoB"))
    stem = a_name[: -len(".md")]
    for p in pages:
        p.unlink()

    assert _wiki(cfg, stem) == 0
    after = {p.name for p in _pages(store_dir)}
    assert a_name in after, "the requested repo's page was not written"
    assert b_name not in after, "an unrequested repo's page was written anyway"


def test_a_partly_matching_filter_is_rejected_rather_than_served(tmp_path):
    """`kb wiki real-id typo-id` must not quietly serve half of what was asked.

    Checking only "did ANY id match" let a mixed request through: it filtered to the real
    id, wrote one page, and exited 0 without ever naming the id it could not find. That is a
    partial run reported as a complete one -- found by an adversarial review of the very
    commit that fixed the total-miss case.
    """
    store_dir, cfg = _two_repo_store(tmp_path)
    assert _wiki(cfg) == 0
    pages = _pages(store_dir)
    real = next(p.name[: -len(".md")] for p in pages if p.name.startswith("repoA"))
    before = {p: p.read_bytes() for p in pages}

    assert _wiki(cfg, real, "no-such-repo") == 1
    assert {p: p.read_bytes() for p in _pages(store_dir)} == before, (
        "a partly-matching filter wrote pages anyway")


def test_no_filter_still_regenerates_everything(tmp_path):
    """The behaviour the fix must not cost: bare `kb wiki` covers the whole store."""
    store_dir, cfg = _two_repo_store(tmp_path)
    assert _wiki(cfg) == 0
    names = {p.name for p in _pages(store_dir)}
    assert len(names) == 2
    for p in _pages(store_dir):
        p.unlink()

    assert _wiki(cfg) == 0
    assert {p.name for p in _pages(store_dir)} == names
