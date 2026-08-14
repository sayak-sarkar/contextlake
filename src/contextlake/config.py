"""
Configuration loading for contextlake
"""

import configparser
import hashlib
import os
import re

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


# Where the forge-project cache lands when nothing configures it.
#
# This file lists every repository the account can enumerate, together with its
# clone URLs, so its location is a privacy decision rather than a scratch-file
# one. It used to default to `/tmp`, which is wrong on three counts: it sits
# outside the user's home, so no HOME-based isolation (container, test harness,
# `sudo -H`) reaches it; it is world-readable on a shared host; and its path is
# fully predictable, so another user can pre-create a file or symlink there
# before contextlake ever runs. `/tmp`'s sticky bit narrows that last window,
# it does not close it.
#
# Stored UNEXPANDED on purpose: DEFAULT_CONFIG is a module-level literal, so an
# expanded value would freeze this process's HOME at import time and no later
# override could move it. `~` is resolved per run instead -- by load_config's
# PATH_KEYS loop, and by _default_cache_root() below.
DEFAULT_CACHE_DIR = '~/.cache/contextlake'

# Default Configuration
DEFAULT_CONFIG = {
    'work_dir': os.path.expanduser('~/work'),
    # Which platform `fetch` enumerates: gitlab (default) | github | bitbucket |
    # gitea (codeberg/forgejo are gitea flavors). `group` is the generic key for
    # the org/workspace/owner to mirror; gitlab_group remains as its alias.
    'platform': 'gitlab',
    'gitlab_group': 'your-gitlab-group',
    'cache_dir': DEFAULT_CACHE_DIR,
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


def load_config(config_path=None, cli_group=None):
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

    # `cli_group` is `--group` as typed on the command line. It is merged into the
    # effective group AFTER this function returns, so without it this warning fired on
    # every single `--group` invocation of every mirror command -- telling a user who
    # had just supplied the group that no group was found. A warning that is wrong on a
    # correct invocation trains people to ignore warnings.
    if (config.get('gitlab_group') == DEFAULT_CONFIG['gitlab_group']
            and not config.get('group') and not cli_group):
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


def _default_cache_root():
    """The contextlake-owned cache root: ``$XDG_CACHE_HOME/contextlake`` when
    that variable is set, else ``~/.cache/contextlake``.

    Resolved on every call rather than at import so ``HOME``/``XDG_CACHE_HOME``
    stay overridable -- the test harness, containers, and ``sudo -H`` all set
    them after this module is imported.
    """
    xdg = os.environ.get('XDG_CACHE_HOME', '').strip()
    if xdg:
        return os.path.join(expand_path(xdg), 'contextlake')
    return expand_path(DEFAULT_CACHE_DIR)


def _is_default_cache_dir(configured):
    """Whether ``configured`` is (still) the built-in default rather than a
    directory the user named. Both spellings count, since load_config expands
    PATH_KEYS in place and callers that build a config dict by hand do not."""
    if not configured:
        return True
    return configured in (DEFAULT_CACHE_DIR, expand_path(DEFAULT_CACHE_DIR))


def _workspace_slug(config):
    """A short, stable directory name identifying *this* workspace's cache.

    Keyed on the workspace directory plus the platform and group being mirrored,
    because those three are exactly what decide which repositories the cache
    describes: two runs that would disagree about its contents can then never
    share a file. Before this, one global ``<cache_dir>/gitlab_projects.txt``
    was read and overwritten by every config in a directory nest and by every
    unrelated workspace on the machine, so a directory-scoped config was never
    actually isolated and ``mirror status`` could report another workspace's
    fleet as this one's.

    The readable prefix is for a human listing the cache root; the digest is
    what makes the name unique.
    """
    work_dir = os.path.realpath(expand_path(config.get('work_dir') or '.'))
    platform = (config.get('platform') or 'gitlab').strip().lower()
    group = (config.get('group') or config.get('gitlab_group') or '').strip()
    digest = hashlib.sha256(
        '\0'.join((work_dir, platform, group)).encode('utf-8')).hexdigest()[:12]
    name = re.sub(r'[^A-Za-z0-9._-]', '-', os.path.basename(work_dir)) or 'workspace'
    return f"{name[:32]}-{digest}"


def _ensure_cache_dir(path, *, owned):
    """Create the cache directory. ``owned`` means contextlake chose the path
    (the default location), so it is also tightened to 0700 -- the cache names
    every repository the account can reach, and ``makedirs``' own ``mode`` is
    masked by umask and ignored outright for a directory that already exists.
    A directory the user explicitly configured is only created, never
    re-permissioned: silently chmod-ing a path they pointed us at (a shared
    cache, ``/tmp``) is a side effect nobody asked for.

    Never raises. This also runs on read paths, and a cache directory that
    cannot be prepared should surface as the real read/write error from the
    code actually touching the file, not as a mkdir traceback from a path
    helper.
    """
    try:
        os.makedirs(path, exist_ok=True)
        if owned:
            for directory in (os.path.dirname(path), path):
                os.chmod(directory, 0o700)
    except OSError as e:
        log(f"WARNING: could not prepare the cache directory {path}: {e}")


def get_cache_paths(config):
    """Get cache file paths from config, preparing the directory that holds them.

    With no ``cache_dir`` configured the cache goes under the user's own cache
    root, in a per-workspace subdirectory (see :func:`_workspace_slug`). An
    explicitly configured ``cache_dir`` is used verbatim, with no subdirectory:
    naming the directory is the user saying "put it exactly here" -- some point
    several workspaces at one shared cache deliberately, and the audit report
    documented at ``<cache_dir>/repo_audit.json`` is written alongside these.
    """
    configured = config.get('cache_dir')
    owned = _is_default_cache_dir(configured)
    cache_dir = (os.path.join(_default_cache_root(), _workspace_slug(config)) if owned
                 else expand_path(configured))
    _ensure_cache_dir(cache_dir, owned=owned)
    cache_file = config.get('cache_file', 'gitlab_projects.txt')
    cache_json = config.get('cache_json', 'gitlab_projects.json')
    return os.path.join(cache_dir, cache_file), os.path.join(cache_dir, cache_json)
