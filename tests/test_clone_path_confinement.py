"""clone never touches a path outside work_dir, whatever the forge's project list says.

The incident: `to_local_path` only strips the `<group>/` prefix, and
`clone_repository` joins the result onto work_dir with `os.path.join`. A project
path of `grp/../outside` became `../outside`, an absolute one stayed absolute,
and `grp/` became work_dir itself. An existing non-git directory at the target
counts as "corrupted", so clone ran `shutil.rmtree` on it and cloned in its
place. Reproduced before the fix: a sentinel directory outside work_dir was
deleted and replaced by the clone.

Every directory these tests create, and every one the code could delete, is
under tmp_path. git is never run: `fake_subprocess` stands in for it.
"""

import os

import pytest

from contextlake import core
from contextlake.core import clone_repository


@pytest.fixture
def layout(tmp_path):
    """work_dir with one existing clone, plus a sentinel non-git directory beside it."""
    work = tmp_path / "work"
    (work / "keep" / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "SENTINEL.txt").write_text("not a repository")
    (outside / "inner" / "SENTINEL.txt").write_text("not a repository")
    return work, outside


# Local paths that each resolve outside work_dir, or onto work_dir itself. The
# symlink case goes one level past the link: rmtree refuses a symlink itself, but
# not a real directory reached through one.
ESCAPING = {
    "dot-dot": lambda work, outside: "../outside",
    "absolute": lambda work, outside: str(outside),
    "group root, trailing slash": lambda work, outside: "",
    "group root, dot": lambda work, outside: ".",
    "dot-dot back to the root": lambda work, outside: "keep/..",
    "through a symlink inside a clone": lambda work, outside: "keep/link/inner",
}


@pytest.mark.parametrize("dry_run", ["false", "true"])
@pytest.mark.parametrize("label", list(ESCAPING))
def test_a_path_that_leaves_work_dir_is_refused_before_any_filesystem_action(
        layout, base_config, fake_subprocess, label, dry_run):
    work, outside = layout
    (work / "keep" / "link").symlink_to(outside, target_is_directory=True)
    local = ESCAPING[label](work, outside)
    config = {**base_config, "clean_corrupted": "true", "dry_run": dry_run,
              "clone_method": "git"}

    status, path, message = clone_repository(
        local, f"grp/{local}", "https://gitlab.com/grp/x.git", "", str(work), config)

    # The conclusion first: nothing outside work_dir was deleted, nothing inside
    # it either, and no clone was attempted.
    assert (outside / "SENTINEL.txt").exists()
    assert (outside / "inner" / "SENTINEL.txt").exists()
    assert (work / "keep" / ".git").is_dir()
    assert fake_subprocess.calls == []
    assert (status, path) == ("skip", local)
    assert message.startswith("Refused")


def test_an_ordinary_nested_path_still_clones(layout, base_config, fake_subprocess, monkeypatch):
    work, _ = layout
    monkeypatch.setattr(core.shutil, "which", lambda _: None)
    status, _, _ = clone_repository("team/api", "grp/team/api",
                                    "https://gitlab.com/grp/team/api.git", "",
                                    str(work), {**base_config, "clone_method": "git"})
    assert status == "ok"
    assert fake_subprocess.commands_matching("git", "clone", str(work / "team" / "api"))


def test_the_whole_pipeline_skips_an_escaping_project(layout, base_config, fake_subprocess,
                                                     monkeypatch):
    """fetch -> cache -> clone_missing_repos, with a forge listing one bad path and
    one good one. The bad one is reported as skipped; the good one clones."""
    work, outside = layout
    monkeypatch.setattr(core.shutil, "which", lambda _: None)
    listing = {1: [
        {"path_with_namespace": "grp/../outside", "http_url_to_repo": "https://gitlab.com/x.git",
         "ssh_url_to_repo": "", "archived": False, "default_branch": "main"},
        {"path_with_namespace": "grp/team/api", "http_url_to_repo": "https://gitlab.com/a.git",
         "ssh_url_to_repo": "", "archived": False, "default_branch": "main"},
    ]}
    monkeypatch.setattr(core, "_fetch_projects_page_glab",
                        lambda group_enc, per_page, page: listing.get(page, []))
    config = {**base_config, "work_dir": str(work), "gitlab_group": "grp", "group": "grp",
              "clone_method": "git", "adaptive_workers": "false"}

    core.fetch_gitlab_projects("grp", config)
    result = core.clone_missing_repos(str(work), config, "grp")

    assert (outside / "SENTINEL.txt").exists()
    assert result.ok == 1 and result.skipped == 1 and result.failed == 0
    clones = fake_subprocess.commands_matching("git", "clone")
    assert [c[-1] for c in clones] == [os.path.join(str(work), "team/api")]
