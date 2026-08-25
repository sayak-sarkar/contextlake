"""`kb index <dir>` when the directory is not itself a repo but holds some.

Three defects lived in the same four lines: the scan was one level deep (so the
count that conveys the damage was 95% low), the remedy it printed hardcoded
`--workspace .` regardless of the path actually named, and it was a warning that
indexing then blew straight past -- leaving one real store 63% duplicate with no
repair path at the time.
"""

import logging
import os
import subprocess
from argparse import Namespace

import pytest

from contextlake.kb.cmds.index import (
    _LOOSE_FILES_TRIVIAL,
    _bundle_shape,
    _depth_phrase,
    _nested_repo_dirs,
    _typed_path,
)

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture
def logs():
    logger = logging.getLogger("contextlake")
    saved = logger.handlers[:]
    logger.handlers.clear()
    messages: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield messages
    logger.handlers[:] = saved


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _git_repo(path, body="def foo():\n    return 1\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "m.py").write_text(body)
    _git(["init", "-q", "-b", "main"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "c"], path)
    return _git(["rev-parse", "HEAD"], path)


def _fleet(tmp_path):
    """The real layout that reported 1 where the truth was 3: one repo at depth
    1 and the rest a level further down, under a plain directory."""
    ws = tmp_path / "ws"
    _git_repo(ws / "solo")                    # depth 1
    _git_repo(ws / "repositories" / "alpha")  # depth 2
    _git_repo(ws / "repositories" / "beta")   # depth 2
    return ws


def _bundling_message(logs):
    """The line naming what the scan found -- the diagnosis, not the remedy."""
    hits = [m for m in logs if "isn't itself a git repo" in m]
    assert hits, f"no bundling message in {logs!r}"
    return hits[0]


def _prescribed(logs):
    """The command the message tells the reader to run, stripped of indentation.

    Matched on `contextlake kb index` rather than on `--workspace`, because the
    zero-config notice mentions that flag too ("Pass --source PATH or
    --workspace DIR to index elsewhere") and would otherwise match first.
    """
    hits = [m.strip() for m in logs if "contextlake kb index" in m]
    assert hits, f"no prescribed command in {logs!r}"
    return hits[0]


def _kb(tmp_path):
    """A config naming this test's own store. `--store-dir` belongs to `init`, so
    a kb command is pointed at a store only through `[kb] store_dir`."""
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    return cfg, store_dir


def _repos(store_dir):
    """What the run actually filed, which is the only proof a refusal wrote
    nothing and a bundle wrote something."""
    from contextlake.kb.state import check_schema
    from contextlake.kb.store.sqlite_store import SqliteStore

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    try:
        return sorted(r.id for r in store.list_repos())
    finally:
        store.close()


def _args(cfg, source, **kw):
    return Namespace(config=str(cfg), workspace=None,
                     source=None if source is None else str(source),
                     repo=None, force=False, **kw)


# --- F11: the scan is one level deep ---------------------------------------

def test_nested_scan_finds_repos_below_the_first_level(tmp_path):
    """`glob("*/.git")` sees only direct children, so 2 of these 3 were
    invisible; the count is the whole point of the message that reports it."""
    ws = _fleet(tmp_path)

    assert len(list(ws.glob("*/.git"))) == 1, "the old shallow scan, for contrast"

    found = _nested_repo_dirs(ws)
    assert len(found) == 3
    assert sorted(p.relative_to(ws).as_posix() for p in found) == [
        "repositories/alpha", "repositories/beta", "solo",
    ]


def test_nested_scan_does_not_descend_into_a_repo_or_a_skipped_dir(tmp_path):
    """Bounded: a submodule is the parent repo's business, not a separate
    workspace member, and `node_modules` is never walked at all."""
    ws = tmp_path / "ws"
    _git_repo(ws / "outer")
    _git_repo(ws / "outer" / "vendor" / "inner")       # inside a repo
    _git_repo(ws / "node_modules" / "dep")             # inside a skipped dir
    _git_repo(ws / "deep" / "a" / "b" / "c" / "leaf")  # depth 5, still found

    found = sorted(p.relative_to(ws).as_posix() for p in _nested_repo_dirs(ws))
    assert found == ["deep/a/b/c/leaf", "outer"]


def test_nested_scan_skips_a_vendored_upstream_clone(tmp_path):
    """The one exclusion `discover_repos` applies that costs no git calls, so
    this count and what `--workspace` walks agree about it too."""
    ws = tmp_path / "ws"
    _git_repo(ws / "mine")
    _git_repo(ws / "module-federation" / "upstream")

    found = [p.relative_to(ws).as_posix() for p in _nested_repo_dirs(ws)]
    assert found == ["mine"]


def test_depth_phrase_reports_a_range_only_when_there_is_one():
    assert _depth_phrase({2}) == "at depth 2"
    assert _depth_phrase({1, 2}) == "at depths 1-2"


def test_index_reports_the_true_nested_count_and_its_depths(tmp_path, logs):
    """The message a reader acts on. Exit code is deliberately not asserted
    here: F4 owns whether this refuses, this test owns the count."""
    from contextlake.kb.commands import cmd_index

    ws = _fleet(tmp_path)
    cfg, store_dir = _kb(tmp_path)

    cmd_index(_args(cfg, ws))
    warned = [m for m in logs if "isn't itself a git repo" in m]
    assert warned, "a directory holding repos must say so"
    assert "3 git working tree(s) at depths 1-2" in warned[0]


# --- F12: the remedy must name the directory that was actually indexed -------

def test_typed_path_echoes_what_was_typed_and_quotes_only_when_needed():
    assert _typed_path(".") == "."
    assert _typed_path("./repositories") == "./repositories"
    assert _typed_path("/srv/fleet") == "/srv/fleet"
    assert _typed_path("/srv/my fleet") == "'/srv/my fleet'"


def test_advice_names_the_directory_given_not_the_current_one(tmp_path, logs):
    """`kb index <dir>` was told to run `--workspace .`, which points at the
    shell's cwd rather than at <dir>. Followed verbatim it indexes the wrong
    tree."""
    from contextlake.kb.commands import cmd_index

    ws = _fleet(tmp_path)
    cfg, store_dir = _kb(tmp_path)

    cmd_index(_args(cfg, ws))
    assert _prescribed(logs) == f"contextlake kb index --workspace {ws}"


def test_advice_still_says_dot_when_the_directory_is_the_current_one(
        tmp_path, monkeypatch, logs):
    """The short form is correct *and* recognisable when it is what the user
    typed (or what the zero-config default filled in), so it is not replaced by
    a long absolute path."""
    from contextlake.kb.commands import cmd_index

    ws = _fleet(tmp_path)
    cfg, store_dir = _kb(tmp_path)
    monkeypatch.chdir(ws)

    cmd_index(_args(cfg, None))
    assert _prescribed(logs) == "contextlake kb index --workspace ."


# --- F4: refuse rather than warn, with --bundle to opt in --------------------

def test_a_workspace_of_repos_is_refused_and_nothing_is_written(tmp_path, logs):
    """The defect this closes, measured on a real store: the warning printed, the
    run continued, and the workspace landed as a pseudo-repository holding a
    duplicate of every mirrored repo -- 63% of all nodes, unremovable at the
    time. A warning is one keystroke from being scrolled past."""
    from contextlake.kb.commands import cmd_index

    ws = _fleet(tmp_path)
    cfg, store_dir = _kb(tmp_path)

    assert cmd_index(_args(cfg, ws)) == 1
    assert _repos(store_dir) == [], "a refusal must not index anything"
    found = _bundling_message(logs)
    assert "3 git working tree(s) at depths 1-2" in found  # what
    assert "no indexable file lies outside them" in found  # how much, outside
    assert any("workspace mirroring several repositories" in m for m in logs)  # shape
    assert _prescribed(logs) == f"contextlake kb index --workspace {ws}"
    assert any("--bundle" in m for m in logs), "the override must be named"


def test_a_directory_one_level_above_a_single_repo_names_that_repo(tmp_path, logs):
    """Not every directory holding repos is a fleet. With one repo and nothing of
    the user's own beside it, `--workspace` would work but the honest command is
    the repo itself."""
    from contextlake.kb.commands import cmd_index

    ws = tmp_path / "ws"
    _git_repo(ws / "solo")
    cfg, store_dir = _kb(tmp_path)

    assert cmd_index(_args(cfg, ws)) == 1
    assert _repos(store_dir) == []
    assert any("one level above it" in m for m in logs)
    assert _prescribed(logs) == f"contextlake kb index {ws / 'solo'}"


def test_a_prescribed_command_survives_a_path_with_a_space(tmp_path, logs):
    """The single-repo prescription joins the path the user typed with the repo's
    own relative path, so it is the one branch that builds a path rather than
    echoing one. Quote the result, or the command it prints cannot be pasted."""
    from contextlake.kb.commands import cmd_index

    ws = tmp_path / "my ws"
    _git_repo(ws / "solo")
    cfg, store_dir = _kb(tmp_path)

    assert cmd_index(_args(cfg, ws)) == 1
    assert _prescribed(logs) == f"contextlake kb index '{ws / 'solo'}'"


def test_loose_sources_beside_a_vendored_clone_are_bundled_not_refused(tmp_path, logs):
    """The failure mode that makes auto-switching to `--workspace` unsafe, and so
    the one the diagnosis exists to tell apart: `--workspace` indexes the nested
    repo and nothing outside it, so refusing here would index the dependency and
    drop the user's own code -- worse than the bundling it replaced."""
    from contextlake.kb.commands import cmd_index

    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    for i in range(3):
        (proj / "src" / f"mod{i}.py").write_text(f"def own_{i}():\n    return {i}\n")
    _git_repo(proj / "vendor" / "lib")
    cfg, store_dir = _kb(tmp_path)

    assert cmd_index(_args(cfg, proj)) == 0
    assert _repos(store_dir) == ["proj"], "the user's own files must be indexed"
    assert "3 indexable files lie outside them" in _bundling_message(logs)


def test_bundle_opts_back_in_to_indexing_a_workspace_as_one_repo(tmp_path, logs):
    """A real escape hatch: the refusal is overridable, and --bundle is read
    before the shape is measured at all, so no diagnosis defect can block it."""
    from contextlake.kb.commands import cmd_index

    ws = _fleet(tmp_path)
    cfg, store_dir = _kb(tmp_path)

    assert cmd_index(_args(cfg, ws, bundle=True)) == 0
    assert _repos(store_dir) == ["ws"]
    assert not [m for m in logs if "isn't itself a git repo" in m], \
        "an explicit --bundle needs no lecture about bundling"


def test_a_plain_single_repo_directory_is_never_refused(tmp_path, logs):
    """The cost of a wrong refusal is an evaluator concluding the tool is broken,
    so the ordinary case -- point it at a repo -- must not go anywhere near the
    diagnosis."""
    from contextlake.kb.commands import cmd_index

    repo = tmp_path / "widgets"
    _git_repo(repo)
    cfg, store_dir = _kb(tmp_path)

    assert cmd_index(_args(cfg, repo)) == 0
    # `widgets@<root-commit>`, not bare `widgets`: this repo has no origin remote, and
    # since 2026-08-25 `--source` files repos under `resolve_repo_id` exactly as
    # `--workspace` always has. The two paths used to disagree, which left every
    # `--source`-indexed repo invisible to the connectors. What this test is about --
    # that pointing at a plain repo is never refused -- is unchanged.
    ids = _repos(store_dir)
    assert len(ids) == 1 and ids[0].startswith("widgets@"), ids
    assert not [m for m in logs if "isn't itself a git repo" in m]


def test_a_repo_with_its_own_submodules_is_never_refused(tmp_path, logs):
    """Same guarantee one step harder: a repo that itself contains checkouts is
    still a repo, and the diagnosis is gated on `src/.git` before it looks."""
    from contextlake.kb.commands import cmd_index

    repo = tmp_path / "widgets"
    _git_repo(repo)
    _git_repo(repo / "third_party" / "dep")
    cfg, store_dir = _kb(tmp_path)

    assert cmd_index(_args(cfg, repo)) == 0
    assert not [m for m in logs if "isn't itself a git repo" in m]


# --- F4: the shape decision itself ------------------------------------------

def _loose(dir_path, n, ext=".py"):
    dir_path.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (dir_path / f"f{i}{ext}").write_text("x = 1\n")


def test_shape_treats_a_handful_of_top_level_files_as_scaffolding(tmp_path):
    """A mirror with a setup.py and a few helper scripts next to it is still a
    mirror; an absolute floor is what keeps the share test from reading those as
    the user's codebase."""
    ws = _fleet(tmp_path)
    _loose(ws, _LOOSE_FILES_TRIVIAL)

    assert _bundle_shape(ws, _nested_repo_dirs(ws)) == ("workspace",
                                                        _LOOSE_FILES_TRIVIAL)


def test_shape_bundles_once_the_loose_files_are_a_real_share_of_the_tree(tmp_path):
    """Past the floor the decision is a share, not a count: enough of the user's
    own code outside the repos and bundling is what captures it."""
    ws = _fleet(tmp_path)
    _loose(ws / "own", _LOOSE_FILES_TRIVIAL + 1)

    assert _bundle_shape(ws, _nested_repo_dirs(ws)) == ("bundle",
                                                        _LOOSE_FILES_TRIVIAL + 1)


def test_shape_keeps_a_large_mirror_a_mirror_despite_loose_files(tmp_path):
    """The floor alone would send this to bundling. Twelve loose files against a
    repo holding hundreds are noise, and a rule tuned only to small trees would
    hand a whole fleet back to the pseudo-repository this refusal exists to
    prevent."""
    ws = _fleet(tmp_path)
    _loose(ws / "own", 12)
    _loose(ws / "repositories" / "alpha" / "deep", 12 * 19)

    shape, loose = _bundle_shape(ws, _nested_repo_dirs(ws))
    assert (shape, loose) == ("workspace", 12)


def test_shape_never_prescribes_a_single_repo_when_anything_sits_outside_it(tmp_path):
    """The one threshold that must stay exactly zero: with one nested repo, any
    file outside it is evidence of a loose-sources tree carrying a dependency,
    and "index that repo instead" would drop the user's file."""
    ws = tmp_path / "ws"
    _git_repo(ws / "solo")
    assert _bundle_shape(ws, _nested_repo_dirs(ws)) == ("one-level-too-high", 0)

    (ws / "mine.py").write_text("x = 1\n")
    assert _bundle_shape(ws, _nested_repo_dirs(ws)) == ("bundle", 1)


def test_content_outside_a_repo_is_measured_outside_every_repo(tmp_path):
    """A vendored upstream clone is excluded from the repo *count* (it is not a
    repo `--workspace` would index) but its files are not the user's content
    either, so they must not be counted as sitting outside. Two definitions of
    "repo" in one decision, deliberately."""
    from contextlake.kb.parse import count_files_outside_repos

    ws = tmp_path / "ws"
    _git_repo(ws / "module-federation" / "upstream")
    _loose(ws / "module-federation" / "upstream" / "pkg", 40)
    (ws / "mine.py").write_text("x = 1\n")

    assert _nested_repo_dirs(ws) == []
    assert count_files_outside_repos(ws) == 1
