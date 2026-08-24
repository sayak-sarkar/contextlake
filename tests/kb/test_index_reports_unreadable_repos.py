"""A repository git cannot open must not vanish from the tally.

`kb index --workspace` warned about a directory git could not read, dropped it from
discovery, indexed the rest, and reported **"0 failed" with exit 0**. `discover_repos`
logged the skip and returned only the survivors, so the caller counting its own results had
no way to learn anything had been dropped.

`docs/connecting-and-enriching.md:163` promises the opposite verdict, in those
words: the exit code is non-zero when any repository was skipped, "the same
verdict `kb index` gives a workspace where one repo failed to parse", because
"the graph an agent will cite from is not the one
you asked for, so the run should not read as clean".

The distinction the fix preserves: a VENDORED tree and a DUPLICATE checkout are also skipped,
and both are correct decisions taken on purpose. Folding them in would turn a clean run red
and teach a reader to ignore the count, so only the git-unreadable case is collected.
"""

from __future__ import annotations

import os
import subprocess

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _repo(path, name="a.py", body="def f():\n    return 1\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(body)
    for args in (["init", "-q", "."], ["add", "-A"], ["commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=path, env=_ENV, check=True,
                       capture_output=True)
    return path


def test_discovery_reports_a_directory_git_cannot_open(tmp_path):
    """The out-parameter carries what the log used to carry alone."""
    from contextlake.kb.parse import discover_repos

    ws = tmp_path / "ws"
    _repo(ws / "good")
    broken = _repo(ws / "broken")
    # A .git with no HEAD and no refs: git refuses to call it a repository at all.
    for leftover in ("HEAD", "refs"):
        target = broken / ".git" / leftover
        subprocess.run(["rm", "-rf", str(target)], check=True)

    unusable: list[str] = []
    found = discover_repos(str(ws), unusable=unusable)

    # A repo with no `origin` remote gets a `name@shorthash` canonical id, so compare on
    # the name half rather than the whole id.
    assert [rid.split("@")[0].rsplit("/", 1)[-1] for rid, _p in found] == ["good"]
    assert unusable == ["broken"], "the unreadable directory was dropped without a trace"


def test_discovery_without_the_out_parameter_is_unchanged(tmp_path):
    """The argument is optional, so every existing caller keeps working untouched."""
    from contextlake.kb.parse import discover_repos

    ws = tmp_path / "ws"
    _repo(ws / "good")
    assert len(discover_repos(str(ws))) == 1


def test_a_vendored_tree_is_not_reported_as_unreadable(tmp_path):
    """The skip that is a correct decision, not a problem.

    Without this, the fix would make every workspace containing a vendored dependency exit
    non-zero, and a user would learn to ignore the exit code -- which is worse than the
    defect it replaced.
    """
    from contextlake.kb.parse import discover_repos

    ws = tmp_path / "ws"
    outer = _repo(ws / "outer")
    # A vendored checkout inside another repo's working tree.
    _repo(outer / "vendor" / "dep")

    unusable: list[str] = []
    discover_repos(str(ws), unusable=unusable)
    assert unusable == [], f"a deliberate skip was reported as unreadable: {unusable}"
