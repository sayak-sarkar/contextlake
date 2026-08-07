"""
Core git operations for contextlake
"""

import base64
import json
import os
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from . import observability, style
from .config import get_cache_paths
from .logging_setup import log
from .safety import check_repository_safety, is_safe_branch, restore_stash, stash_changes


def _status(i, total, state, path, message):
    """A coloured per-repo progress line: dim counter, state glyph, cyan path.

    Thin wrapper over the shared ``style.status_line`` vocabulary -- ``state``
    is one of the state names it recognises (ok/warn/fail/skip/nochange/
    switched/dryrun), not a hand-built glyph.
    """
    return style.status_line(i, total, state, path, message)


def _is_truthy(config, key, default="false"):
    """Return True when a string-valued config flag is set to 'true'."""
    return str(config.get(key, default)).strip().lower() == "true"


def _int(config, key, default):
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _float(config, key, default):
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return float(default)


@dataclass(frozen=True)
class StageResult:
    """What one mirror stage did, in counts its caller can act on.

    Every stage used to return None, so `mirror sync` exited 0 even when every
    single clone failed -- an unattended run (examples/contextlake.service and
    .timer) could never trip systemd's OnFailure=, and a fleet that silently
    stopped mirroring looked identical to a healthy one. Stages add together so
    `sync` decides on the whole pipeline's total rather than its last stage.

    ``skipped`` covers work deliberately not done (already up to date, protected
    branch, dry run): counted for the record, never a failure.
    """

    ok: int = 0
    failed: int = 0
    skipped: int = 0

    def __add__(self, other: "StageResult") -> "StageResult":
        return StageResult(self.ok + other.ok,
                           self.failed + other.failed,
                           self.skipped + other.skipped)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


def to_local_path(path_with_namespace, gitlab_group):
    """Map a GitLab ``path_with_namespace`` to its local path.

    Local clones mirror the namespace tree *below* the configured group, so the
    leading ``<group>/`` prefix is stripped (e.g. ``acme/team/api`` -> ``team/api``
    when the group is ``acme``). Paths outside the group are returned unchanged.
    """
    prefix = gitlab_group.strip("/") + "/"
    if path_with_namespace.startswith(prefix):
        return path_with_namespace[len(prefix):]
    return path_with_namespace


_SCP_LIKE = re.compile(r"^[\w.-]+@([\w.-]+):(.+)$")


def _remote_namespace(url):
    """A remote URL's ``group/sub/project`` path: host, scheme, credentials and a
    trailing ``.git`` removed, lowercased. ``None`` if there is no path to read."""
    url = (url or "").strip()
    if not url:
        return None
    m = _SCP_LIKE.match(url)          # git@host:group/project.git
    path = m.group(2) if m else urllib.parse.urlsplit(url).path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path.lower() or None


def read_origin_url(repo_dir):
    """The ``origin`` remote URL recorded in a clone's ``.git/config``, or None.

    Read from the file rather than asked of ``git``, deliberately. ``verify`` and
    ``status`` promise to be read-only and fast over a whole fleet, and spawning
    one ``git remote get-url`` per repository would put hundreds of processes
    behind two commands whose entire job is to print a summary. The config file is
    where git itself keeps this, and reading it is a single small open().

    Hand-parsed rather than fed to ``configparser`` because git's config format is
    only INI-ish: it permits repeated keys, tabs before names, and section headers
    like ``[remote "origin"]`` that a strict parser rejects outright. A parse
    failure here must degrade to "unknown", never raise.
    """
    cfg = os.path.join(repo_dir, ".git", "config")
    try:
        with open(cfg, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        # Includes the gitlink case (a submodule/worktree `.git` FILE, so
        # `.git/config` is not a path) -- unknown, which callers treat as
        # in-scope rather than guessing.
        return None
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
        elif section == 'remote "origin"' and "=" in s and not s.startswith("#"):
            key, _, value = s.partition("=")
            if key.strip().lower() == "url":
                return value.strip()
    return None


def belongs_to_other_group(work_dir, local_path, gitlab_group):
    """True only when this clone's origin says it came from a DIFFERENT group.

    A workspace legitimately holds several groups: `to_local_path` strips the
    ``<group>/`` prefix, so a clone of ``group-a/team/api`` and one of
    ``group-b/team/api`` both live at ``team/api`` and the path on disk cannot
    answer which group a repository came from. The origin remote can, and it is
    the only thing that can.

    Deliberately one-sided. An unreadable config, a clone with no origin, a URL
    with no path: all return False, so they keep being reported exactly as before.
    Only a positively-attributed foreign repository is excluded from this run's
    scope, which is the difference between narrowing a report and suppressing it.
    """
    group = (gitlab_group or "").strip("/").lower()
    if not group:
        return False
    ns = _remote_namespace(read_origin_url(os.path.join(work_dir, local_path)))
    if not ns:
        return False
    return not (ns == group or ns.startswith(group + "/"))


def in_group_repos(work_dir, local_paths, gitlab_group):
    """``(this group's local repos, the count attributed to another group)``."""
    scoped, foreign = [], 0
    for p in local_paths:
        if belongs_to_other_group(work_dir, p, gitlab_group):
            foreign += 1
        else:
            scoped.append(p)
    return scoped, foreign


def classify_error(error_msg):
    """Classify a git/network error to drive the retry strategy.

    Transient categories (network/timeout) are retried. The rest fail fast:
    dns/tls won't recover on retry, and the three expected "the remote moved"
    states are not errors to retry but states to report -- ``missing-ref`` (the
    upstream branch was deleted), ``project-deleted`` (the whole upstream project
    was deleted or the token lost access to it), and ``diverged`` (local and
    remote both moved).
    """
    error_msg = error_msg.lower()
    # 'eof' is checked first so a "TLS ... unexpected eof" (a dropped connection,
    # not a cert failure) is treated as transient/network rather than tls.
    if 'eof' in error_msg or 'connection reset' in error_msg or 'broken pipe' in error_msg:
        return 'network'
    if "couldn't find remote ref" in error_msg or 'unknown revision' in error_msg:
        return 'missing-ref'
    # GitLab returns this exact wording for both a deleted project and one the
    # token no longer has access to -- indistinguishable from a fetch failure,
    # and either way there is nothing to retry or fast-forward.
    if ('could not be found' in error_msg and "don't have permission" in error_msg):
        return 'project-deleted'
    if ('not possible to fast-forward' in error_msg or 'divergent branches' in error_msg
            or 'have divergent' in error_msg):
        return 'diverged'
    if 'timeout' in error_msg or 'timed out' in error_msg:
        return 'timeout'
    if 'lookup' in error_msg or 'dns' in error_msg:
        return 'dns'
    if 'tls' in error_msg or 'ssl' in error_msg or 'handshake' in error_msg:
        return 'tls'
    return 'other'


def git_error_is_transient(error):
    """Whether a *git/glab* failure is worth another attempt.

    The mirror tier's failures arrive as text on stderr, so the decision is made
    by classifying that string (see :func:`classify_error`): network/timeout are
    transient, while DNS, TLS, deleted-upstream and diverged-branch are states to
    report rather than retry.
    """
    non_transient = ('dns', 'tls', 'missing-ref', 'project-deleted', 'diverged')
    return classify_error(str(error)) not in non_transient


def retry_with_backoff(func, *args, max_retries=3, backoff_initial=1, backoff_max=30,
                       is_transient=None, **kwargs):
    """Retry ``func`` with exponential backoff and jitter.

    Network/timeout/transient errors are retried; DNS, TLS, deleted-upstream and
    diverged-branch errors are non-transient and fail fast. The last error is
    re-raised on exhaustion.

    ``is_transient(exc) -> bool`` overrides that judgement so the *same* retry
    loop can serve callers whose failures don't look like git's. It exists for
    ``contextlake.kb.resilience``, which drives the knowledge layer's HTTP/MCP
    calls: those fail with a *typed* exception (``HTTPError``, a timeout) that
    :func:`classify_error`'s string matching cannot read, and their retry policy
    genuinely differs (a call that already burned its full timeout budget is not
    worth repeating). Defaults to :func:`git_error_is_transient`, so nothing in
    the mirror tier changes -- and the primitive stays here, in the stdlib-only
    core tier, because core must never import kb (kb is an optional extra).
    """
    decide_transient = is_transient or git_error_is_transient
    last_error = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except FileNotFoundError:
            raise  # a missing binary/path never recovers on retry
        except Exception as e:
            last_error = e
            if not decide_transient(e):
                break
            if attempt < max_retries - 1:
                backoff = min(backoff_initial * (2 ** attempt), backoff_max)
                time.sleep(backoff * random.uniform(0.5, 1.5))  # noqa: S311 - backoff jitter
    raise last_error


class AdaptiveWorkerPool:
    """Tracks a sliding error-rate window and recommends a worker count.

    Used to throttle parallelism down when a sync run starts failing (e.g. the
    network or GitLab is struggling) and ramp it back up as things recover.
    """

    def __init__(self, max_workers, min_workers, error_threshold):
        self.max_workers = max_workers
        self.min_workers = min_workers
        self.error_threshold = error_threshold
        self.current_workers = max_workers
        self.recent_results = []
        self.window_size = 10

    def record_result(self, success):
        """Record an outcome and adjust the recommended worker count."""
        self.recent_results.append(bool(success))
        if len(self.recent_results) > self.window_size:
            self.recent_results.pop(0)

        if len(self.recent_results) >= self.window_size:
            error_rate = 1 - (sum(self.recent_results) / len(self.recent_results))
            if error_rate > self.error_threshold and self.current_workers > self.min_workers:
                self.current_workers = max(self.min_workers, self.current_workers - 1)
                log(f"Reducing workers to {self.current_workers} (error rate: {error_rate:.2%})")
            elif error_rate < self.error_threshold / 2 and self.current_workers < self.max_workers:
                self.current_workers = min(self.max_workers, self.current_workers + 1)
                log(f"Increasing workers to {self.current_workers} (error rate: {error_rate:.2%})")

    def get_worker_count(self):
        return self.current_workers


# ---------------------------------------------------------------------------
# Fetch / cache
# ---------------------------------------------------------------------------

DEFAULT_GITLAB_HOST = "gitlab.com"


def configure_network_resilience(config):
    """Make child git/glab DNS lookups tolerant of slow corporate resolvers.

    Some networks (notably a TLS-inspecting proxy like Zscaler) answer DNS for
    the GitLab host in several seconds. glibc's resolver gives up after its
    default ``timeout`` x ``attempts`` budget, so git operations intermittently
    fail with "i/o timeout". Widen that budget for this process tree via
    ``RES_OPTIONS`` -- root-free, and only when the user hasn't set it. (This
    does not lift glab's own short Go dial timeout; project enumeration sidesteps
    that by using the native HTTP client below when a token is available.)
    """
    if not os.environ.get("RES_OPTIONS"):
        timeout = config.get("dns_timeout", "15")
        attempts = config.get("dns_attempts", "3")
        os.environ["RES_OPTIONS"] = f"timeout:{timeout} attempts:{attempts}"


# ---------------------------------------------------------------------------
# Platform seam. Every supported platform's enumerator normalizes its listing
# to the same project dict (path_with_namespace / clone URLs / default_branch /
# archived / timestamps), so clone/update/branches/verify/status/audit stay
# platform-agnostic -- they only ever see the cache.
# ---------------------------------------------------------------------------

PLATFORM_DEFAULTS = {
    # api_base: REST root · token_env: where the token is read from ·
    # clone_user: the basic-auth username git-over-HTTPS expects with a token ·
    # per_page: the platform's real page-size cap (termination depends on it)
    "gitlab": {"api_base": "https://gitlab.com", "token_env": "GITLAB_TOKEN",
               "clone_user": "oauth2", "per_page": 100},
    "github": {"api_base": "https://api.github.com", "token_env": "GITHUB_TOKEN",
               "clone_user": "x-access-token", "per_page": 100},
    "bitbucket": {"api_base": "https://api.bitbucket.org/2.0",
                  "token_env": "BITBUCKET_TOKEN",
                  "clone_user": "x-token-auth", "per_page": 100},
    "gitea": {"api_base": "https://gitea.com", "token_env": "GITEA_TOKEN",
              "clone_user": "oauth2", "per_page": 50},
}
# How each forge spells its own name. One source of truth, because the banner,
# the enumerator and the failure message used to be able to disagree with each
# other in a single run: a config resolving to github printed "Github group",
# called api.github.com, and then reported that it "could not enumerate GitLab
# projects" -- three different answers to "which forge is this?".
PLATFORM_LABELS = {"gitlab": "GitLab", "github": "GitHub",
                   "bitbucket": "Bitbucket", "gitea": "Gitea"}
# Hosted flavors that speak an existing platform's API verbatim.
_PLATFORM_ALIASES = {"codeberg": "gitea", "forgejo": "gitea"}
# Alias -> its canonical hosted endpoint (used only when no api_base is set).
_ALIAS_API_BASE = {"codeberg": "https://codeberg.org", "forgejo": "https://gitea.com"}


def platform_name(config) -> str:
    """The canonical platform key from config (default gitlab). Raises on unknown."""
    raw = (config.get("platform") or "gitlab").strip().lower()
    name = _PLATFORM_ALIASES.get(raw, raw)
    if name not in PLATFORM_DEFAULTS:
        raise FetchError(
            f"unknown platform {raw!r} -- expected one of "
            f"{sorted(set(PLATFORM_DEFAULTS) | set(_PLATFORM_ALIASES))}")
    return name


def platform_label(config) -> str:
    """How the configured forge spells its own name ("GitLab", "GitHub", ...),
    for anything a person reads. Falls back to the raw key for an unknown
    platform, since reporting *that* is the caller's job, not this helper's."""
    try:
        name = platform_name(config)
    except FetchError:
        return (config.get("platform") or "gitlab").strip().lower()
    return PLATFORM_LABELS.get(name, name)


def _platform_token(config):
    """The API token for the configured platform, from ``token_env`` (config) or
    the platform's default env var. None when unset; read only here, never logged."""
    name = platform_name(config)
    if name == "gitlab":
        return _gitlab_token(config)
    env_name = config.get("token_env") or PLATFORM_DEFAULTS[name]["token_env"]
    return os.environ.get(env_name) or None


def _platform_api_base(config):
    """REST root for the configured platform (config ``api_base`` wins; the
    codeberg/forgejo aliases resolve to their hosted endpoints)."""
    name = platform_name(config)
    if name == "gitlab":
        return _gitlab_api_base(config)
    configured = (config.get("api_base") or "").strip().rstrip("/")
    if configured:
        return configured if configured.startswith(("http://", "https://")) \
            else f"https://{configured}"
    raw = (config.get("platform") or "").strip().lower()
    return _ALIAS_API_BASE.get(raw, PLATFORM_DEFAULTS[name]["api_base"])


def _gitlab_token(config):
    """The GitLab API token from the configured env var (default GITLAB_TOKEN).

    Returns None when unset -- callers then fall back to the ``glab`` CLI, which
    carries its own auth. Read only here; never logged.
    """
    env_name = config.get("gitlab_token_env") or config.get("token_env") or "GITLAB_TOKEN"
    return os.environ.get(env_name) or os.environ.get("GITLAB_TOKEN") or None


def _gitlab_api_base(config):
    """Base ``https://host`` for the GitLab REST API (GITLAB_HOST / config / default)."""
    host = (os.environ.get("GITLAB_HOST") or config.get("gitlab_host")
            or DEFAULT_GITLAB_HOST).strip().rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    return f"https://{host}"


def _projects_endpoint(group_enc, per_page, page):
    return (f"groups/{group_enc}/projects"
            f"?include_subgroups=true&archived=false&per_page={per_page}&page={page}")


def _fetch_projects_page_http(base_url, group_enc, token, per_page, timeout, page):
    """One page of a group's projects via the GitLab REST API (native HTTP).

    Used instead of the ``glab`` CLI so a slow corporate DNS that exceeds glab's
    short dial timeout still succeeds (Python's resolver budget is more generous).
    Raises on HTTP/network error so the caller's retry/backoff can engage.
    """
    url = f"{base_url}/api/v4/{_projects_endpoint(group_enc, per_page, page)}"
    req = urllib.request.Request(  # noqa: S310 - URL from trusted config
        url, headers={"PRIVATE-TOKEN": token, "User-Agent": "contextlake"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL from trusted config
        return json.loads(resp.read().decode())


def _fetch_projects_page_glab(group_enc, per_page, page):
    """One page via the ``glab`` CLI (uses glab's own auth). Raises on failure."""
    # timeout so a stalled read (not just glab's own dial timeout) surfaces as an
    # exception the retry_with_backoff wrapper can rescue, rather than hanging fetch.
    result = subprocess.run(
        ["glab", "api", _projects_endpoint(group_enc, per_page, page)],
        capture_output=True, text=True, errors="replace", timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "glab api failed")
    return json.loads(result.stdout)


def _get_json(url, headers, timeout):
    """GET a JSON document. Raises on HTTP/network error so retry can engage."""
    req = urllib.request.Request(url, headers={"User-Agent": "contextlake", **headers})  # noqa: S310 - URL from trusted config
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL from trusted config
        return json.loads(resp.read().decode())


def _norm_project(full_name, http, ssh, archived, default_branch, created, activity):
    """Normalize any platform's repo listing entry to the GitLab-shaped dict the
    fetch loop (and therefore the whole downstream pipeline) consumes."""
    return {
        "path_with_namespace": full_name,
        "http_url_to_repo": http or "",
        "ssh_url_to_repo": ssh or "",
        "archived": bool(archived),
        "default_branch": default_branch or "main",
        "created_at": created,
        "last_activity_at": activity,
    }


def _fetch_projects_page_github(base, owner, token, per_page, timeout, page):
    """One page of a GitHub org's (or user's) repos, normalized. Tokenless works
    for public owners (rate-limited); a token unlocks private repos."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        rows = _get_json(f"{base}/orgs/{owner}/repos?type=all"
                         f"&per_page={per_page}&page={page}", headers, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # Not an org -- a user account. Same shape, different endpoint.
        rows = _get_json(f"{base}/users/{owner}/repos"
                         f"?per_page={per_page}&page={page}", headers, timeout)
    return [_norm_project(r.get("full_name"), r.get("clone_url"), r.get("ssh_url"),
                          r.get("archived", False), r.get("default_branch"),
                          r.get("created_at"), r.get("pushed_at")) for r in rows]


def _fetch_projects_page_bitbucket(base, workspace, token, per_page, timeout, page):
    """One page of a Bitbucket workspace's repositories, normalized."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        data = _get_json(f"{base}/repositories/{workspace}"
                         f"?pagelen={per_page}&page={page}", headers, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404 and page > 1:
            return []  # Bitbucket 404s past the last page; that IS the empty page
        raise
    values = data.get("values", []) if isinstance(data, dict) else []
    out = []
    for r in values:
        clones = {c.get("name"): c.get("href") for c in r.get("links", {}).get("clone", [])}
        out.append(_norm_project(
            r.get("full_name"), clones.get("https"), clones.get("ssh"),
            False,  # Bitbucket Cloud has no archived flag
            (r.get("mainbranch") or {}).get("name"),
            r.get("created_on"), r.get("updated_on")))
    return out


def _fetch_projects_page_gitea(base, owner, token, per_page, timeout, page):
    """One page of a Gitea/Forgejo (incl. Codeberg) org's or user's repos."""
    headers = {"Authorization": f"token {token}"} if token else {}
    try:
        rows = _get_json(f"{base}/api/v1/orgs/{owner}/repos"
                         f"?limit={per_page}&page={page}", headers, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        rows = _get_json(f"{base}/api/v1/users/{owner}/repos"
                         f"?limit={per_page}&page={page}", headers, timeout)
    return [_norm_project(r.get("full_name"), r.get("clone_url"), r.get("ssh_url"),
                          r.get("archived", False), r.get("default_branch"),
                          r.get("created_at"), r.get("updated_at")) for r in rows]


_PLATFORM_FETCHERS = {
    "github": _fetch_projects_page_github,
    "bitbucket": _fetch_projects_page_bitbucket,
    "gitea": _fetch_projects_page_gitea,
}


def repo_filter_patterns(config) -> list[str]:
    """Comma-separated ``--repos`` / ``repo_filter`` patterns, or [] if unset."""
    raw = (config.get("repo_filter") or "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def repo_filter_is_exact(config) -> bool:
    """Whether ``--repos-exact`` / ``repo_filter_exact`` was passed.

    Kept as its own accessor, mirroring :func:`repo_filter_patterns`, so every
    :func:`match_repo_filter` call site reads both off the same ``config`` it
    already has in hand rather than threading a second argument by hand.
    """
    return bool(config.get("repo_filter_exact"))


def match_repo_filter(full_path: str, local_path: str, patterns: list[str],
                      *, exact: bool = False) -> bool:
    """A repo matches if any pattern is a glob hit or -- unless ``exact`` -- a plain
    substring of its group-qualified path or its local (group-stripped) path.
    Case-insensitive. ``team/*``, ``billing``, and ``acme/catalog-api`` all work.

    ``exact=True`` (``--repos-exact``) drops the substring leg, so a plain pattern
    with no glob characters must equal the *whole* id/path rather than merely occur
    somewhere in it -- ``fnmatch`` already anchors to the full string, so a bare
    name like ``atlas`` only matches a repo whose id/path is exactly ``atlas``, not
    one that merely contains it (e.g. ``platform-atlas``). Default unchanged for
    every existing caller that never passes ``exact``.
    """
    from fnmatch import fnmatch
    fp, lp = (full_path or "").lower(), (local_path or "").lower()
    for p in patterns:
        pl = p.lower()
        if fnmatch(fp, pl) or fnmatch(lp, pl):
            return True
        if not exact and (pl in fp or pl in lp):
            return True
    return False


class FetchError(RuntimeError):
    """Project enumeration failed. Raised instead of returning partial data so a
    transient blip can never overwrite a good cache or masquerade as success."""


def fetch_gitlab_projects(gitlab_group, config):
    """Enumerate every repository of the configured platform's group/org/workspace.

    The platform seam: ``platform`` in the config selects the enumerator
    (gitlab default; github / bitbucket / gitea, with codeberg + forgejo as
    gitea flavors). Every enumerator normalizes to the same project shape, so
    the whole pipeline downstream of this cache is platform-agnostic.

    GitLab prefers contextlake's own HTTP client when ``GITLAB_TOKEN`` is set --
    this avoids glab's short dial timeout, which a slow corporate DNS (e.g.
    Zscaler) would otherwise trip on every call -- and falls back to the ``glab``
    CLI (its own auth). Other platforms use the native client, tokenless for
    public owners or with the platform's token env var. Each page is retried
    with backoff on transient errors.

    Results are written to two caches under ``cache_dir``: a JSON map keyed by
    ``path_with_namespace`` and a pipe-delimited text file
    (``path|ssh|http|default_branch|archived``) for quick human/script use.
    """
    cache_file, cache_json = get_cache_paths(config)
    platform = platform_name(config)
    # Resolved once and used by every line this run prints, including the
    # failures below: the forge a message names must always be the forge that
    # was actually called.
    label = platform_label(config)
    log(f"Fetching {label} projects for: {style.cyan(gitlab_group)}")

    per_page = PLATFORM_DEFAULTS[platform]["per_page"]
    timeout = int(config.get("network_timeout", 30))

    if platform == "gitlab":
        group_enc = urllib.parse.quote(gitlab_group, safe="")
        token = _gitlab_token(config)
        if token:
            base = _gitlab_api_base(config)
            log(f"Enumerating via the GitLab REST API at {base} (token auth)")
            fetch_page = partial(_fetch_projects_page_http,
                                 base, group_enc, token, per_page, timeout)
        else:
            log("No GITLAB_TOKEN set -- enumerating via the 'glab' CLI (its own auth)")
            fetch_page = partial(_fetch_projects_page_glab, group_enc, per_page)
    else:
        token = _platform_token(config)
        base = _platform_api_base(config)
        auth = "token auth" if token else "no token: public repos only, rate-limited"
        log(f"Enumerating via the {label} REST API at {base} ({auth})")
        fetch_page = partial(_PLATFORM_FETCHERS[platform],
                             base, gitlab_group, token, per_page, timeout)

    all_projects = {}
    page = 1
    while True:
        try:
            # Enumeration is a one-shot bulk step, so it can afford to be patient:
            # more retries (≈1+2+4+8+16s of backoff) ride out a brief VPN/proxy
            # reconnect. A sustained outage still fails fast enough to degrade.
            projects = retry_with_backoff(fetch_page, page, max_retries=6)
        except Exception as e:  # noqa: BLE001 - a hard failure must surface, not truncate
            # Raise instead of writing what we have: a partial (or empty) result must
            # never replace a good cache under a green checkmark.
            #
            # The missing-glab advice belongs only to the run that actually
            # reached for glab. Both branches name `label`, so a failure can no
            # longer report a forge this run never called -- a github config
            # that 404'd used to say it "could not enumerate GitLab projects".
            if isinstance(e, FileNotFoundError) and fetch_page.func is _fetch_projects_page_glab:
                log("ERROR: 'glab' not found and no GITLAB_TOKEN set. Set GITLAB_TOKEN "
                    "(a read_api token), or install the GitLab CLI and run 'glab auth login'.")
                raise FetchError(
                    f"could not enumerate {label} projects: 'glab' not found and no "
                    "GITLAB_TOKEN set (existing caches left untouched)") from e
            log(f"Error fetching projects (page {page}): {e}")
            raise FetchError(
                f"could not enumerate {label} projects (failed on page {page}: {e}); "
                "existing caches left untouched") from e
        if not projects:
            break
        for p in projects:
            full = p.get("path_with_namespace")
            if full:
                all_projects[to_local_path(full, gitlab_group)] = {
                    "full_path": full,
                    "http": p.get("http_url_to_repo", ""),
                    "ssh": p.get("ssh_url_to_repo", ""),
                    "archived": p.get("archived", False),
                    "default_branch": p.get("default_branch", "main"),
                    # captured for the post-sync audit (repo age / activity)
                    "created_at": p.get("created_at"),
                    "last_activity_at": p.get("last_activity_at"),
                }
        log(style.dim(f"Fetched page {page}, total projects: {len(all_projects)}"))
        # Paginate until an EMPTY page (the `if not projects` above), never on a
        # short one: some servers cap the page size below what we request (Gitea
        # instances configure a max limit), and a short-page break would then
        # silently truncate the fleet. One extra request buys correctness.
        page += 1

    # Optional subset: --repos / repo_filter narrows the mirror to matching repos, so
    # `clone`/`update`/`branches`/`verify`/`status` (all keyed off this cache) operate
    # on just that set. Ideal for a demo or a try-before-fleet run.
    patterns = repo_filter_patterns(config)
    if patterns:
        exact = repo_filter_is_exact(config)
        before = len(all_projects)
        all_projects = {k: v for k, v in all_projects.items()
                        if match_repo_filter(v.get("full_path", k), k, patterns, exact=exact)}
        log(style.dim(f"Repo filter {patterns} -> {len(all_projects)} of {before} projects"))

    _warn_if_widening_scope(cache_json, patterns, len(all_projects))
    _write_caches(all_projects, cache_json, cache_file)
    _record_cache_filter(cache_json, patterns)
    if not all_projects:
        if patterns:
            log(style.warn(f"No projects matched --repos {patterns} — "
                           "check the pattern against `contextlake mirror status`"))
        else:
            log(style.warn("Fetched 0 projects — check the group name and your token's "
                           "read_api access before trusting this result"))
    else:
        label = "matching" if patterns else "total"
        log(f"{style.ok()} Fetched {style.bold(str(len(all_projects)))} {label} projects")
    return _remember_repo_names(all_projects)


def _remember_repo_names(projects):
    """Teach log redaction the fleet's repo ids, and return the map unchanged.

    Both the local key (``team/api``) and the forge's own ``full_path``
    (``acme/team/api``) go in: the two spellings appear in different messages,
    and a redacted log is only shareable if it hides both.
    """
    observability.add_repo_names(
        list(projects)
        + [p.get("full_path", "") for p in projects.values() if isinstance(p, dict)])
    return projects


def fetch_result(projects, config) -> StageResult:
    """Score a completed enumeration for the dispatch layer's exit code.

    Deliberately a separate function rather than a different return type on
    `fetch_gitlab_projects`: the project map is what `load_gitlab_projects` and
    every downstream stage consume, so fetch keeps returning the dict.

    An empty fetch counts as a failure only when nothing was filtering it. With
    no --repos/repo_filter, 0 projects means the group name or the token's
    read_api access is wrong (fetch warns exactly that above) and an unattended
    run must not call that a success; with a filter in play, 0 matches is a
    legitimate answer to a narrow pattern and stays a clean exit. Hard failures
    (network, auth, missing glab) never reach here -- they raise FetchError.
    """
    count = len(projects)
    if not count and not repo_filter_patterns(config):
        return StageResult(failed=1)
    return StageResult(ok=count)


def _cache_filter_path(cache_json):
    """Sidecar recording which ``--repos`` filter produced the cache next to it.

    A sidecar rather than a key inside the JSON: that file is a flat map of
    project path -> project, and every reader iterates its keys as projects, so
    a metadata entry there would be read back as a repository.
    """
    return cache_json + ".filter"


def _read_cache_filter(cache_json):
    try:
        with open(_cache_filter_path(cache_json)) as f:
            return f.read().strip()
    except OSError:
        return ""


def _record_cache_filter(cache_json, patterns):
    """Remember the filter this cache was written with (or that it had none)."""
    try:
        with open(_cache_filter_path(cache_json), "w") as f:
            f.write(",".join(patterns))
    except OSError:
        pass  # advisory only: never fail a good fetch over the breadcrumb


def cache_filter_conflict(config) -> str | None:
    """The ``--repos`` scope a warm cache was built with, when that scope cannot
    answer THIS invocation. None when the cache is usable as-is.

    ``--repos`` is a per-invocation choice (see :func:`_warn_if_widening_scope`),
    but the cache holds the *filtered* project list, so the two can disagree --
    and every command that reads a warm cache used to answer from it regardless.
    That made ``--repos`` silently inert on a warm cache: ``clone --repos
    <no-match>`` planned the previous filter's repositories, and ``status``
    reported a filtered count as the group total.

    Three cases, decided by whether the cached set is a superset of what this run
    asked for:

    * same scope -- the cache is exactly this run's set.
    * cache unfiltered -- a superset of any narrower run, so the filter is simply
      applied at read time (:func:`_apply_repo_filter`).
    * cache filtered *differently* -- neither superset nor subset, so it can
      neither confirm nor deny membership for this run. That is what this
      function reports, and the caller re-enumerates or says it cannot answer.
    """
    _, cache_json = get_cache_paths(config)
    recorded = _read_cache_filter(cache_json)
    if not recorded:
        return None
    return None if recorded == ",".join(repo_filter_patterns(config)) else recorded


def _apply_repo_filter(projects, config):
    """Narrow a cached project map to this run's ``--repos`` patterns.

    Idempotent, so it is safe to run over a cache the same filter already
    narrowed: re-matching the same patterns keeps the same set.
    """
    patterns = repo_filter_patterns(config)
    if not patterns:
        return projects
    exact = repo_filter_is_exact(config)
    return {k: v for k, v in projects.items()
            if match_repo_filter(v.get("full_path", k), k, patterns, exact=exact)}


def _warn_if_widening_scope(cache_json, patterns, new_count):
    """Say so when an unfiltered fetch replaces a deliberately scoped cache.

    `fetch --repos demo` narrows the cache, and clone/update/branches/verify all
    key off it. A later `sync` runs an unfiltered fetch that silently overwrites
    that cache, so a user who scoped to a handful of repos got the entire group
    cloned with no warning -- the widening is announced instead. Advisory only:
    the flag stays a per-invocation choice rather than becoming sticky, which
    would surprise in the other direction.
    """
    if patterns:
        return
    previous = _read_cache_filter(cache_json)
    if not previous:
        return
    log(style.warn(
        f"The cached project list was scoped to --repos {previous!r}; this run was not, "
        f"so the scope widens to the whole group ({new_count} projects). "
        "Re-run with --repos to keep it narrow."))


def _write_caches(all_projects, cache_json, cache_file):
    """Persist the project map as JSON and as a pipe-delimited text cache."""
    os.makedirs(os.path.dirname(cache_json) or ".", exist_ok=True)
    with open(cache_json, "w") as f:
        json.dump(all_projects, f, indent=2)
    with open(cache_file, "w") as f:
        for path, p in all_projects.items():
            f.write(f"{path}|{p['ssh']}|{p['http']}|{p['default_branch']}|{p['archived']}\n")


def load_gitlab_projects(config, gitlab_group, allow_fetch=True):
    """Load the cached project map, normalizing legacy list-shaped JSON.

    Falls back to a fresh fetch when no usable cache exists, unless
    ``allow_fetch`` is False -- then a cold cache returns ``{}`` and the caller
    reports it. ``status`` reads as an inspection and is the first command of
    the day, so silently enumerating the whole forge (30-50s, and able to fail
    on the network) and writing the cache was a surprise from a command whose
    job is to describe state, not change it.

    This run's ``--repos`` is honoured against whatever the cache holds: a cache
    the same filter (or no filter) produced is narrowed here, and a cache some
    *other* filter produced cannot answer at all and is re-enumerated instead of
    answered from. See :func:`cache_filter_conflict`.
    """
    _, cache_json = get_cache_paths(config)
    stale_scope = cache_filter_conflict(config)

    if stale_scope is None and os.path.exists(cache_json):
        try:
            with open(cache_json) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = None

        if isinstance(data, dict) and data:
            # Re-key by local path and backfill full_path so a cache written by
            # an older (full-path-keyed) version still maps onto local clones.
            normalized = {}
            for key, value in data.items():
                full = value.get("full_path", key)
                normalized[to_local_path(key, gitlab_group)] = {**value, "full_path": full}
            return _remember_repo_names(_apply_repo_filter(normalized, config))
        if isinstance(data, list) and data:
            # Legacy/raw list of project objects -> normalize to the dict shape.
            normalized = {}
            for p in data:
                full = p.get("path_with_namespace") if isinstance(p, dict) else None
                if full:
                    normalized[to_local_path(full, gitlab_group)] = {
                        "full_path": full,
                        "http": p.get("http_url_to_repo", ""),
                        "ssh": p.get("ssh_url_to_repo", ""),
                        "archived": p.get("archived", False),
                        "default_branch": p.get("default_branch", "main"),
                    }
            if normalized:
                return _remember_repo_names(_apply_repo_filter(normalized, config))

    if not allow_fetch:
        return {}
    if stale_scope:
        log(f"The cached project list covers only --repos {stale_scope!r}, which is not "
            "the scope of this run -- re-enumerating rather than answering from it")
    else:
        log("Cache not found or invalid, fetching fresh data...")
    return fetch_gitlab_projects(gitlab_group, config)


def get_local_repos(work_dir):
    """Return repo paths (relative to work_dir) for every directory with a .git."""
    local_repos = []
    for root, dirs, _files in os.walk(work_dir):
        if ".git" in dirs:
            local_repos.append(os.path.relpath(root, work_dir))
    # This and load_gitlab_projects() are the two places that know the fleet's
    # names, so they are where log redaction learns them (see
    # observability.add_repo_names). Cheap: a set union, with nothing compiled
    # unless redaction is actually in use.
    observability.add_repo_names(local_repos)
    return local_repos


def filtered_local_repos(work_dir, config):
    """Local repo paths, narrowed by ``--repos``/``repo_filter``.

    ``fetch`` applies the filter to the project cache, so ``clone`` inherits it;
    ``update``/``branches``/``verify`` walk the work directory instead and so
    accepted the flag and ignored it. That is worse than not supporting it: the
    hint those same commands print on failure recommends
    ``--repos <name>`` as the way to retry just the failures, so a user
    following it re-ran the entire fleet.

    A local repo has only its group-relative path to match on (the forge's
    ``full_path`` lives in the project cache, which these commands do not
    consult), so that one spelling is passed as both of
    :func:`match_repo_filter`'s arguments.
    """
    repos = get_local_repos(work_dir)
    patterns = repo_filter_patterns(config)
    if not patterns:
        return repos
    exact = repo_filter_is_exact(config)
    matched = [p for p in repos if match_repo_filter(p, p, patterns, exact=exact)]
    # A typo in the pattern must not read as a clean run over nothing, which is
    # the shape honouring the filter newly makes reachable here. `fetch` already
    # says this for the same situation against the project list.
    #
    # Only when there was something to match, though. An empty work directory is
    # the ordinary state of `clone --repos <name>` on a first run, and warning
    # there tells the user to check a pattern that is doing exactly its job.
    if repos and not matched:
        # Names the directory rather than a command: `mirror status` is one of
        # this helper's callers, and sending a reader from status to status is
        # the sort of self-referential advice that reads as a bug.
        log(style.warn(f"No local repositories matched --repos {patterns}; "
                       f"check the pattern against the repositories under {work_dir}"))
    return matched


def is_valid_git_repo(full_path):
    """True if ``full_path`` exists and contains a .git entry."""
    return os.path.isdir(full_path) and os.path.exists(os.path.join(full_path, ".git"))


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------

def _git_token_env(token, username="oauth2"):
    """A child env that authenticates git-over-HTTPS with a platform token.

    The credential travels as an ``http.extraHeader`` config entry injected via
    the ``GIT_CONFIG_*`` environment (offset past any entries the user already
    set) — never on the command line (visible in ``ps``), never in the clone
    URL (git would persist it into ``.git/config``). ``username`` is the
    basic-auth user the platform expects alongside a token (oauth2 for
    GitLab/Gitea, x-access-token for GitHub, x-token-auth for Bitbucket).
    """
    env = os.environ.copy()
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    basic = base64.b64encode(f"{username}:{token}".encode()).decode()
    env[f"GIT_CONFIG_KEY_{count}"] = "http.extraHeader"
    env[f"GIT_CONFIG_VALUE_{count}"] = f"Authorization: Basic {basic}"
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def _build_clone_cmd(project_path, http_url, full_path, method, token=None,
                     platform="gitlab"):
    """Choose the clone command (and child env) for one repository.

    ``auto`` prefers, in order: native ``git`` with token auth (no platform CLI
    needed, and git tolerates slow corporate DNS that trips glab's short dial
    timeout) -> ``glab`` when installed (GitLab only; its own auth) -> plain
    ``git`` over HTTPS (public repos / an ambient credential helper).
    """
    if token and method in ("auto", "git"):
        user = PLATFORM_DEFAULTS.get(platform, PLATFORM_DEFAULTS["gitlab"])["clone_user"]
        return ["git", "clone", http_url, full_path], _git_token_env(token, user)
    use_glab = method == "glab" or (
        method == "auto" and platform == "gitlab" and shutil.which("glab") is not None)
    if use_glab and project_path:
        return ["glab", "repo", "clone", project_path, full_path], None
    return ["git", "clone", http_url, full_path], None


def _clone_once(clone_cmd, timeout, env=None, dest=None):
    """Run a single clone attempt, raising on failure so retry can engage.

    Clearing ``dest`` belongs to the attempt, not to a one-time step before the
    retry loop. A clone that dies partway leaves a partially populated
    destination behind, and ``git clone`` refuses a non-empty directory -- so
    every retry died instantly on "destination path already exists" instead of
    actually retrying. That made ``max_retries`` dead for precisely the failures
    it exists for, and replaced the real first error with a misleading one. Each
    attempt now starts from the same state the first one did.
    """
    if dest and os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    result = subprocess.run(clone_cmd, capture_output=True, text=True, errors="replace",
                            timeout=timeout, env=env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "clone failed").strip()[:200])
    return result


def clone_repository(local_path, gitlab_path, http, ssh, work_dir, config):
    """Clone one repository, with corruption cleanup, retry/backoff and dry-run.

    ``local_path`` is the destination (group-relative); ``gitlab_path`` is the
    full ``<group>/...`` project path that ``glab`` needs to resolve the repo.
    """
    full_path = os.path.join(work_dir, local_path)
    clone_timeout = _int(config, "clone_timeout", "300")
    clean_corrupted = _is_truthy(config, "clean_corrupted", "true")
    dry_run = _is_truthy(config, "dry_run")
    method = config.get("clone_method", "auto")

    # Existing directory: skip if a valid clone, otherwise clean it (if allowed).
    if os.path.exists(full_path):
        if is_valid_git_repo(full_path):
            return ("skip", local_path, "Already exists")
        if not clean_corrupted:
            return ("error", local_path, "Exists but not a git repo (use --clean-corrupted)")
        if dry_run:
            return ("dry-run", local_path, "Would clean corrupted dir and clone")
        shutil.rmtree(full_path, ignore_errors=True)

    if dry_run:
        return ("dry-run", local_path, "Would clone")

    os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
    clone_cmd, clone_env = _build_clone_cmd(gitlab_path, http, full_path, method,
                                            token=_platform_token(config),
                                            platform=platform_name(config))

    try:
        retry_with_backoff(
            _clone_once, clone_cmd, clone_timeout, env=clone_env, dest=full_path,
            max_retries=_int(config, "max_retries", "3"),
            backoff_initial=_float(config, "backoff_initial", "1"),
            backoff_max=_float(config, "backoff_max", "30"),
        )
        return ("ok", local_path, "Cloned")
    except subprocess.TimeoutExpired:
        shutil.rmtree(full_path, ignore_errors=True)
        return ("error", local_path, "Timeout")
    except Exception as e:  # noqa: BLE001 - reported per-repo, never aborts the run
        shutil.rmtree(full_path, ignore_errors=True)
        return ("error", local_path, str(e)[:200])


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def _rev_parse(full_path, ref="HEAD", timeout=30):
    """Resolve ``ref`` to its commit sha. Raises on a git failure rather than
    returning "" -- a silent empty result makes the before/after comparison in
    ``update_repository`` misread the update state (e.g. report 'nochange' when
    the fetch actually advanced HEAD)."""
    res = subprocess.run(
        ["git", "rev-parse", ref], capture_output=True, text=True, errors="replace",
        cwd=full_path, timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError((res.stderr or "git rev-parse failed").strip())
    return res.stdout.strip()


def _git_auth_env(config):
    """The authenticated child env for git-over-HTTPS, or ``None`` if no token.

    Cloning built this env inline and nothing else did, so ``update`` and
    ``branches`` ran unauthenticated. On a workstation an ambient credential
    helper supplies the credential and hides it completely; in a container or CI
    job, where the token is the only credential, the first sync clones fine and
    every later refresh fails with ``could not read Username``. Same token, same
    header mechanism as the clone path, just reachable from the other commands.
    """
    token = _platform_token(config)
    if not token:
        return None
    name = platform_name(config)
    user = PLATFORM_DEFAULTS.get(name, PLATFORM_DEFAULTS["gitlab"])["clone_user"]
    return _git_token_env(token, user)


def _run_git(args, cwd, timeout, env=None):
    """Run a git command, raising ``RuntimeError(stderr)`` on a non-zero exit.

    Raising (rather than returning a code) lets ``retry_with_backoff`` see the
    git error text via ``classify_error`` and decide whether to retry.

    ``env`` carries the token header for private repos over HTTPS; ``None``
    inherits this process's environment, which is what a public repo or an
    ambient credential helper needs.
    """
    res = subprocess.run(args, capture_output=True, text=True, errors="replace", cwd=cwd,
                         timeout=timeout, env=env)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "git command failed").strip())
    return res


def _first_line(text, limit=200):
    """First non-empty line of ``text``, trimmed -- keeps multi-line git output
    (e.g. the 'divergent branches' hint) from spilling into a one-line status."""
    stripped = (text or "").strip()
    return stripped.splitlines()[0][:limit] if stripped else ""


def _git_reason(text, limit=120):
    """A short, human-readable reason from raw git error output.

    Git's most common failure here is a repository with no commits, which it
    reports as a three-line "ambiguous argument 'HEAD'" usage hint -- accurate for
    a git user, meaningless as a one-line status. Recognised cases get a plain
    explanation; anything else falls back to the first line, tightly clamped so a
    long remote message cannot wrap the progress display.
    """
    first = _first_line(text, limit=limit)
    lowered = first.lower()
    if "ambiguous argument 'head'" in lowered or "unknown revision" in lowered:
        return "New repo -- no commits yet"
    return first


def _fetch_with_retry(git_args, full_path, fetch_timeout, config):
    """Fetch with exponential-backoff retry on transient proxy/network drops.

    Every network operation outside cloning funnels through here (``update``'s
    branch fetch and both of ``branches``' fetches), which is why authenticating
    in this one place is enough to close the gap that left them unable to reach
    a private repo when a token was the only credential.
    """
    retry_with_backoff(
        _run_git, git_args, full_path, fetch_timeout, _git_auth_env(config),
        max_retries=_int(config, "max_retries", "3"),
        backoff_initial=_float(config, "backoff_initial", "1"),
        backoff_max=_float(config, "backoff_max", "30"),
    )


def update_repository(local_path, work_dir, config):
    """Fetch + fast-forward a single repo's current branch (safety-gated).

    ``--auto-stash`` is a round trip, not a one-way move: the stash is popped
    again once the update is done. Stashing without restoring left the user's
    edits out of the working tree with nothing on screen saying a stash existed,
    which is the surprise the safety flags exist to prevent. A pop that fails is
    reported as an error rather than a quiet note, so the outstanding stash
    reaches the run's failure count instead of being buried in a green summary.
    """
    full_path = os.path.join(work_dir, local_path)
    dry_run = _is_truthy(config, "dry_run")

    safe, warnings = check_repository_safety(local_path, work_dir, config)
    stash_sha = None
    if not safe:
        reason = f'Skipped (unsafe: {", ".join(warnings)})'
        has_changes = any("Uncommitted changes" in w for w in warnings)
        if not has_changes or dry_run or not _is_truthy(config, "auto_stash"):
            return ("skip", local_path, reason)
        # Why the stash failed is appended only when one was actually attempted:
        # the default path never tried, so it has nothing to explain.
        stash_success, stash_msg, stash_sha = stash_changes(full_path, config)
        if not stash_success:
            return ("skip", local_path, f"{reason} -- {stash_msg}")
        log(f"{style.yellow('⚠')} {style.cyan(local_path)}: {stash_msg}")

    try:
        result = _update_synced(local_path, full_path, config)
    finally:
        # In the finally so a Ctrl-C between here and the return still puts the
        # user's work back -- an interrupted run must not be the one that leaves
        # a stash behind.
        restored = restore_msg = None
        if stash_sha:
            restored, restore_msg = restore_stash(full_path, stash_sha)
            if not restored:
                log(f"{style.yellow('⚠')} {style.cyan(local_path)}: "
                    f"your changes are still stashed -- {restore_msg}")
    if stash_sha and not restored:
        return ("error", local_path,
                f"Updated, but restoring your stashed changes failed: {restore_msg}")
    return result


def _update_synced(local_path, full_path, config):
    """The fetch/fast-forward itself, once the tree is known safe to touch.

    Split out of ``update_repository`` so the auto-stash restore wraps every exit
    path of this half, including the error ones -- a stash must come back whether
    the update succeeded, failed, or timed out. Total by construction: every
    failure is a returned tuple, never an exception.
    """
    fetch_timeout = _int(config, "fetch_timeout", "60")
    pull_timeout = _int(config, "pull_timeout", "60")
    dry_run = _is_truthy(config, "dry_run")

    try:
        # _run_git raises on a non-zero exit, so a failed branch read surfaces as a
        # clean per-repo error instead of an empty string that fetches branch "".
        # A repo with no commits yet has no HEAD to resolve -- this describes what
        # the repo currently IS, not something that failed, so it's a "note", not
        # an error or even a skip (nothing was skipped; there's nothing to sync
        # yet). Same classification the branch-switch path below already applies
        # to the identical condition.
        try:
            curr_res = _run_git(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], full_path, 30
            )
        except Exception as e:  # noqa: BLE001 - only empty-repo is special-cased here
            reason = _git_reason(str(e))
            if reason == "New repo -- no commits yet":
                return ("note", local_path, reason)
            raise
        current = curr_res.stdout.strip()
        if current == "HEAD":
            return ("skip", local_path, "Detached HEAD")

        if dry_run:
            return ("dry-run", local_path, f"Would update {current}")

        # Fetch just this branch, retrying transient proxy/network drops (e.g.
        # "unexpected eof", "connection reset") instead of failing on the first
        # hiccup. A deleted upstream branch fails fast and is reported cleanly.
        try:
            _fetch_with_retry(
                ["git", "fetch", "--quiet", "origin", current], full_path, fetch_timeout, config
            )
        except Exception as e:  # noqa: BLE001 - reported per-repo, never aborts the run
            reason = classify_error(str(e))
            if reason == "missing-ref":
                # The tracked branch is gone upstream -- almost always renamed,
                # merged, or superseded by another default, never something a
                # human needs to triage by hand. Auto-reselect instead of just
                # telling the user to run `branches` themselves.
                return _reselect_branch_after_deletion(full_path, local_path, current, config)
            if reason == "project-deleted":
                return ("skip", local_path,
                        "Upstream project not found (deleted or access revoked) "
                        "-- run verify to confirm")
            return ("error", local_path, _first_line(str(e)))

        before = _rev_parse(full_path, "HEAD")
        # Fast-forward only: a mirror never merges or rebases. A branch that has
        # diverged from origin is reported cleanly rather than dumping git's
        # multi-line "divergent branches" hint into the output.
        merge = subprocess.run(
            ["git", "merge", "--ff-only", "--quiet", "FETCH_HEAD"],
            capture_output=True, text=True, errors="replace", cwd=full_path,
            timeout=pull_timeout,
        )
        if merge.returncode != 0:
            detail = (merge.stderr or merge.stdout or "").strip()
            if classify_error(detail) == "diverged":
                return ("skip", local_path,
                        f"Diverged from origin/{current} — skipped (manual reconcile)")
            return ("error", local_path, _first_line(detail) or "fast-forward failed")

        after = _rev_parse(full_path, "HEAD")
        if before != after:
            return ("ok", local_path, f"Updated {current}")
        return ("nochange", local_path, f"Already up to date on {current}")

    except subprocess.TimeoutExpired:
        return ("error", local_path, "Timeout")
    except Exception as e:  # noqa: BLE001
        # _git_reason, not a raw slice: git's "ambiguous argument 'HEAD'" is three
        # lines of usage hint, and printing it verbatim wrecked the progress display.
        return ("error", local_path, _git_reason(str(e)))


# ---------------------------------------------------------------------------
# Branch selection
# ---------------------------------------------------------------------------

def _parse_iso(date_str):
    """Parse a git iso8601 committer date into a POSIX timestamp (0 on failure)."""
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(date_str.strip(), fmt).timestamp()
        except (ValueError, AttributeError):
            continue
    return 0.0


def select_most_active_branch(branch_info, strategy="hybrid"):
    """Pick the most active branch from [{name, count, ts}, ...].

    - commits: highest commit count (legacy behaviour)
    - recency: most recent commit
    - hybrid:  normalized commit-count and recency combined (60/40)
    """
    if not branch_info:
        return None
    if strategy == "commits":
        return max(branch_info, key=lambda b: b["count"])["name"]
    if strategy == "recency":
        return max(branch_info, key=lambda b: b["ts"])["name"]

    counts = [b["count"] for b in branch_info]
    times = [b["ts"] for b in branch_info]
    c_min, c_max = min(counts), max(counts)
    t_min, t_max = min(times), max(times)

    def norm(value, lo, hi):
        return (value - lo) / (hi - lo) if hi > lo else 1.0

    def score(b):
        return 0.6 * norm(b["count"], c_min, c_max) + 0.4 * norm(b["ts"], t_min, t_max)

    return max(branch_info, key=score)["name"]


def _collect_branch_info(full_path, branch_timeout):
    """Return [{name, count, ts}] for each origin/* branch."""
    result = subprocess.run(
        ["git", "for-each-ref", "--sort=-committerdate",
         "--format=%(refname:short)|%(committerdate:iso8601)|%(objectname)",
         "refs/remotes/origin/"],
        capture_output=True, text=True, errors="replace", cwd=full_path,
        timeout=branch_timeout,
    )
    # A git failure here must not masquerade as "No branches found" (a skip);
    # raise so the caller reports a real error instead of silently mis-selecting.
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "git for-each-ref failed").strip())
    branch_info = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        if parts[0] == "origin/HEAD":
            continue  # the symbolic ref, not a real branch -- an *exact* match,
            # not a substring one: a real branch merely named e.g. "release/HEAD-fix"
            # must not be silently dropped from consideration.
        branch = parts[0].replace("origin/", "")
        count_res = subprocess.run(
            ["git", "rev-list", "--count", f"origin/{branch}"],
            capture_output=True, text=True, errors="replace", cwd=full_path,
            timeout=branch_timeout,
        )
        count = int(count_res.stdout.strip()) if count_res.stdout.strip().isdigit() else 0
        branch_info.append({"name": branch, "count": count, "ts": _parse_iso(parts[1])})
    return branch_info


def _reselect_branch_after_deletion(full_path, local_path, deleted_branch, config):
    """The branch `update_repository` was tracking no longer exists on origin
    (renamed, merged, or superseded by another default) -- auto-pick a new
    most-active branch and switch to it, the same selection `branches` uses,
    rather than leaving the repo stuck reporting the same dead branch on every
    future run.

    ``check_repository_safety`` already confirmed the repo is safe to touch
    before the caller reached this fetch, so the checkout below isn't gated
    on a working-branch-protection check: the tracked branch is definitionally
    gone, there's nothing left to protect by staying on it.
    """
    fetch_timeout = _int(config, "fetch_timeout", "60")
    branch_timeout = _int(config, "branch_timeout", "30")
    pull_timeout = _int(config, "pull_timeout", "60")
    strategy = config.get("branch_strategy", "hybrid")
    dry_run = _is_truthy(config, "dry_run")
    prefix = f"Upstream branch deleted: {deleted_branch}"

    try:
        _fetch_with_retry(["git", "fetch", "--all", "--quiet"], full_path, fetch_timeout, config)
        branch_info = _collect_branch_info(full_path, branch_timeout)
    except Exception as e:  # noqa: BLE001 - reported per-repo, never aborts the run
        return ("skip", local_path,
                f"{prefix} (auto-reselect failed: {_first_line(str(e))}; "
                "run branches to pick one manually)")
    if not branch_info:
        return ("skip", local_path, f"{prefix} (no other branches found on origin)")

    new_branch = select_most_active_branch(branch_info, strategy)
    if dry_run:
        return ("dry-run", local_path, f"{prefix} -- would switch to {new_branch}")

    checkout = subprocess.run(
        ["git", "checkout", "--quiet", new_branch],
        capture_output=True, text=True, errors="replace", cwd=full_path,
        timeout=branch_timeout,
    )
    if checkout.returncode != 0:
        return ("skip", local_path,
                f"{prefix} (auto-checkout of {new_branch} failed: "
                f"{_first_line(checkout.stderr)}; run branches to pick one manually)")
    subprocess.run(
        ["git", "merge", "--ff-only", "--quiet", f"origin/{new_branch}"],
        capture_output=True, cwd=full_path, timeout=pull_timeout,
    )
    return ("switched", local_path, f"{prefix} -- auto-switched to {new_branch}")


def switch_repository_branch(local_path, projects, work_dir, config):
    """Switch one repo to its most active branch (protecting working branches)."""
    if local_path not in projects:
        return ("skip", local_path, "Not in GitLab list")
    if projects[local_path]["archived"]:
        return ("skip", local_path, "Archived")

    full_path = os.path.join(work_dir, local_path)
    fetch_timeout = _int(config, "fetch_timeout", "60")
    branch_timeout = _int(config, "branch_timeout", "30")
    pull_timeout = _int(config, "pull_timeout", "60")
    protect = _is_truthy(config, "protect_working_branches", "true")
    strategy = config.get("branch_strategy", "hybrid")
    dry_run = _is_truthy(config, "dry_run")

    try:
        safe, warnings = check_repository_safety(local_path, work_dir, config)
        if not safe:
            return ("skip", local_path, f'Skipped (unsafe: {", ".join(warnings)})')

        # Retry transient proxy/network drops here too (this fetch feeds the
        # most-active-branch selection, so a partial fetch would pick wrong).
        try:
            _fetch_with_retry(
                ["git", "fetch", "--all", "--quiet"], full_path, fetch_timeout, config
            )
        except Exception as e:  # noqa: BLE001 - reported per-repo, never aborts the run
            # Same classification update_repository already applies to its own
            # fetch failure: a deleted/access-revoked upstream project is a
            # clean skip here too, not a generic error inflating the run's
            # error count -- this command also fetches from the same origin.
            if classify_error(str(e)) == "project-deleted":
                return ("skip", local_path,
                        "Upstream project not found (deleted or access revoked) "
                        "-- run verify to confirm")
            return ("error", local_path, _first_line(str(e)))

        try:
            curr_res = _run_git(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], full_path, branch_timeout
            )
        except Exception as e:  # noqa: BLE001
            # A freshly-cloned repo with no commits has no HEAD to resolve
            # (git: "ambiguous argument 'HEAD'"). This describes the repo's own
            # state, not a failure, so it's a "note" -- same classification +
            # message update_repository gives the identical condition.
            return ("note", local_path, _git_reason(str(e)))
        current = curr_res.stdout.strip()

        if protect and not is_safe_branch(current, config):
            return ("skip", local_path, f"Skipped branch switch (on working branch: {current})")

        branch_info = _collect_branch_info(full_path, branch_timeout)
        if not branch_info:
            return ("skip", local_path, "No branches found")

        most_active = select_most_active_branch(branch_info, strategy)
        if current == most_active:
            return ("ok", local_path, f"Already on {most_active}")

        if dry_run:
            return ("dry-run", local_path, f"Would switch {current} -> {most_active}")

        checkout = subprocess.run(
            ["git", "checkout", "--quiet", most_active],
            capture_output=True, text=True, errors="replace", cwd=full_path,
            timeout=branch_timeout,
        )
        if checkout.returncode != 0:
            return ("error", local_path, _first_line(checkout.stderr) or "checkout failed")
        # Fast-forward the freshly-checked-out branch to origin. No network: we
        # already fetched --all above, so this is a local ff (best effort -- a
        # diverged branch is simply left at its current tip).
        subprocess.run(
            ["git", "merge", "--ff-only", "--quiet", f"origin/{most_active}"],
            capture_output=True, cwd=full_path, timeout=pull_timeout,
        )
        return ("switched", local_path, f"{current} -> {most_active}")

    except subprocess.TimeoutExpired:
        return ("error", local_path, "Timeout")
    except Exception as e:  # noqa: BLE001
        # _git_reason, not a raw slice: git's "ambiguous argument 'HEAD'" is three
        # lines of usage hint, and printing it verbatim wrecked the progress display.
        return ("error", local_path, _git_reason(str(e)))


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify_repository(local_path, projects, work_dir, config):
    """Classify a single path as ok / extra / missing / invalid."""
    if local_path not in projects:
        return ("extra", local_path, "Extra local repo")

    full_path = os.path.join(work_dir, local_path)
    if not os.path.exists(full_path):
        return ("missing", local_path, "Missing local repo")
    if not os.path.exists(os.path.join(full_path, ".git")):
        return ("invalid", local_path, "Not a git repository")
    return ("ok", local_path, "Valid")


def find_nested_repos(local_repos):
    """Return repos that live inside another repo's working tree (corruption signal)."""
    repo_set = set(local_repos)
    nested = []
    for path in local_repos:
        parts = path.split(os.sep)
        for i in range(1, len(parts)):
            if os.sep.join(parts[:i]) in repo_set:
                nested.append(path)
                break
    return nested


# ---------------------------------------------------------------------------
# Orchestration (the seven verbs)
# ---------------------------------------------------------------------------

def _summarize(buckets):
    return ", ".join(f"{len(v)} {k}" for k, v in buckets.items())


def _timed(fn, *args):
    """Run a per-repo worker and return ``(its result, elapsed milliseconds)``.

    The workers' ``(status, path, message)`` contract is what the consumer loops
    and the tests are written against, so the timing rides *alongside* it rather
    than being added to it -- that keeps `--log-format json`'s per-repo
    ``duration_ms`` field (the number that answers "which repo is making the
    nightly run slow") free of any change to the stage functions themselves.
    """
    started = time.monotonic()
    result = fn(*args)
    return result, int((time.monotonic() - started) * 1000)


def _repo_fields(status, path, message, duration_ms=None):
    """Structured fields for one per-repo line (invisible in the human format)."""
    fields = {"repo": path, "status": status}
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms
    if status not in _REPO_OK_STATES:
        # The classifier already knows *why* a repo failed (network, dns, auth,
        # timeout, ...); carrying that as its own field is what makes "every
        # failure last night was dns" a query rather than a grep.
        fields["error_type"] = classify_error(message or "")
        fields["error"] = message
    return fields


# Per-repo statuses that are not failures. Anything else a worker returns is one
# (the loops below already treat every unrecognised status as an error).
_REPO_OK_STATES = frozenset({"ok", "nochange", "switched", "skip", "note", "dry-run"})


def _bucket_result(buckets, ok_keys, skipped_keys):
    """Fold a stage's result buckets into a StageResult, without recounting:
    ``errors`` is the failure bucket every loop already sorts into."""
    return StageResult(
        ok=sum(len(buckets[k]) for k in ok_keys),
        failed=len(buckets["errors"]),
        skipped=sum(len(buckets[k]) for k in skipped_keys),
    )


def clone_missing_repos(work_dir, config, gitlab_group):
    """Clone every active GitLab project that is not already present locally."""
    log("Cloning missing repositories...")

    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        return StageResult()

    # Filtered for the same reason status is: `projects` above is already scoped,
    # so an unfiltered local list makes "Already cloned locally" a count from a
    # different population than the two lines around it. Membership is
    # unaffected -- every path in `projects` matches the filter, so it matches
    # here too -- only the reported number changes.
    local_repos = set(filtered_local_repos(work_dir, config))
    max_workers = _int(config, "max_workers", "8")
    adaptive = _is_truthy(config, "adaptive_workers", "true")
    min_workers = _int(config, "min_workers", "2")
    error_threshold = _float(config, "error_threshold", "0.5")

    to_clone = [
        {
            "local_path": path,
            "gitlab_path": p.get("full_path", path),
            "http": p["http"],
            "ssh": p["ssh"],
        }
        for path, p in projects.items()
        if not p["archived"] and path not in local_repos
    ]

    active_count = len([p for p in projects.values() if not p["archived"]])
    # Name the scope in the line that reports the count, for the same reason
    # `status` does: a filtered number presented as the group total is the
    # defect, not the filtering.
    patterns = repo_filter_patterns(config)
    scope = f" matching --repos {','.join(patterns)}" if patterns else ""
    log(f"Active GitLab projects{scope}: {active_count}")
    log(f"Already cloned locally: {len(local_repos)}")
    log(f"To clone: {len(to_clone)}")
    if not to_clone:
        log("No missing repositories to clone")
        return StageResult()

    successes, skipped, failures, dry = [], [], [], []
    done = 0
    total = len(to_clone)
    progress = style.Progress(total, label="clone")

    _CLONE_STATES = {"ok": "ok", "skip": "skip", "note": "note", "dry-run": "dryrun"}

    def handle(result, duration_ms):
        nonlocal done
        done += 1
        status, path, message = result
        if status == "ok":
            successes.append(path)
        elif status == "skip":
            skipped.append(path)
        elif status == "dry-run":
            dry.append(path)
        else:
            failures.append(path)
        # Anything not in _CLONE_STATES is an error, so default to "fail" rather
        # than let an unmapped status reach status_line (which raises).
        log(_status(done, total, _CLONE_STATES.get(status, "fail"), path, message),
            inline=True, **_repo_fields(status, path, message, duration_ms))
        progress.advance(path)
        return status in ("ok", "skip", "dry-run")

    if adaptive:
        # Process in waves; resize the pool between waves based on error rate.
        pool = AdaptiveWorkerPool(max_workers, min_workers, error_threshold)
        remaining = list(to_clone)
        while remaining:
            batch_size = max(1, pool.get_worker_count())
            batch, remaining = remaining[:batch_size], remaining[batch_size:]
            with ThreadPoolExecutor(max_workers=batch_size) as ex:
                futures = [
                    ex.submit(_timed, clone_repository, it["local_path"], it["gitlab_path"],
                              it["http"], it["ssh"], work_dir, config)
                    for it in batch
                ]
                for fut in as_completed(futures):
                    pool.record_result(handle(*fut.result()))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [
                ex.submit(_timed, clone_repository, it["local_path"], it["gitlab_path"],
                          it["http"], it["ssh"], work_dir, config)
                for it in to_clone
            ]
            for fut in as_completed(futures):
                handle(*fut.result())

    progress.done()
    # Glyph follows the outcome, as update and branches already do: a green tick
    # over "1 failed" is the summary contradicting its own counts, and it is the
    # line a fleet run is skimmed by.
    glyph = style.ok() if not failures else style.warn()
    log(f"{glyph} Clone complete: " + _summarize({
        "successful": successes, "skipped": skipped, "dry-run": dry, "failed": failures,
    }))
    # A dry run cloned nothing, so it is skipped work, not a success -- only
    # `failures` (what the loop already classified as an error) drives the exit code.
    return StageResult(ok=len(successes), failed=len(failures),
                       skipped=len(skipped) + len(dry))


def update_repositories(work_dir, config):
    """Update every local repository."""
    # Say which set is being updated before saying how many: "all repositories"
    # over a filtered count contradicts itself the moment --repos is in play.
    patterns = repo_filter_patterns(config)
    log(f"Updating repositories matching {','.join(patterns)}..." if patterns
        else "Updating all repositories...")

    local_repos = filtered_local_repos(work_dir, config)
    max_workers = _int(config, "max_workers", "8")
    log(f"Found {len(local_repos)} local repositories")

    buckets = {"updated": [], "unchanged": [], "switched": [], "skipped": [], "empty": [],
               "dry-run": [], "errors": []}
    total = len(local_repos)
    progress = style.Progress(total, label="update")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_timed, update_repository, p, work_dir, config): p
                   for p in local_repos}
        for i, fut in enumerate(as_completed(futures), 1):
            (status, path, message), duration_ms = fut.result()
            fields = _repo_fields(status, path, message, duration_ms)
            if status == "ok":
                buckets["updated"].append(path)
                log(_status(i, total, "ok", path, message), inline=True, **fields)
            elif status == "nochange":
                buckets["unchanged"].append(path)
                log(_status(i, total, "nochange", path, message), inline=True, **fields)
            elif status == "switched":
                buckets["switched"].append(path)
                log(_status(i, total, "switched", path, message), inline=True, **fields)
            elif status == "skip":
                buckets["skipped"].append(path)
                log(_status(i, total, "skip", path, message), inline=True, **fields)
            elif status == "note":
                buckets["empty"].append(path)
                log(_status(i, total, "note", path, message), inline=True, **fields)
            elif status == "dry-run":
                buckets["dry-run"].append(path)
                log(_status(i, total, "dryrun", path, message), inline=True, **fields)
            else:
                buckets["errors"].append(path)
                log(_status(i, total, "fail", path, message), inline=True, **fields)
            progress.advance(path)

    progress.done()
    glyph = style.ok() if not buckets["errors"] else style.warn()
    log(f"{glyph} Update complete: {_summarize(buckets)}")
    if buckets["switched"]:
        log(f"  {len(buckets['switched'])} repo(s) auto-switched to a new branch "
            "(their tracked upstream branch was deleted) -- see the log above for "
            "old -> new.")
    if buckets["errors"]:
        _report_list("Failed", buckets["errors"], limit=5)
        log("  Re-run to retry, or narrow to just the failures: "
            "contextlake mirror update --repos <name>")
    return _bucket_result(buckets,
                          ok_keys=("updated", "unchanged", "switched"),
                          skipped_keys=("skipped", "empty", "dry-run"))


def switch_repository_branches(work_dir, config, gitlab_group):
    """Switch every local repository to its most active branch."""
    log("Switching repositories to most active branches...")

    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        log("No projects loaded")
        return StageResult()

    # Scoped to the group being synced. A workspace holding several groups sent
    # every other group's clone through this pass, where it fetched nothing,
    # switched nothing, and printed "⊘ <repo>: Not in GitLab list" -- an anomaly
    # report for repositories that are simply not this run's business.
    local_repos, foreign = in_group_repos(
        work_dir, filtered_local_repos(work_dir, config), gitlab_group)
    if foreign:
        log(f"  {foreign} local repo(s) belong to another group; not in scope for "
            f"--group {gitlab_group}")
    max_workers = _int(config, "max_workers", "8")

    buckets = {"switched": [], "already": [], "skipped": [], "empty": [], "dry-run": [],
               "errors": []}
    total = len(local_repos)
    progress = style.Progress(total, label="branches")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_timed, switch_repository_branch, p, projects, work_dir, config): p
            for p in local_repos
        }
        for i, fut in enumerate(as_completed(futures), 1):
            (status, path, message), duration_ms = fut.result()
            fields = _repo_fields(status, path, message, duration_ms)
            if status == "switched":
                buckets["switched"].append(path)
                log(_status(i, total, "switched", path, message), inline=True, **fields)
            elif status == "ok":
                buckets["already"].append(path)
                log(_status(i, total, "ok", path, message), inline=True, **fields)
            elif status == "skip":
                buckets["skipped"].append(path)
                log(_status(i, total, "skip", path, message), inline=True, **fields)
            elif status == "note":
                buckets["empty"].append(path)
                log(_status(i, total, "note", path, message), inline=True, **fields)
            elif status == "dry-run":
                buckets["dry-run"].append(path)
                log(_status(i, total, "dryrun", path, message), inline=True, **fields)
            else:
                buckets["errors"].append(path)
                log(_status(i, total, "fail", path, message), inline=True, **fields)
            progress.advance(path)

    progress.done()
    glyph = style.ok() if not buckets["errors"] else style.warn()
    log(f"{glyph} Branch switch complete: {_summarize(buckets)}")
    if buckets["errors"]:
        _report_list("Failed", buckets["errors"], limit=5)
        log("  Re-run to retry, or narrow to just the failures: "
            "contextlake mirror branches --repos <name>")
    return _bucket_result(buckets,
                          ok_keys=("switched", "already"),
                          skipped_keys=("skipped", "empty", "dry-run"))


def _report_list(label, items, limit=10):
    if not items:
        return
    log(f"{label}:")
    for path in items[:limit]:
        log(f"  {path}")
    if len(items) > limit:
        log(f"  ... and {len(items) - limit} more")


def _verify_summary(valid, missing, extra, invalid, nested, other_groups=0, width=None):
    """Styled, aligned glyph summary rows for `verify_structure` (pure, testable).

    Mirrors `_status_summary`'s glyph-prefixed-label treatment, but renders
    through `style.kv` instead of hand-rolled `align_right` calls.

    ``other_groups`` only earns a row when there are any: it counts repositories
    this run was not asked about, which in the ordinary single-group workspace is
    always zero and would be one more line of nothing to read past.
    """
    rows = [
        (style.green("✓"), "Valid", valid),
        (style.yellow("⚠") if missing else style.dim("·"), "Missing", missing),
        (style.yellow("⚠") if extra else style.dim("·"), "Extra", extra),
        (style.red("✗") if invalid else style.dim("·"), "Invalid", invalid),
        (style.yellow("⚠") if nested else style.dim("·"), "Nested", nested),
    ]
    if other_groups:
        # Dim, never a warning glyph: a workspace holding several groups is a
        # supported arrangement, not an anomaly.
        rows.append((style.dim("·"), "Other groups", other_groups))
    pairs = [(f"  {glyph} {label}", str(n)) for glyph, label, n in rows]
    return style.kv(pairs, width=width).splitlines()


def verify_structure(work_dir, config, gitlab_group):
    """Verify the local tree matches GitLab and flag repos nested inside repos."""
    log("Verifying repository structure...")

    # Read-only, as this command's own help promises ("compare the local
    # workspace against GitLab (read-only)" / "change nothing"): a cold cache is
    # reported, never filled by enumerating the forge. Same reasoning as status.
    projects = load_gitlab_projects(config, gitlab_group, allow_fetch=False)
    if not projects:
        log(f"{style.warn()} No projects loaded, run 'fetch' first")
        return StageResult()

    # Both sides, not just the local one: verify compares the project list
    # against the work directory, so filtering only local repos would report
    # every unmatched project as `missing` and make a scoped verify look broken.
    patterns = repo_filter_patterns(config)
    if patterns:
        exact = repo_filter_is_exact(config)
        projects = {k: v for k, v in projects.items()
                    if match_repo_filter(v.get("full_path", k), k, patterns, exact=exact)}
    # Scoped to the group being verified, for the reason `switch_repository_branches`
    # is: a workspace holding several groups had every other group's clone reported
    # as an Extra repository, which is an anomaly report about repositories this run
    # was never asked about. Only positively-attributed foreign repos drop out (see
    # `belongs_to_other_group`), so a stray clone with no readable origin is still
    # reported exactly as before.
    all_local = filtered_local_repos(work_dir, config)
    local_repos, foreign = in_group_repos(work_dir, all_local, gitlab_group)
    valid, missing, extra, invalid = [], [], [], []

    for path in set(local_repos) | set(projects.keys()):
        status, local_path, _ = verify_repository(path, projects, work_dir, config)
        {"ok": valid, "missing": missing, "extra": extra, "invalid": invalid}[status].append(
            local_path
        )

    # The UNSCOPED list, deliberately. Nesting is a property of the disk layout,
    # not of which group is being synced: a clone from another group sitting
    # inside this group's working tree is precisely the corruption this check
    # exists to catch, and scoping it here would make `verify --group A` blind to
    # exactly the case that most needs reporting.
    nested = find_nested_repos(all_local)

    for line in _verify_summary(len(valid), len(missing), len(extra), len(invalid),
                                len(nested), foreign):
        log(line)
    _report_list("Missing repositories", missing)
    _report_list("Extra repositories", extra)
    _report_list("Nested repositories (repo inside another repo)", nested)
    # Only `invalid` fails the run: a path that is in the project list and on disk
    # but has no .git is real corruption. `missing` is routine (archived projects
    # are never cloned), and `extra`/`nested` are the advisories the summary
    # already marks ⚠ -- failing on those would exit 1 on almost every real
    # workspace and train everyone to ignore the exit code.
    return StageResult(ok=len(valid), failed=len(invalid),
                       skipped=len(missing) + len(extra))


def _status_summary(active, local, synced, missing, extra, other_groups=0, width=None):
    """Styled, right-aligned glyph summary lines for `status` (pure, testable).

    ``other_groups`` only earns a row when there are any -- see `_verify_summary`.
    """
    rows = [
        (style.dim("•"), "GitLab projects (active)", active),
        (style.dim("•"), "Local repositories", local),
        (style.green("✓"), "Synchronized", synced),
        (style.yellow("⚠") if missing else style.dim("·"), "Missing", missing),
        (style.yellow("⚠") if extra else style.dim("·"), "Extra", extra),
    ]
    if other_groups:
        rows.append((style.dim("·"), "Other groups", other_groups))
    if width is None:  # widest "  glyph label" (4 visible chrome) + gap + widest count
        width = 4 + max(len(label) for _, label, _ in rows) + 2 \
            + max(len(str(n)) for _, _, n in rows)
    return [style.align_right(f"  {g} {label}", str(n), width) for g, label, n in rows]


def show_status(work_dir, config, gitlab_group):
    """Show a read-only summary of local vs GitLab state."""
    log(style.bold("Synchronization status"))

    # Read-only on purpose: never enumerate the forge from `status` (see
    # load_gitlab_projects). Nothing to report is reported, not fixed silently.
    projects = load_gitlab_projects(config, gitlab_group, allow_fetch=False)
    if not projects:
        # A cache scoped to some other --repos is not "no cache": saying so lets
        # the user re-fetch at the scope they want instead of re-running a fetch
        # that looks like it should already have worked.
        if (scope := cache_filter_conflict(config)):
            log(f"{style.warn()} The cached project list covers only --repos {scope!r}, "
                "not this run's scope -- re-run 'contextlake mirror fetch' to refresh it")
        else:
            log(f"{style.warn()} No projects loaded, run 'fetch' first")
        return

    # Say what these counts cover, and say it where counts actually follow: a
    # scoped number read as the group total is wrong exactly where it is trusted
    # most, and this is the command the docs send you to before a sync.
    patterns = repo_filter_patterns(config)
    if patterns:
        log(style.dim(f"  Scoped to --repos {','.join(patterns)}: counts below cover the "
                      "matching repositories, not the whole group"))

    # BOTH sides of the comparison, as `verify` already does. status compares the
    # project list against the work directory, so narrowing one side and not the
    # other invents a difference that is not there: a fully-synced workspace
    # reported every non-matching clone as an Extra repository.
    # And scoped to the group, as `verify` is: a workspace holding several groups
    # had every other group's clone counted as both a Local repository and an Extra
    # one, so a fully-synced workspace read as full of anomalies.
    scoped, foreign = in_group_repos(
        work_dir, filtered_local_repos(work_dir, config), gitlab_group)
    local_repos = set(scoped)
    active_projects = {k: v for k, v in projects.items() if not v["archived"]}

    synchronized = [p for p in active_projects if p in local_repos]
    missing = [p for p in active_projects if p not in local_repos]
    extra = [p for p in local_repos if p not in active_projects]

    for line in _status_summary(len(active_projects), len(local_repos),
                                len(synchronized), len(missing), len(extra), foreign):
        log(line)
    _report_list("Missing repositories", missing)
    _report_list("Extra repositories", extra)
