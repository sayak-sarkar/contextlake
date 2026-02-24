"""
Configuration loading for gitlab_sync
"""

import configparser
import os

from .logging_setup import log

# Configuration file paths
CONFIG_FILE = os.path.expanduser('~/.gitlab_sync.ini')
LOCAL_CONFIG_FILE = '.gitlab_sync.ini'

# Config values that name a filesystem location and so must have ~ and $VARS
# expanded (the INI/CLI layers store them verbatim, unlike DEFAULT_CONFIG).
PATH_KEYS = ('work_dir', 'cache_dir')


def expand_path(value):
    """Expand ~ and environment variables in a path-like config value."""
    return os.path.expanduser(os.path.expandvars(value)) if value else value

# Default Configuration
DEFAULT_CONFIG = {
    'work_dir': os.path.expanduser('~/work'),
    'gitlab_group': 'your-gitlab-group',
    'cache_dir': '/tmp',
    'cache_file': 'gitlab_projects.txt',
    'cache_json': 'gitlab_projects.json',
    'clone_timeout': '300',
    'fetch_timeout': '60',
    'branch_timeout': '30',
    'pull_timeout': '60',
    'max_workers': '8',
    'clone_method': 'auto',  # auto -> prefer glab (uses its auth), else git over HTTPS
    'branch_strategy': 'hybrid',  # most-active selection: commits | recency | hybrid
    'clean_corrupted': 'true',
    'max_retries': '3',
    'backoff_initial': '1',
    'backoff_max': '30',
    'adaptive_workers': 'true',
    'min_workers': '2',
    'error_threshold': '0.5',
    'protect_working_branches': 'true',
    'safe_branches': 'main,master,develop,development',
    'require_clean_workspace': 'true',
    'auto_stash': 'false'
}


def _merge(config, path):
    """Merge the [gitlab_sync] section of an INI file into config, if present."""
    if not path or not os.path.exists(path):
        return
    parser = configparser.ConfigParser()
    parser.read(path)
    if 'gitlab_sync' in parser:
        config.update(parser['gitlab_sync'])


def load_config(config_path=None):
    """Load configuration with precedence: explicit --config > local > global > defaults.

    Sources are merged from lowest to highest precedence so the later (more
    specific) source wins on conflicting keys.
    """
    config = DEFAULT_CONFIG.copy()
    _merge(config, CONFIG_FILE)         # global (~/.gitlab_sync.ini)
    _merge(config, LOCAL_CONFIG_FILE)   # local workspace config
    _merge(config, config_path)         # explicit --config path

    # INI/CLI values are stored verbatim, so a `work_dir = ~/repos` would
    # otherwise be treated as a literal "~" directory. Expand here.
    for key in PATH_KEYS:
        if key in config:
            config[key] = expand_path(config[key])

    if config.get('gitlab_group') == DEFAULT_CONFIG['gitlab_group']:
        log("WARNING: gitlab_group is still the placeholder 'your-gitlab-group'. "
            "Copy .gitlab_sync.ini.example to .gitlab_sync.ini and set your group.")

    return config


def get_cache_paths(config):
    """Get cache file paths from config."""
    cache_dir = config.get('cache_dir', '/tmp')
    cache_file = config.get('cache_file', 'gitlab_projects.txt')
    cache_json = config.get('cache_json', 'gitlab_projects.json')
    return os.path.join(cache_dir, cache_file), os.path.join(cache_dir, cache_json)
