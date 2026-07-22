"""
Core git operations for contextlake
"""

import base64
import json
import os
import random
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import partial

from . import style
from .config import get_cache_paths
from .logging_setup import log
from .safety import check_repository_safety, is_safe_branch, stash_changes


def _status(i, total, glyph, path, message):
    """A coloured per-repo progress line: dim counter, coloured glyph, cyan path."""
    return f"{style.dim(f'[{i}/{total}]')} {glyph} {style.cyan(path)}: {message}"


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


def classify_error(error_msg):
    """Classify a git/network error to drive the retry strategy.

    Transient categories (network/timeout) are retried. The rest fail fast:
    dns/tls won't recover on retry, and the two expected "the remote moved"
    states are not errors to retry but states to report -- ``missing-ref`` (the
    upstream branch was deleted) and ``diverged`` (local and remote both moved).
    """
    error_msg = error_msg.lower()
    # 'eof' is checked first so a "TLS ... unexpected eof" (a dropped connection,
    # not a cert failure) is treated as transient/network rather than tls.
    if 'eof' in error_msg or 'connection reset' in error_msg or 'broken pipe' in error_msg:
        return 'network'
    if "couldn't find remote ref" in error_msg or 'unknown revision' in error_msg:
        return 'missing-ref'
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


def retry_with_backoff(func, *args, max_retries=3, backoff_initial=1, backoff_max=30, **kwargs):
    """Retry ``func`` with exponential backoff and jitter.

    Network/timeout/transient errors are retried; DNS, TLS, deleted-upstream and
    diverged-branch errors are non-transient and fail fast. The last error is
    re-raised on exhaustion.
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except FileNotFoundError:
            raise  # a missing binary/path never recovers on retry
        except Exception as e:
            last_error = e
            if classify_error(str(e)) in ('dns', 'tls', 'missing-ref', 'diverged'):
                break
            if attempt < max_retries - 1:
                backoff = min(backoff_initial * (2 ** attempt), backoff_max)
                time.sleep(backoff * random.uniform(0.5, 1.5))
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
    req = urllib.request.Request(
        url, headers={"PRIVATE-TOKEN": token, "User-Agent": "contextlake"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fetch_projects_page_glab(group_enc, per_page, page):
    """One page via the ``glab`` CLI (uses glab's own auth). Raises on failure."""
    result = subprocess.run(
        ["glab", "api", _projects_endpoint(group_enc, per_page, page)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "glab api failed")
    return json.loads(result.stdout)


def _get_json(url, headers, timeout):
    """GET a JSON document. Raises on HTTP/network error so retry can engage."""
    req = urllib.request.Request(url, headers={"User-Agent": "contextlake", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def _repo_filter_patterns(config) -> list[str]:
    """Comma-separated ``--repos`` / ``repo_filter`` patterns, or [] if unset."""
    raw = (config.get("repo_filter") or "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def match_repo_filter(full_path: str, local_path: str, patterns: list[str]) -> bool:
    """A repo matches if any pattern is a glob hit or a plain substring of its
    group-qualified path or its local (group-stripped) path. Case-insensitive.
    ``team/*``, ``billing``, and ``acme/orders-api`` all work."""
    from fnmatch import fnmatch
    fp, lp = (full_path or "").lower(), (local_path or "").lower()
    for p in patterns:
        pl = p.lower()
        if pl in fp or pl in lp or fnmatch(fp, pl) or fnmatch(lp, pl):
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
    log(f"Fetching {platform} projects for: {style.cyan(gitlab_group)}")

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
        log(f"Enumerating via the {platform} REST API at {base} ({auth})")
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
        except FileNotFoundError as e:
            # Raise instead of writing what we have: a partial (or empty) result must
            # never replace a good cache under a green checkmark.
            log("ERROR: 'glab' not found and no GITLAB_TOKEN set. Set GITLAB_TOKEN "
                "(a read_api token), or install the GitLab CLI and run 'glab auth login'.")
            raise FetchError(
                "could not enumerate GitLab projects: 'glab' not found and no "
                "GITLAB_TOKEN set (existing caches left untouched)") from e
        except Exception as e:  # noqa: BLE001 - a hard failure must surface, not truncate
            log(f"Error fetching projects (page {page}): {e}")
            raise FetchError(
                f"could not enumerate GitLab projects (failed on page {page}: {e}); "
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
    patterns = _repo_filter_patterns(config)
    if patterns:
        before = len(all_projects)
        all_projects = {k: v for k, v in all_projects.items()
                        if match_repo_filter(v.get("full_path", k), k, patterns)}
        log(style.dim(f"Repo filter {patterns} -> {len(all_projects)} of {before} projects"))

    _write_caches(all_projects, cache_json, cache_file)
    if not all_projects:
        if patterns:
            log(style.warn(f"No projects matched --repos {patterns} — "
                           "check the pattern against `contextlake status`"))
        else:
            log(style.warn("Fetched 0 projects — check the group name and your token's "
                           "read_api access before trusting this result"))
    else:
        label = "matching" if patterns else "total"
        log(f"{style.ok()} Fetched {style.bold(str(len(all_projects)))} {label} projects")
    return all_projects


def _write_caches(all_projects, cache_json, cache_file):
    """Persist the project map as JSON and as a pipe-delimited text cache."""
    os.makedirs(os.path.dirname(cache_json) or ".", exist_ok=True)
    with open(cache_json, "w") as f:
        json.dump(all_projects, f, indent=2)
    with open(cache_file, "w") as f:
        for path, p in all_projects.items():
            f.write(f"{path}|{p['ssh']}|{p['http']}|{p['default_branch']}|{p['archived']}\n")


def load_gitlab_projects(config, gitlab_group):
    """Load the cached project map, normalizing legacy list-shaped JSON.

    Falls back to a fresh fetch when no usable cache exists.
    """
    _, cache_json = get_cache_paths(config)

    if os.path.exists(cache_json):
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
            return normalized
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
                return normalized

    log("Cache not found or invalid, fetching fresh data...")
    return fetch_gitlab_projects(gitlab_group, config)


def get_local_repos(work_dir):
    """Return repo paths (relative to work_dir) for every directory with a .git."""
    local_repos = []
    for root, dirs, _files in os.walk(work_dir):
        if ".git" in dirs:
            local_repos.append(os.path.relpath(root, work_dir))
    return local_repos


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


def _clone_once(clone_cmd, timeout, env=None):
    """Run a single clone attempt, raising on failure so retry can engage."""
    result = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=timeout,
                            env=env)
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
            _clone_once, clone_cmd, clone_timeout, env=clone_env,
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
        ["git", "rev-parse", ref], capture_output=True, text=True, cwd=full_path, timeout=timeout
    )
    if res.returncode != 0:
        raise RuntimeError((res.stderr or "git rev-parse failed").strip())
    return res.stdout.strip()


def _run_git(args, cwd, timeout):
    """Run a git command, raising ``RuntimeError(stderr)`` on a non-zero exit.

    Raising (rather than returning a code) lets ``retry_with_backoff`` see the
    git error text via ``classify_error`` and decide whether to retry.
    """
    res = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "git command failed").strip())
    return res


def _first_line(text, limit=200):
    """First non-empty line of ``text``, trimmed -- keeps multi-line git output
    (e.g. the 'divergent branches' hint) from spilling into a one-line status."""
    stripped = (text or "").strip()
    return stripped.splitlines()[0][:limit] if stripped else ""


def _fetch_with_retry(git_args, full_path, fetch_timeout, config):
    """Fetch with exponential-backoff retry on transient proxy/network drops."""
    retry_with_backoff(
        _run_git, git_args, full_path, fetch_timeout,
        max_retries=_int(config, "max_retries", "3"),
        backoff_initial=_float(config, "backoff_initial", "1"),
        backoff_max=_float(config, "backoff_max", "30"),
    )


def update_repository(local_path, work_dir, config):
    """Fetch + fast-forward a single repo's current branch (safety-gated)."""
    full_path = os.path.join(work_dir, local_path)
    fetch_timeout = _int(config, "fetch_timeout", "60")
    pull_timeout = _int(config, "pull_timeout", "60")
    dry_run = _is_truthy(config, "dry_run")

    try:
        safe, warnings = check_repository_safety(local_path, work_dir, config)
        if not safe:
            has_changes = any("Uncommitted changes" in w for w in warnings)
            if has_changes and not dry_run:
                stash_success, stash_msg = stash_changes(full_path, config)
                if stash_success:
                    log(f"{style.yellow('⚠')} {style.cyan(local_path)}: {stash_msg}")
                else:
                    return ("skip", local_path, f'Skipped (unsafe: {", ".join(warnings)})')
            else:
                return ("skip", local_path, f'Skipped (unsafe: {", ".join(warnings)})')

        # _run_git raises on a non-zero exit, so a failed branch read surfaces as a
        # clean per-repo error instead of an empty string that fetches branch "".
        curr_res = _run_git(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], full_path, 30
        )
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
            if classify_error(str(e)) == "missing-ref":
                return ("skip", local_path, f"Upstream branch deleted: {current}")
            return ("error", local_path, _first_line(str(e)))

        before = _rev_parse(full_path, "HEAD")
        # Fast-forward only: a mirror never merges or rebases. A branch that has
        # diverged from origin is reported cleanly rather than dumping git's
        # multi-line "divergent branches" hint into the output.
        merge = subprocess.run(
            ["git", "merge", "--ff-only", "--quiet", "FETCH_HEAD"],
            capture_output=True, text=True, cwd=full_path, timeout=pull_timeout,
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
        return ("error", local_path, str(e)[:200])


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
        capture_output=True, text=True, cwd=full_path, timeout=branch_timeout,
    )
    # A git failure here must not masquerade as "No branches found" (a skip);
    # raise so the caller reports a real error instead of silently mis-selecting.
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "git for-each-ref failed").strip())
    branch_info = []
    for line in result.stdout.strip().split("\n"):
        if not line or "HEAD" in line:
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        branch = parts[0].replace("origin/", "")
        count_res = subprocess.run(
            ["git", "rev-list", "--count", f"origin/{branch}"],
            capture_output=True, text=True, cwd=full_path, timeout=branch_timeout,
        )
        count = int(count_res.stdout.strip()) if count_res.stdout.strip().isdigit() else 0
        branch_info.append({"name": branch, "count": count, "ts": _parse_iso(parts[1])})
    return branch_info


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
            return ("error", local_path, _first_line(str(e)))

        try:
            curr_res = _run_git(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], full_path, branch_timeout
            )
        except Exception:  # noqa: BLE001
            # A freshly-cloned repo with no commits has no HEAD to resolve
            # (git: "ambiguous argument 'HEAD'"). There is no branch to switch
            # to, so skip it cleanly instead of reporting it as an error.
            return ("skip", local_path, "Empty repo (no commits)")
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
            capture_output=True, text=True, cwd=full_path, timeout=branch_timeout,
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
        return ("error", local_path, str(e)[:200])


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


def clone_missing_repos(work_dir, config, gitlab_group):
    """Clone every active GitLab project that is not already present locally."""
    log("Cloning missing repositories...")

    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        return

    local_repos = set(get_local_repos(work_dir))
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
    log(f"Active GitLab projects: {active_count}")
    log(f"Already cloned locally: {len(local_repos)}")
    log(f"To clone: {len(to_clone)}")
    if not to_clone:
        log("No missing repositories to clone")
        return

    successes, skipped, failures, dry = [], [], [], []
    done = 0
    total = len(to_clone)
    progress = style.Progress(total, label="clone")

    def handle(result):
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
        log(f"[{done}/{total}] {path}: {message}")
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
                    ex.submit(clone_repository, it["local_path"], it["gitlab_path"],
                              it["http"], it["ssh"], work_dir, config)
                    for it in batch
                ]
                for fut in as_completed(futures):
                    pool.record_result(handle(fut.result()))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [
                ex.submit(clone_repository, it["local_path"], it["gitlab_path"],
                          it["http"], it["ssh"], work_dir, config)
                for it in to_clone
            ]
            for fut in as_completed(futures):
                handle(fut.result())

    progress.done()
    log(style.ok("Clone complete: ") + _summarize({
        "successful": successes, "skipped": skipped, "dry-run": dry, "failed": failures,
    }))


def update_repositories(work_dir, config):
    """Update every local repository."""
    log("Updating all repositories...")

    local_repos = get_local_repos(work_dir)
    max_workers = _int(config, "max_workers", "8")
    log(f"Found {len(local_repos)} local repositories")

    buckets = {"updated": [], "unchanged": [], "skipped": [], "dry-run": [], "errors": []}
    total = len(local_repos)
    progress = style.Progress(total, label="update")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(update_repository, p, work_dir, config): p for p in local_repos}
        for i, fut in enumerate(as_completed(futures), 1):
            status, path, message = fut.result()
            if status == "ok":
                buckets["updated"].append(path)
                log(_status(i, total, style.green("✓"), path, message))
            elif status == "nochange":
                buckets["unchanged"].append(path)
                log(_status(i, total, style.dim("="), path, message))
            elif status == "skip":
                buckets["skipped"].append(path)
                log(_status(i, total, style.dim("⊘"), path, message))
            elif status == "dry-run":
                buckets["dry-run"].append(path)
                log(_status(i, total, style.yellow("~"), path, message))
            else:
                buckets["errors"].append(path)
                log(_status(i, total, style.red("✗"), path, message))
            progress.advance(path)

    progress.done()
    log(style.ok("Update complete: ") + _summarize(buckets))


def switch_repository_branches(work_dir, config, gitlab_group):
    """Switch every local repository to its most active branch."""
    log("Switching repositories to most active branches...")

    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        log("No projects loaded")
        return

    local_repos = get_local_repos(work_dir)
    max_workers = _int(config, "max_workers", "8")

    buckets = {"switched": [], "already": [], "skipped": [], "dry-run": [], "errors": []}
    total = len(local_repos)
    progress = style.Progress(total, label="branches")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(switch_repository_branch, p, projects, work_dir, config): p
            for p in local_repos
        }
        for i, fut in enumerate(as_completed(futures), 1):
            status, path, message = fut.result()
            if status == "switched":
                buckets["switched"].append(path)
                log(_status(i, total, style.cyan("↝"), path, message))
            elif status == "ok":
                buckets["already"].append(path)
                log(_status(i, total, style.green("✓"), path, message))
            elif status == "skip":
                buckets["skipped"].append(path)
                log(_status(i, total, style.dim("⊘"), path, message))
            elif status == "dry-run":
                buckets["dry-run"].append(path)
                log(_status(i, total, style.yellow("~"), path, message))
            else:
                buckets["errors"].append(path)
                log(_status(i, total, style.red("✗"), path, message))
            progress.advance(path)

    progress.done()
    log(style.ok("Branch switch complete: ") + _summarize(buckets))


def _report_list(label, items, limit=10):
    if not items:
        return
    log(f"{label}:")
    for path in items[:limit]:
        log(f"  {path}")
    if len(items) > limit:
        log(f"  ... and {len(items) - limit} more")


def verify_structure(work_dir, config, gitlab_group):
    """Verify the local tree matches GitLab and flag repos nested inside repos."""
    log("Verifying repository structure...")

    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        log("No projects loaded")
        return

    local_repos = get_local_repos(work_dir)
    valid, missing, extra, invalid = [], [], [], []

    for path in set(local_repos) | set(projects.keys()):
        status, local_path, _ = verify_repository(path, projects, work_dir, config)
        {"ok": valid, "missing": missing, "extra": extra, "invalid": invalid}[status].append(
            local_path
        )

    nested = find_nested_repos(local_repos)

    log(
        f"Verification complete: {len(valid)} valid, {len(missing)} missing, "
        f"{len(extra)} extra, {len(invalid)} invalid, {len(nested)} nested"
    )
    _report_list("Missing repositories", missing)
    _report_list("Extra repositories", extra)
    _report_list("Nested repositories (repo inside another repo)", nested)


def _status_summary(active, local, synced, missing, extra, width=None):
    """Styled, right-aligned glyph summary lines for `status` (pure, testable)."""
    rows = [
        (style.dim("•"), "GitLab projects (active)", active),
        (style.dim("•"), "Local repositories", local),
        (style.green("✓"), "Synchronized", synced),
        (style.yellow("⚠") if missing else style.dim("·"), "Missing", missing),
        (style.yellow("⚠") if extra else style.dim("·"), "Extra", extra),
    ]
    if width is None:  # widest "  glyph label" (4 visible chrome) + gap + widest count
        width = 4 + max(len(label) for _, label, _ in rows) + 2 \
            + max(len(str(n)) for _, _, n in rows)
    return [style.align_right(f"  {g} {label}", str(n), width) for g, label, n in rows]


def show_status(work_dir, config, gitlab_group):
    """Show a read-only summary of local vs GitLab state."""
    log(style.bold("Synchronization status"))

    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        log(f"{style.warn()} No projects loaded, run 'fetch' first")
        return

    local_repos = set(get_local_repos(work_dir))
    active_projects = {k: v for k, v in projects.items() if not v["archived"]}

    synchronized = [p for p in active_projects if p in local_repos]
    missing = [p for p in active_projects if p not in local_repos]
    extra = [p for p in local_repos if p not in active_projects]

    for line in _status_summary(len(active_projects), len(local_repos),
                                len(synchronized), len(missing), len(extra)):
        log(line)
    _report_list("Missing repositories", missing)
    _report_list("Extra repositories", extra)
