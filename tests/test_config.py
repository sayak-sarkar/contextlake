"""Tests for configuration loading and precedence."""

import os

from contextlake.config import DEFAULT_CONFIG, get_cache_paths, load_config


def _isolate_globals(monkeypatch, tmp_path):
    """Point both the current and legacy global config files at non-existent
    paths so a test only ever sees the files it writes itself."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    monkeypatch.setattr("contextlake.config.LEGACY_CONFIG_FILE", str(tmp_path / "none-legacy.ini"))
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


def test_legacy_gitlab_sync_section_still_read(tmp_path, monkeypatch):
    # Back-compat: a config file using the former [gitlab_sync] section must
    # still load after the rename.
    _isolate_globals(monkeypatch, tmp_path)
    custom = tmp_path / "old.ini"
    custom.write_text("[gitlab_sync]\ngitlab_group = legacy-group\nmax_workers = 5\n")

    config = load_config(str(custom))
    assert config["gitlab_group"] == "legacy-group"
    assert config["max_workers"] == "5"


def test_legacy_global_config_is_discovered(tmp_path, monkeypatch):
    # Back-compat: an existing ~/.gitlab_sync.ini (the legacy global) is still
    # discovered without passing --config.
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "nonexistent.ini"))
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "legacy_global.ini"
    legacy.write_text("[gitlab_sync]\ngitlab_group = from-legacy-global\n")
    monkeypatch.setattr("contextlake.config.LEGACY_CONFIG_FILE", str(legacy))

    config = load_config()
    assert config["gitlab_group"] == "from-legacy-global"


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
