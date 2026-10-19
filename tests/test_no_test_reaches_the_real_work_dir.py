"""Every test starts with the mirror defaults pointed under its own temporary home.

`config.py` expands `~` in `DEFAULT_CONFIG["work_dir"]` and in `CONFIG_FILE` when it is
imported. `conftest.py` imports contextlake first and moves HOME afterwards, so the
expanded values kept the operator's real home. A test that let a mirror verb run on the
defaults, with the code under test broken or absent, fetched every clone under the real
work directory. That happened during a break-test of a new CLI test.

These tests read the values and run no mirror verb. A verb here would be the incident.
"""

from pathlib import Path

from contextlake import config
from contextlake.config import DEFAULT_CONFIG


def test_the_default_work_dir_is_under_the_test_home(tmp_path):
    home = tmp_path / "isolated-home"
    assert DEFAULT_CONFIG["work_dir"] == str(home / "work")
    assert Path.home() == home


def test_the_global_config_file_is_under_the_test_home(tmp_path):
    assert config.CONFIG_FILE == str(tmp_path / "isolated-home" / ".contextlake.ini")


def test_load_config_on_the_defaults_resolves_work_dir_under_the_test_home(tmp_path, monkeypatch):
    """Through `load_config`, the route a mirror verb takes to its work_dir."""
    monkeypatch.chdir(tmp_path)
    assert tmp_path in Path(config.load_config(None)["work_dir"]).parents
