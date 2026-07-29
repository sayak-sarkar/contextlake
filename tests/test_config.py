"""Tests for configuration loading and precedence."""

import os

from contextlake.config import (
    DEFAULT_CONFIG,
    find_ancestor_config,
    get_cache_paths,
    load_config,
)


def _isolate_globals(monkeypatch, tmp_path):
    """Point the global AND local config files at non-existent absolute paths
    so a test only ever sees the files it writes itself -- an absolute path
    is checked directly (see find_ancestor_config), not walked, so this also
    neutralizes the ancestor-directory search for tests that don't want it."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    monkeypatch.setattr("contextlake.config.LOCAL_CONFIG_FILE", str(tmp_path / "no-local.ini"))
    monkeypatch.chdir(tmp_path)


def test_defaults_when_no_files(tmp_path, monkeypatch):
    # Run from an empty dir with no global config so only defaults apply.
    _isolate_globals(monkeypatch, tmp_path)
    config = load_config()
    assert config["gitlab_group"] == DEFAULT_CONFIG["gitlab_group"]
    assert config["max_workers"] == DEFAULT_CONFIG["max_workers"]


def test_explicit_config_path_overrides_defaults(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    custom = tmp_path / "custom.ini"
    custom.write_text("[contextlake]\ngitlab_group = my-group\nmax_workers = 3\n")

    config = load_config(str(custom))
    assert config["gitlab_group"] == "my-group"
    assert config["max_workers"] == "3"
    # untouched keys still come from defaults
    assert config["clone_timeout"] == DEFAULT_CONFIG["clone_timeout"]


def test_config_path_values_are_tilde_expanded(tmp_path, monkeypatch):
    # Bug: a `~` in a config-file work_dir was treated as a literal directory,
    # so status/clone operated on a non-existent path and saw 0 local repos.
    _isolate_globals(monkeypatch, tmp_path)
    custom = tmp_path / "c.ini"
    custom.write_text("[contextlake]\nwork_dir = ~/repos\ncache_dir = ~/.cache/gs\n")
    config = load_config(str(custom))
    assert config["work_dir"] == os.path.expanduser("~/repos")
    assert config["cache_dir"] == os.path.expanduser("~/.cache/gs")
    assert "~" not in config["work_dir"]


def test_get_cache_paths_joins_dir_and_names():
    config = {"cache_dir": "/var/cache", "cache_file": "p.txt", "cache_json": "p.json"}
    text, js = get_cache_paths(config)
    assert text == "/var/cache/p.txt"
    assert js == "/var/cache/p.json"


def test_find_ancestor_config_walks_up_from_a_subdirectory(tmp_path):
    """A config at the project root must be discovered from a subdirectory
    several levels deep underneath it -- the whole point of "directory
    inheritance" is that you don't have to be in the exact directory that
    holds the file."""
    (tmp_path / ".contextlake.ini").write_text("[contextlake]\ngitlab_group = root\n")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    found = find_ancestor_config(".contextlake.ini", start=str(deep))
    assert found == str(tmp_path / ".contextlake.ini")


def test_find_ancestor_config_prefers_the_nearest_match(tmp_path):
    """Two ancestors both have the file -- the closer one wins, not the root."""
    (tmp_path / ".contextlake.ini").write_text("[contextlake]\ngitlab_group = root\n")
    mid = tmp_path / "a"
    mid.mkdir()
    (mid / ".contextlake.ini").write_text("[contextlake]\ngitlab_group = mid\n")
    deep = mid / "b"
    deep.mkdir()
    found = find_ancestor_config(".contextlake.ini", start=str(deep))
    assert found == str(mid / ".contextlake.ini")


def test_find_ancestor_config_returns_none_when_nothing_found(tmp_path):
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    assert find_ancestor_config(".contextlake.ini", start=str(deep)) is None


def test_find_ancestor_config_checks_an_absolute_filename_directly_no_walk(tmp_path):
    """An absolute path is the isolation mechanism every existing test uses
    (monkeypatch LOCAL_CONFIG_FILE to a specific tmp path) -- it must never
    walk, only check that exact path, or those tests would start seeing real
    ancestor files from wherever pytest happens to run."""
    missing = tmp_path / "does-not-exist.ini"
    assert find_ancestor_config(str(missing)) is None
    missing.write_text("[contextlake]\n")
    assert find_ancestor_config(str(missing)) == str(missing)


def test_load_config_inherits_local_config_from_a_parent_directory(tmp_path, monkeypatch):
    """End-to-end: cd into a subdirectory of a project that has a root-level
    .contextlake.ini, and load_config() must still pick it up."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    (tmp_path / ".contextlake.ini").write_text(
        "[contextlake]\ngitlab_group = inherited-group\n")
    deep = tmp_path / "sub" / "dir"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    config = load_config()
    assert config["gitlab_group"] == "inherited-group"
