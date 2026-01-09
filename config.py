"""
Configuration loading for gitlab_sync
"""

import configparser
import os

# Configuration file paths
CONFIG_FILE = os.path.expanduser('~/.gitlab_sync.ini')
LOCAL_CONFIG_FILE = '.gitlab_sync.ini'

# Default Configuration
DEFAULT_CONFIG = {
    'work_dir': os.path.expanduser('~/Work'),
    'gitlab_group': 'your-gitlab-group',
    'cache_dir': '/tmp',
    'cache_file': 'gitlab_projects.txt',
    'cache_json': 'gitlab_projects.json',
    'clone_timeout': '300',
    'fetch_timeout': '60',
    'branch_timeout': '30',
    'pull_timeout': '60',
    'max_workers': '8',
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


def load_config(config_path=None):
    """Load configuration from config files with precedence: local > global > defaults."""
    config = DEFAULT_CONFIG.copy()
    parser = configparser.ConfigParser()
    
    # Try local config first
    if os.path.exists(LOCAL_CONFIG_FILE):
        parser.read(LOCAL_CONFIG_FILE)
        if 'gitlab_sync' in parser:
            config.update(parser['gitlab_sync'])
    
    # Then try global config
    if os.path.exists(CONFIG_FILE):
        parser.read(CONFIG_FILE)
        if 'gitlab_sync' in parser:
            config.update(parser['gitlab_sync'])
    
    # If custom config path provided, use it
    if config_path and os.path.exists(config_path):
        parser.read(config_path)
        if 'gitlab_sync' in parser:
            config.update(parser['gitlab_sync'])
    
    return config


def get_cache_paths(config):
    """Get cache file paths from config."""
    cache_dir = config.get('cache_dir', '/tmp')
    cache_file = config.get('cache_file', 'gitlab_projects.txt')
    cache_json = config.get('cache_json', 'gitlab_projects.json')
    return os.path.join(cache_dir, cache_file), os.path.join(cache_dir, cache_json)
