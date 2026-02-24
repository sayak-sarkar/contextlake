"""Tests for configuration loading and precedence."""

import os

from contextlake.config import DEFAULT_CONFIG, get_cache_paths, load_config


def test_defaults_when_no_files(tmp_path, monkeypatch):
    # Run from an empty dir with no global config so only defaults apply.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "nonexistent.ini"))
    config = load_config()
    assert config["gitlab_group"] == DEFAULT_CONFIG["gitlab_group"]
    assert config["max_workers"] == DEFAULT_CONFIG["max_workers"]


def test_explicit_config_path_overrides_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "nonexistent.ini"))
    custom = tmp_path / "custom.ini"
    custom.write_text("[gitlab_sync]\ngitlab_group = my-group\nmax_workers = 3\n")

    config = load_config(str(custom))
    assert config["gitlab_group"] == "my-group"
    assert config["max_workers"] == "3"
    # untouched keys still come from defaults
    assert config["clone_timeout"] == DEFAULT_CONFIG["clone_timeout"]


def test_config_path_values_are_tilde_expanded(tmp_path, monkeypatch):
    # Bug: a `~` in a config-file work_dir was treated as a literal directory,
    # so status/clone operated on a non-existent path and saw 0 local repos.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    custom = tmp_path / "c.ini"
    custom.write_text("[gitlab_sync]\nwork_dir = ~/repos\ncache_dir = ~/.cache/gs\n")
    config = load_config(str(custom))
    assert config["work_dir"] == os.path.expanduser("~/repos")
    assert config["cache_dir"] == os.path.expanduser("~/.cache/gs")
    assert "~" not in config["work_dir"]


def test_get_cache_paths_joins_dir_and_names():
    config = {"cache_dir": "/var/cache", "cache_file": "p.txt", "cache_json": "p.json"}
    text, js = get_cache_paths(config)
    assert text == "/var/cache/p.txt"
    assert js == "/var/cache/p.json"
