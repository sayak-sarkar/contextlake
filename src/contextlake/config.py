"""
Configuration loading for contextlake
"""

import configparser
import os

from .logging_setup import log

# Configuration file paths.
CONFIG_FILE = os.path.expanduser('~/.contextlake.ini')
LOCAL_CONFIG_FILE = '.contextlake.ini'

# INI section name.
SECTIONS = ('contextlake',)

# Config values that name a filesystem location and so must have ~ and $VARS
# expanded (the INI/CLI layers store them verbatim, unlike DEFAULT_CONFIG).
PATH_KEYS = ('work_dir', 'cache_dir')


def expand_path(value):
    """Expand ~ and environment variables in a path-like config value."""
    return os.path.expanduser(os.path.expandvars(value)) if value else value


class ConfigError(RuntimeError):
    """An explicit --config path was invalid."""


# Default Configuration
DEFAULT_CONFIG = {
    'work_dir': os.path.expanduser('~/work'),
    # Which platform `fetch` enumerates: gitlab (default) | github | bitbucket |
    # gitea (codeberg/forgejo are gitea flavors). `group` is the generic key for
    # the org/workspace/owner to mirror; gitlab_group remains as its alias.
    'platform': 'gitlab',
    'gitlab_group': 'your-gitlab-group',
    'cache_dir': '/tmp',
    'cache_file': 'gitlab_projects.txt',
    'cache_json': 'gitlab_projects.json',
    'clone_timeout': '300',
    'fetch_timeout': '60',
    'branch_timeout': '30',
    'pull_timeout': '60',
    'max_workers': '8',
    'clone_method': 'auto',  # auto -> git with GITLAB_TOKEN auth when set, else glab, else git
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


# Escape hatch for CI, containers, and anywhere else untrusted checkouts are
# handled in bulk: opt out of the "local config" tier entirely, so a config file
# that happens to sit in (or above) the working directory can never take effect.
# Complements kb/trust.py's provenance gate -- that one drops only the handful of
# argv-reaching keys from a discovered file; this one ignores the file outright.
NO_LOCAL_CONFIG_ENV = 'CONTEXTLAKE_NO_LOCAL_CONFIG'


def local_config_disabled():
    """Whether ``CONTEXTLAKE_NO_LOCAL_CONFIG`` opts out of ancestor discovery.

    Any non-empty value counts except an explicit off word, so ``=1``/``=true``/
    ``=yes`` all work and only ``=0``/``=false``/``=no``/unset keep discovering.
    Erring toward "set means off" is the safe direction for a security opt-out:
    a typo'd value disables a convenience feature rather than silently leaving
    the surface open.

    Note this also moves where ``kb source add --local`` *writes*
    (``kb/config_edit.resolve_write_target`` resolves its target through
    ``find_ancestor_config`` too). That is deliberate rather than an oversight:
    with the local tier disabled, writing a source into a file this environment
    will never read would be the more surprising behavior.
    """
    return os.environ.get(NO_LOCAL_CONFIG_ENV, '').strip().lower() not in ('', '0', 'false', 'no')


def find_ancestor_config(filename, start=None):
    """The nearest ancestor directory's ``filename``, walking from ``start``
    (default: cwd) up through every parent to the filesystem root -- the same
    discovery git uses for ``.git``. ``None`` if no ancestor has it.

    An **absolute** ``filename`` is checked directly, no walking: this is what
    every existing test does to isolate the "local" tier at a specific tmp
    path (``monkeypatch.setattr(module, "LOCAL_CONFIG_FILE", str(tmp_path /
    "..."))``), and an absolute path doesn't have "parent directories" to walk
    in the sense this function means anyway. The opt-out below sits *after* that
    fast path on purpose: it disables directory *discovery*, which is the part
    an untrusted checkout can exploit, and must not break the test seam.
    """
    if os.path.isabs(filename):
        return filename if os.path.exists(filename) else None
    if local_config_disabled():
        return None
    directory = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(directory, filename)
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:  # reached filesystem root
            return None
        directory = parent


def _merge(config, path):
    """Merge an INI file's ``[contextlake]`` section into config, if present."""
    if not path or not os.path.exists(path):
        return
    parser = configparser.ConfigParser()
    parser.read(path)
    for section in SECTIONS:
        if section in parser:
            config.update(parser[section])


def load_config(config_path=None):
    """Load configuration with precedence: explicit --config > local > global > defaults.

    Sources are merged from lowest to highest precedence so the later (more
    specific) source wins on conflicting keys. "Local" is the nearest ancestor
    directory (walking up from cwd) with a ``.contextlake.ini`` -- like a
    project-root config that every subdirectory underneath it inherits.

    An explicit ``--config`` path that doesn't exist is a hard error, not a
    silent no-op: without this, a typo'd or not-yet-created path falls through
    to the next file in the precedence chain -- typically ``~/.contextlake.ini``,
    which can point at a completely different workspace than the one the caller
    meant to target. The other, auto-discovered files in the chain are
    legitimately optional and keep silently no-op'ing when absent. ``ConfigError``
    is shared with ``kb.load_kb_config``, which applies the identical guard to
    kb.toml.
    """
    if config_path and not os.path.exists(expand_path(config_path)):
        raise ConfigError(
            f"--config path not found: {config_path}\n"
            "Refusing to fall back to the next config in the precedence chain "
            "(~/.contextlake.ini, or the nearest ancestor directory's "
            ".contextlake.ini), which may point at a different workspace than "
            "the one you meant to use."
        )
    config = DEFAULT_CONFIG.copy()
    local_config_file = find_ancestor_config(LOCAL_CONFIG_FILE)
    _merge(config, CONFIG_FILE)               # global (~/.contextlake.ini)
    _merge(config, local_config_file)         # nearest ancestor's local config
    _merge(config, config_path)               # explicit --config path

    # INI/CLI values are stored verbatim, so a `work_dir = ~/repos` would
    # otherwise be treated as a literal "~" directory. Expand here.
    for key in PATH_KEYS:
        if key in config:
            config[key] = expand_path(config[key])

    if (config.get('gitlab_group') == DEFAULT_CONFIG['gitlab_group']
            and not config.get('group')):
        # No usable config was found -- show the exact paths searched (absolute)
        # and whether each exists, so a config that lives one directory up (or in
        # the wrong spot entirely) isn't a silent mystery.
        log("WARNING: gitlab_group is still the placeholder 'your-gitlab-group' — "
            "no config with your group was found. Searched (low to high precedence):")
        for path in (CONFIG_FILE, local_config_file, config_path):
            if not path:
                continue
            mark = "found" if os.path.exists(path) else "absent"
            log(f"    [{mark}] {os.path.abspath(path)}")
        if not local_config_file:
            log(f"    [absent] {LOCAL_CONFIG_FILE} (searched this directory and "
                "every parent, up to filesystem root)")
        log("  A local config is the nearest ancestor directory's "
            f"{LOCAL_CONFIG_FILE} -- every subdirectory underneath it inherits it. "
            "Copy .contextlake.ini.example to one of the paths above (or pass "
            "--config PATH) and set gitlab_group.")

    return config


def get_cache_paths(config):
    """Get cache file paths from config."""
    cache_dir = config.get('cache_dir', '/tmp')
    cache_file = config.get('cache_file', 'gitlab_projects.txt')
    cache_json = config.get('cache_json', 'gitlab_projects.json')
    return os.path.join(cache_dir, cache_file), os.path.join(cache_dir, cache_json)
