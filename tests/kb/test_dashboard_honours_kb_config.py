"""The dashboard's Sync and Add buttons must index by the same rules as `kb index`.

`add_repo` and `sync_repo` called `index_repo_dir` with NO options, so they used the
library defaults while the CLI used the user's `[kb]` config. One click then permanently
replaced a repo's filtered graph with an unfiltered one, in the same store, with no
message and nothing recording which policy produced which rows.

The measurement that matters is a config that DIFFERS from the library default -- with
`languages = ["python"]` on a repo holding Python and JavaScript, the button wrote 6
nodes where the CLI wrote 3. A fixture whose config happens to match the default proves
nothing, which is the first thing this test got wrong.
"""

from __future__ import annotations

import subprocess

import pytest

from contextlake.kb.dashboard.mutations import _parse_opts
from contextlake.kb.parse import index_repo_dir


@pytest.fixture
def mixed_repo(tmp_path, monkeypatch):
    """A repo with two languages, and a config that indexes only one of them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def one():\n    return 1\ndef two():\n    return 2\n",
                               encoding="utf-8")
    (repo / "b.js").write_text("function jsOne() { return 1; }\n"
                               "function jsTwo() { return 2; }\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin"}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env,
                       capture_output=True)

    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)
    (home / ".contextlake" / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{(tmp_path / "store").as_posix()}"\n'
        'languages = ["python"]\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    return repo, tmp_path / "store"


def test_the_dashboard_path_indexes_by_the_same_rules_as_the_cli(mixed_repo):
    """THE LOAD-BEARING ASSERTION: the button's options must match the config."""
    repo, store_dir = mixed_repo
    opts = _parse_opts(store_dir)
    assert opts.get("languages") == ["python"], (
        f"the dashboard did not read [kb] languages; got {opts!r}")

    unconfigured = index_repo_dir(str(repo), "demo", head_commit="h")
    configured = index_repo_dir(str(repo), "demo", head_commit="h", **opts)

    assert len(configured.nodes) < len(unconfigured.nodes), (
        "the fixture no longer distinguishes the two paths, so this test proves "
        "nothing — pick a config that differs from the library default")
    langs = {n.lang for n in configured.nodes if getattr(n, "lang", None)}
    assert "javascript" not in langs, (
        f"a filtered index still contains javascript nodes: {langs}")


def test_a_broken_config_falls_back_loudly_not_to_a_third_policy(tmp_path, monkeypatch):
    """If the config cannot be read the button must use the documented defaults and say
    so — inventing a third indexing policy is how the two paths diverged originally."""
    def boom(*a, **k):
        raise RuntimeError("unreadable config")

    monkeypatch.setattr("contextlake.kb.config.load_kb_config", boom)
    # Patched at `logging_setup`, NOT on the mutations module: `_parse_opts` does
    # `from ...logging_setup import log` INSIDE the function, so it binds the real
    # function at call time and a module-attribute patch never takes. The first
    # version of this test patched `mut.log`, collected nothing, and would have
    # passed while asserting nothing -- the exact failure this release is about.
    import contextlake.logging_setup as ls

    said: list[str] = []
    monkeypatch.setattr(ls, "log", lambda msg, *a, **k: said.append(str(msg)))

    opts = _parse_opts(tmp_path)
    assert opts == {}, "a failed config read must yield the documented defaults"
    # The LOUD half, which this test's name promises and its first version never
    # asserted -- it collected into `said` and then ignored it. A silent fallback is
    # the same defect the surrounding release is about.
    assert any("could not read" in s for s in said), (
        f"the fallback was silent; said={said!r}")
