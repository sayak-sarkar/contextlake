"""
Configuration loading for contextlake
"""

import configparser
import hashlib
import logging
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
    """An explicit --config path was invalid, or a config value is not one the key accepts."""

    # The status the CLI exits with. 1 for a config that is wrong; see WrongConfigFile.
    exit_code = 1


class WrongConfigFile(ConfigError):
    """An explicit --config file is a knowledge config where the mirror INI is read.

    Exits 2, the status ``bootstrap`` gives the same mistake (see ``cli._bootstrap``).
    """

    exit_code = 2


# The spellings configparser's getboolean accepts, so an INI file may write a yes/no key
# the way the rest of the INI world does. Every reader of these keys compares against the
# literal "true", so parse_bool hands back that canonical form.
_BOOLEAN_WORDS = {'true': 'true', 'yes': 'true', 'on': 'true', '1': 'true',
                  'false': 'false', 'no': 'false', 'off': 'false', '0': 'false'}


def parse_bool(key, value, source=None):
    """``'true'`` or ``'false'`` for a yes/no setting, in any spelling configparser accepts.

    Raises :class:`ConfigError` for anything else, naming ``source`` (the file that set the
    value) when it is known. An unrecognised value used to read as false, so
    ``dry_run = yes`` ran for real and ``require_clean_workspace = yes`` turned its guard
    off, both with no message. Refusing the run fails closed instead.
    """
    try:
        return _BOOLEAN_WORDS[str(value).strip().lower()]
    except KeyError:
        where = f" (set in {source})" if source else ""
        raise ConfigError(
            f"{key}: {value!r} is not one of true, false, yes, no, on, off, 1, 0{where}"
        ) from None


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
    # Pin every repository to one named branch. Empty (the default) keeps the
    # most-active selection above. A repository without that branch is REPORTED as
    # not having it and is put on its most active one instead (a repository on a
    # working branch is skipped, as for any branch switch), rather than silently
    # reading as though the request had been carried out.
    'branch': '',
    # Per-repository branch pins, for when one branch does not fit the whole fleet.
    # Comma-separated `pattern=branch` pairs; the pattern is the same glob `repo_filter`
    # uses, matched against the group-qualified path and the local one. First match wins,
    # so list the specific before the general. Falls back to `branch` above, then to the
    # most-active selection. A repository without its mapped branch is reported as such,
    # exactly like `branch`.
    #   branch_map = team/api=develop, legacy-*=maintenance, acme/one-off=spike
    'branch_map': '',
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
    'auto_stash': 'false',
    # --- self-scheduling -------------------------------------------------
    # `auto` lets the tool pick from what it measured; a duration pins it and
    # turns auto-adjust off. See docs/scheduling.md.
    'schedule_interval': 'auto',
    # The most wall-clock time contextlake may occupy. 0.10 means a 7-minute
    # sync earns a 70-minute gap.
    'schedule_duty_cycle': '0.10',
    # Bounds auto-adjust can never leave, however the measurements move.
    'schedule_min': '1h',
    'schedule_max': '24h',
    # How often a full rebuild replaces an incremental run.
    'schedule_full_every': '7d',
    # How far the recommendation must drift before a run rewrites its own unit.
    'schedule_adjust_threshold': '0.5',
    # `skip` or `run`. No effect on a machine with no battery.
    'schedule_on_battery': 'skip',
    # Opt-in. A no-op where user idle cannot be detected.
    'schedule_require_idle': 'false',
    # Skip when the 1-minute load average is above this. Empty means never.
    'schedule_max_load': '',
    # How soon to retry after a gated skip, rather than waiting a full interval.
    'schedule_gate_retry': '10m',
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


# The mirror keys that decide which host receives the forge token, or which
# environment variable is read for it. core._gitlab_api_base reads gitlab_host,
# core._platform_api_base reads api_base, and core._gitlab_token and
# core._platform_token read gitlab_token_env and token_env. The REST client sends
# that variable's value to that host (as PRIVATE-TOKEN for GitLab, Authorization
# for the others), and git sends it to the same host on clone and fetch.
#
# A .contextlake.ini found by walking up from the current directory can arrive
# inside a cloned repository, so it could name any variable in the environment
# and a host to send it to. These keys are honoured only from the global
# ~/.contextlake.ini or a file passed to --config. It is the rule kb/trust.py
# applies to kb.toml's base_url and api_key_env.
#
# `platform` stays ungated. Each platform's built-in host and token variable are
# fixed, so it moves between the forges' own hosts and never names a new one.
# work_dir, gitlab_group and platform are what an honest project-local file sets,
# and all that `contextlake init --local` writes.
FORGE_CREDENTIAL_KEYS = ('gitlab_host', 'api_base', 'token_env', 'gitlab_token_env')

# The mirror keys that decide where files are written. get_cache_paths joins them into the
# path of the project-list cache, and core._write_caches opens that path for writing and
# truncates it. `os.path.join` keeps an absolute second part and any `..`, and `cache_dir`
# can be any directory, so a planted `cache_json = /home/me/.gitconfig` (or `cache_dir =
# /home/me` with `cache_json = .gitconfig`) replaced a file the user did not choose with
# the JSON project list. The audit report and the schedule history sit in the same
# directory. Like FORGE_CREDENTIAL_KEYS, these are honoured only from the global
# ~/.contextlake.ini or a file passed to --config. They do not touch the forge token.
CACHE_LOCATION_KEYS = ('cache_dir', 'cache_file', 'cache_json')

# Set in the merged config when a refused key leaves a built-in default in its
# place. core reads it and sends no forge token for the run. Dropping the key is
# not enough on its own: a refused `gitlab_host = gitlab.corp` falls back to
# gitlab.com, so the token meant for gitlab.corp goes to gitlab.com, and a refused
# `token_env = PROJECT_TOKEN` falls back to GITLAB_TOKEN, a broader secret.
# kb/trust.py switches an LLM tier off for the same reason. A refused key that the
# global file or --config also sets falls back to that value instead, which the
# user chose, so it leaves the token on.
FORGE_TOKEN_REFUSED = '_forge_token_refused'  # noqa: S105 - a config key name, not a secret

# Set in the merged config: which file set each key, as {key: path}. apply_cli_overrides
# checks the yes/no keys after the merge, and a bad value there has to name its file. Like
# FORGE_TOKEN_REFUSED it is set only here and never read from a file.
CONFIG_ORIGIN = '_config_origin'


# Tables a knowledge config (kb.toml) has and a mirror INI does not. A file with a header for
# one of them, or one that ends in .toml, is a kb.toml handed to a command that reads the INI.
_KB_TABLES = ('kb', 'embeddings', 'llm', 'sources', 'rules')


def _wrong_kind(path):
    """The error for a kb.toml given where the mirror INI is read, or None if it is not one.

    Read from the raw text, because a kb.toml with two ``[[sources]]`` blocks does not parse
    as an INI: the section name repeats. A multi-line array whose closing bracket is at
    column 0 does not parse either. A ``[contextlake]`` header means a mirror INI, whatever
    the file is called, so a broken one gets its own parse error.
    """
    try:
        # utf-8-sig, as the loader reads the file: a BOM in front of the first header is not
        # whitespace to the regex below and would hide a leading [contextlake] header.
        with open(path, encoding='utf-8-sig', errors='replace') as f:
            text = f.read()
    except OSError:
        text = ''
    if re.search(r'^\s*\[contextlake\]', text, re.MULTILINE):
        return None
    if not path.lower().endswith('.toml'):
        headers = re.findall(r'^\s*\[+\s*([^\]\s]+)\s*\]+', text, re.MULTILINE)
        if not any(h.lower() in _KB_TABLES for h in headers):
            return None
    return WrongConfigFile(
        f"--config {path} looks like a knowledge config, but this command reads "
        "the mirror INI.\n  A mirror INI has a [contextlake] section. Give a "
        "kb.toml to a `kb` command with --config, or to `bootstrap` with --kb-config.")


# What a configparser failure means for the person who edited the file, by class name.
_INI_FAULTS = {
    'MissingSectionHeaderError': 'a setting comes before any [contextlake] header',
    'DuplicateOptionError': 'a key is set twice in one section',
    'DuplicateSectionError': 'a section appears twice',
    'ParsingError': 'a line is not "key = value"',
}


def _ini_error(path, exc):
    """A :class:`ConfigError` for an INI file configparser cannot parse, naming the file.

    These used to leave ``load_config`` as a raw traceback. Names the file, the line and
    the type of fault. Never ``str(exc)``: the parse errors quote the offending line, and
    a config file's values are not echoed (see _warn_untrusted_forge_key).
    """
    lineno = getattr(exc, 'lineno', None)
    if not lineno and getattr(exc, 'errors', None):
        lineno = exc.errors[0][0]
    at = f" (line {lineno})" if lineno else ""
    why = _INI_FAULTS.get(type(exc).__name__, "it is not a valid INI file")
    return ConfigError(f"Cannot read the config file {path}{at}: {why}.")


def _read_section(path, explicit=False):
    """An INI file's ``[contextlake]`` section as a dict, or ``{}`` if absent.

    Includes ``[DEFAULT]`` keys, as configparser's section view does, so a gated
    key cannot slip past the gate by sitting under ``[DEFAULT]``. configparser
    lowercases key names, so ``GITLAB_HOST = ...`` is the same key too.

    A file the user named (``explicit``) must be readable and must have the section,
    or :class:`ConfigError` says which. Section names are case-sensitive, so a
    ``[ContextLake]`` header used to be read as an empty file, and the run went on with
    the defaults. An auto-discovered file that cannot be opened is still skipped.
    """
    if not path or not os.path.exists(path):
        return {}
    # interpolation=None: a value is literal text. The default treats `%` as syntax, so
    # `work_dir = /srv/50%off`, which `contextlake init` writes unescaped, made every
    # later mirror command raise InterpolationSyntaxError.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        # UTF-8, the encoding `contextlake init` writes. Without it the file is read in the
        # locale's encoding, which is not UTF-8 on Windows or under LC_ALL=C, and a
        # `work_dir` with an accented character decoded to a different path. `utf-8-sig`
        # reads the same text and also drops the BOM that some Windows editors add.
        with open(path, encoding='utf-8-sig') as f:
            parser.read_file(f, path)
    except OSError as e:
        if explicit:
            raise ConfigError(f"--config file cannot be read: {path} ({e.strerror or e})") from e
        return {}
    except UnicodeDecodeError:
        wrong = _wrong_kind(path) if explicit else None
        raise (wrong or ConfigError(f"Cannot read the config file {path}: it is not valid "
                                    "UTF-8. Save it as UTF-8.")) from None
    except configparser.Error as e:
        wrong = _wrong_kind(path) if explicit else None
        raise (wrong or _ini_error(path, e)) from None
    found = [section for section in SECTIONS if section in parser]
    if explicit and not found:
        have = ', '.join(parser.sections()) or 'none'
        wrong = _wrong_kind(path)
        if wrong:
            raise wrong
        raise ConfigError(
            f"--config file has no [contextlake] section: {path}\n"
            f"  Sections found: {have}. Section names are case-sensitive.\n"
            "Refusing to fall back to the defaults or to another config file in the "
            "precedence chain, which may point at a different workspace than the one you "
            "meant to use.")
    values = {}
    for section in found:
        values.update(parser[section])
    return values


def _same_file(a, b):
    """Whether two config paths name the same file.

    Both sides get ``~``/``$VAR`` expansion and ``realpath``. ``CONFIG_FILE`` is
    absolute, the discovered path is absolute, and ``--config`` is whatever the
    user typed, so a raw string compare would miss a real match.
    """
    if not a or not b:
        return False
    return os.path.realpath(expand_path(a)) == os.path.realpath(expand_path(b))


def _warn_untrusted_forge_key(key, source):
    """Report one refused key by name and file. Never the value: it is text the
    file's author chose, and it can name a secret's variable."""
    hint = (" The GITLAB_HOST environment variable also sets the host."
            if key == 'gitlab_host' else "")
    log(f"config: ignoring {key} from {source} -- a config file found by walking up "
        "from the current directory may not choose which host receives the forge "
        "token, or which environment variable holds it. "
        f"Set it in {CONFIG_FILE} instead, or pass `--config {source}` to say you "
        f"meant this file.{hint}", level=logging.WARNING)


def _warn_untrusted_cache_key(key, source):
    """Report one refused cache key by name and file. Never the value: it is text the
    file's author chose, and it can be a path."""
    log(f"config: ignoring {key} from {source} -- a config file found by walking up "
        "from the current directory may not choose where the project cache is written. "
        f"Set it in {CONFIG_FILE} instead, or pass `--config {source}` to say you "
        "meant this file.", level=logging.WARNING)


def _warn_forge_token_off(keys, source):
    """Report that the run sends no forge token, and what turns it back on."""
    names = ', '.join(keys)
    that, its = ("that key", "its") if len(keys) == 1 else ("those keys", "their")
    log(f"config: the forge token is off for this run. {source} set {names}, and no "
        f"file you chose sets {that} instead, so a built-in default would take {its} "
        "place: the platform's public host, or its standard token variable. Clones "
        f"and fetches run without the token. To use it here, delete {that} from "
        f"{source} and set {that} in {CONFIG_FILE}, or pass `--config {source}`.",
        level=logging.WARNING)


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
    kb.toml. An explicit file that exists but cannot be read, or has no
    ``[contextlake]`` section, is the same hard error: it used to be read as an empty
    file, and the run went on with the defaults.

    The discovered local file may not set :data:`FORGE_CREDENTIAL_KEYS`. They
    are dropped with a warning naming the key and the file, unless that file is
    also the global one or the ``--config`` one. When a dropped key leaves a
    built-in default in its place, :data:`FORGE_TOKEN_REFUSED` is set and the
    run sends no forge token. :data:`CACHE_LOCATION_KEYS` are dropped the same
    way, and the token stays on.
    """
    if config_path and not os.path.exists(expand_path(config_path)):
        raise ConfigError(
            f"--config path not found: {config_path}\n"
            "Refusing to fall back to the next config in the precedence chain "
            "(~/.contextlake.ini, or the nearest ancestor directory's "
            ".contextlake.ini), which may point at a different workspace than "
            "the one you meant to use."
        )
    # A `~` or `$VAR` that the shell did not expand (`--config=~/x.ini`, a quoted value, a
    # systemd line) is expanded for the check above, so it must be expanded for the read.
    config_path = expand_path(config_path)
    config = DEFAULT_CONFIG.copy()
    local_config_file = find_ancestor_config(LOCAL_CONFIG_FILE)
    global_values = _read_section(CONFIG_FILE)
    local_values = _read_section(local_config_file)
    explicit_values = _read_section(config_path, explicit=True)
    # Provenance gate. Run from the home directory, the ancestor walk finds
    # ~/.contextlake.ini itself, and `--config ./.contextlake.ini` names the
    # discovered file. Both are files the user chose, so both keep every key.
    refused = []
    if not (_same_file(local_config_file, CONFIG_FILE)
            or _same_file(local_config_file, config_path)):
        refused = [k for k in FORGE_CREDENTIAL_KEYS if k in local_values]
        for key in refused:
            del local_values[key]
            _warn_untrusted_forge_key(key, local_config_file)
        for key in CACHE_LOCATION_KEYS:
            if key in local_values:
                del local_values[key]
                _warn_untrusted_cache_key(key, local_config_file)
    config.update(global_values)              # global (~/.contextlake.ini)
    config.update(local_values)               # nearest ancestor's local config
    config.update(explicit_values)            # explicit --config path
    config[CONFIG_ORIGIN] = {key: path for path, values in (
        (CONFIG_FILE, global_values), (local_config_file, local_values),
        (config_path, explicit_values)) for key in values}

    # Set only here, never from a file.
    config.pop(FORGE_TOKEN_REFUSED, None)
    chosen = set(global_values) | set(explicit_values)
    if os.environ.get('GITLAB_HOST'):
        chosen.add('gitlab_host')  # the env var wins over every file (core._gitlab_api_base)
    defaulted = [k for k in refused if k not in chosen]
    if defaulted:
        config[FORGE_TOKEN_REFUSED] = ','.join(defaulted)
        _warn_forge_token_off(defaulted, local_config_file)

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
        log("WARNING: gitlab_group is still the placeholder 'your-gitlab-group': "
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
