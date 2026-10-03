"""`get_local_repos` lists mirror repos, not every `.git` below them.

The walk used to descend into every repo, so a vendored checkout inside a clone
was listed as a mirror repo, and ``update`` fetched it. It also walked into
``.git`` and ``node_modules``. A directory that is a repo is now a stopping point.
``verify`` opts back in with ``include_nested=True``, because reporting a repo
inside another repo is its job.
"""

import os

from conftest import make_local_repo
from contextlake import core
from contextlake.core import filtered_local_repos, get_local_repos


def _sorted(paths):
    return sorted(p.replace(os.sep, "/") for p in paths)


def test_a_checkout_inside_a_clone_is_not_a_mirror_repo(tmp_path):
    make_local_repo(tmp_path, "grp/outer")
    make_local_repo(tmp_path, "grp/outer/vendor/lib")
    make_local_repo(tmp_path, "grp/sibling")

    assert _sorted(get_local_repos(str(tmp_path))) == ["grp/outer", "grp/sibling"]


def test_a_flat_and_deep_fleet_is_listed_unchanged(tmp_path):
    for rel in ("a", "g/b", "g/h/c", "g/h/i/d"):
        make_local_repo(tmp_path, rel)

    assert _sorted(get_local_repos(str(tmp_path))) == ["a", "g/b", "g/h/c", "g/h/i/d"]


def test_node_modules_is_never_entered(tmp_path):
    """Under a plain directory, so the stop-at-a-repo rule cannot be what hides it."""
    make_local_repo(tmp_path, "grp/real")
    make_local_repo(tmp_path, "grp/stray/node_modules/dep")

    assert _sorted(get_local_repos(str(tmp_path))) == ["grp/real"]


def test_include_nested_lists_a_repo_inside_a_repo(tmp_path):
    """What `verify` needs, so that its nested-repo check has something to find."""
    make_local_repo(tmp_path, "grp/outer")
    make_local_repo(tmp_path, "grp/outer/vendor/lib")

    assert _sorted(get_local_repos(str(tmp_path), include_nested=True)) == [
        "grp/outer", "grp/outer/vendor/lib"]


def test_include_nested_still_skips_dot_git_and_node_modules(tmp_path):
    outer = make_local_repo(tmp_path, "grp/outer")
    (outer / ".git" / "modules" / "sub" / ".git").mkdir(parents=True)
    make_local_repo(tmp_path, "grp/outer/node_modules/dep")
    make_local_repo(tmp_path, "grp/outer/vendor/lib")

    assert _sorted(get_local_repos(str(tmp_path), include_nested=True)) == [
        "grp/outer", "grp/outer/vendor/lib"]


def test_a_work_dir_that_is_itself_a_repo_still_lists_the_clones_below_it(tmp_path):
    """The work directory is the container, never a stopping point. Without this the
    whole fleet would vanish from `update` and `clone` would try to re-clone it."""
    (tmp_path / ".git").mkdir()
    make_local_repo(tmp_path, "grp/a")
    make_local_repo(tmp_path, "grp/b")

    assert _sorted(get_local_repos(str(tmp_path))) == [".", "grp/a", "grp/b"]


def test_a_submodule_checkout_is_not_a_repo_before_or_after(tmp_path):
    """A submodule's `.git` is a file, so it never matched. Its parent stops the walk."""
    outer = make_local_repo(tmp_path, "grp/outer")
    sub = outer / "libs" / "sub"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/sub\n")

    assert _sorted(get_local_repos(str(tmp_path))) == ["grp/outer"]
    assert _sorted(get_local_repos(str(tmp_path), include_nested=True)) == ["grp/outer"]


def test_filtered_local_repos_passes_include_nested_through(tmp_path, base_config):
    make_local_repo(tmp_path, "grp/outer")
    make_local_repo(tmp_path, "grp/outer/vendor/lib")

    assert _sorted(filtered_local_repos(tmp_path, base_config)) == ["grp/outer"]
    assert _sorted(filtered_local_repos(tmp_path, base_config, include_nested=True)) == [
        "grp/outer", "grp/outer/vendor/lib"]


def test_update_does_not_touch_a_vendored_checkout(tmp_path, base_config, monkeypatch):
    """The harm: `update` fetched and fast-forwarded a checkout the clone vendors."""
    make_local_repo(tmp_path, "grp/outer")
    make_local_repo(tmp_path, "grp/outer/vendor/lib")
    touched = []
    monkeypatch.setattr(core, "update_repository",
                        lambda p, wd, cfg: touched.append(p) or ("nochange", p, "x"))

    core.update_repositories(str(tmp_path), base_config)

    assert _sorted(touched) == ["grp/outer"]


def test_verify_still_reports_a_repo_nested_in_a_repo(tmp_path, base_config, monkeypatch,
                                                      gls_logs):
    """`verify` is the one reader that wants nested repos, and it must keep them."""
    import re

    monkeypatch.setattr(core, "load_gitlab_projects",
                        lambda c, g, **kw: {"g/a": {"archived": False, "full_path": "g/a"}})
    make_local_repo(tmp_path, "g/a")
    make_local_repo(tmp_path, "g/a/vendor/lib")
    make_local_repo(tmp_path, "g/a/node_modules/dep")  # an installed package, not corruption

    core.verify_structure(str(tmp_path), base_config, "g")

    assert re.search(r"Nested\s+1\b", gls_logs.text)
    assert "g/a/vendor/lib" in gls_logs.text
    assert "node_modules" not in gls_logs.text
    # Reported once, as nested. `status` does not count it either, so it is no Extra repo.
    assert re.search(r"Extra\s+0\b", gls_logs.text)
