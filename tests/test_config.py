"""Tests for configuration loading and precedence."""

import os

import pytest

from contextlake.config import (
    DEFAULT_CONFIG,
    ConfigError,
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


def test_missing_explicit_config_path_is_a_hard_error(tmp_path, monkeypatch):
    """A --config path that doesn't exist must fail loudly, not silently fall
    through the precedence chain to ~/.contextlake.ini -- which can point at a
    completely different workspace than intended. Mirrors kb.toml's identical
    guard (see kb/test_kb_config.py's test of the same name)."""
    _isolate_globals(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "does-not-exist.ini"))


def test_missing_explicit_config_path_does_not_fall_back_to_global(tmp_path, monkeypatch):
    """The exact near-miss this guards against: a real global config exists (as
    it would on the user's own machine) and a typo'd/not-yet-created --config
    path must never silently resolve to it."""
    _isolate_globals(monkeypatch, tmp_path)
    real_global = tmp_path / "real-global.ini"
    real_global.write_text("[contextlake]\ngitlab_group = real-group\n")
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(real_global))
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "typo-d.ini"))


def test_get_cache_paths_joins_dir_and_names():
    config = {"cache_dir": "/var/cache", "cache_file": "p.txt", "cache_json": "p.json"}
    text, js = get_cache_paths(config)
    assert text == "/var/cache/p.txt"
    assert js == "/var/cache/p.json"


# --- where the projects cache lands ----------------------------------------
#
# The cache lists every repository the account can enumerate, with clone URLs.
# It used to default to /tmp/gitlab_projects.txt: outside the user's home, so no
# HOME-based isolation reached it; world-readable on a shared host; and at a
# fully predictable path another user can pre-create or symlink.

def test_default_cache_lands_under_home_not_tmp(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    cache_file, cache_json = get_cache_paths(load_config())

    assert cache_file.startswith(str(home / ".cache" / "contextlake") + os.sep)
    assert cache_json.startswith(str(home / ".cache" / "contextlake") + os.sep)
    # The old default, verbatim (tmp_path itself lives under /tmp, so a bare
    # "not under /tmp" assertion would be meaningless here).
    assert cache_file != "/tmp/gitlab_projects.txt"


def test_default_cache_honours_xdg_cache_home(tmp_path, monkeypatch):
    """Read at call time, never frozen at import -- DEFAULT_CONFIG is a
    module-level literal, so an expanded value would pin the first HOME seen."""
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    cache_file, _ = get_cache_paths(load_config())

    assert cache_file.startswith(str(tmp_path / "xdg" / "contextlake") + os.sep)


def test_default_cache_directory_is_private(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    cache_file, _ = get_cache_paths(load_config())

    for directory in (os.path.dirname(cache_file),
                      os.path.dirname(os.path.dirname(cache_file))):
        assert os.stat(directory).st_mode & 0o777 == 0o700, directory


def test_default_cache_is_per_workspace_not_one_global_file(tmp_path, monkeypatch):
    """Two unrelated workspaces (and every config in a directory nest) shared
    one /tmp/gitlab_projects.txt, so a directory-scoped config was never
    actually isolated: `mirror status` could report another workspace's fleet
    as its own, from a cache it never wrote."""
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    a, _ = get_cache_paths({"work_dir": str(tmp_path / "a"), "gitlab_group": "team"})
    b, _ = get_cache_paths({"work_dir": str(tmp_path / "b"), "gitlab_group": "team"})
    assert a != b


def test_default_cache_also_separates_two_groups_in_one_workspace(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    work = str(tmp_path / "w")

    a, _ = get_cache_paths({"work_dir": work, "gitlab_group": "team-a"})
    b, _ = get_cache_paths({"work_dir": work, "gitlab_group": "team-b"})
    assert a != b


def test_default_cache_path_is_stable_across_calls(tmp_path, monkeypatch):
    """The whole point of a cache is that the next run finds it again."""
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = {"work_dir": str(tmp_path / "w"), "gitlab_group": "team"}

    assert get_cache_paths(config) == get_cache_paths(dict(config))


def test_an_explicitly_configured_cache_dir_is_used_verbatim(tmp_path, monkeypatch):
    """Some users set cache_dir deliberately (including to /tmp), and the audit
    report documented at <cache_dir>/repo_audit.json is written alongside these
    -- naming the directory means "put it exactly here", no subdirectory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    chosen = tmp_path / "chosen"

    cache_file, cache_json = get_cache_paths({"cache_dir": str(chosen),
                                              "work_dir": str(tmp_path / "w"),
                                              "gitlab_group": "team"})

    assert cache_file == str(chosen / "gitlab_projects.txt")
    assert cache_json == str(chosen / "gitlab_projects.json")


def test_an_unwritable_cache_dir_warns_instead_of_raising(tmp_path, monkeypatch, gls_logs):
    """get_cache_paths runs on read paths too; a directory that cannot be made
    must surface as the real read/write error from the code touching the file,
    not as a mkdir traceback from a path helper."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    cache_file, _ = get_cache_paths({"cache_dir": str(blocker / "under")})

    assert cache_file.startswith(str(blocker))
    assert any("cache directory" in r.getMessage() for r in gls_logs.records)


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
