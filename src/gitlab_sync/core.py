"""
Core git operations for gitlab_sync
"""

import json
import os
import random
import shutil
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .config import get_cache_paths
from .logging_setup import log
from .safety import check_repository_safety, is_safe_branch, stash_changes


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
    """Classify error type for retry strategy."""
    error_msg = error_msg.lower()
    if 'eof' in error_msg or 'connection reset' in error_msg or 'broken pipe' in error_msg:
        return 'network'
    elif 'timeout' in error_msg or 'timed out' in error_msg:
        return 'timeout'
    elif 'lookup' in error_msg or 'dns' in error_msg:
        return 'dns'
    elif 'tls' in error_msg or 'ssl' in error_msg or 'handshake' in error_msg:
        return 'tls'
    else:
        return 'other'


def retry_with_backoff(func, *args, max_retries=3, backoff_initial=1, backoff_max=30, **kwargs):
    """Retry ``func`` with exponential backoff and jitter.

    Network/timeout/transient errors are retried; DNS and TLS errors are treated
    as non-transient and fail fast. The last error is re-raised on exhaustion.
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if classify_error(str(e)) in ('dns', 'tls'):
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

def fetch_gitlab_projects(gitlab_group, config):
    """Fetch all projects in a GitLab group (incl. subgroups) via the glab API.

    Results are written to two caches under ``cache_dir``: a JSON map keyed by
    ``path_with_namespace`` and a pipe-delimited text file
    (``path|ssh|http|default_branch|archived``) for quick human/script use.
    """
    cache_file, cache_json = get_cache_paths(config)
    log(f"Fetching GitLab projects for group: {gitlab_group}")

    all_projects = {}
    page = 1
    per_page = 100
    group_enc = urllib.parse.quote(gitlab_group, safe="")

    while True:
        endpoint = (
            f"groups/{group_enc}/projects"
            f"?include_subgroups=true&archived=false&per_page={per_page}&page={page}"
        )
        try:
            result = subprocess.run(
                ["glab", "api", endpoint], capture_output=True, text=True
            )
        except FileNotFoundError:
            log("ERROR: 'glab' not found. Install the GitLab CLI and run 'glab auth login'.")
            break

        if result.returncode != 0:
            log(f"Error fetching projects (page {page}): {result.stderr.strip()}")
            break

        try:
            projects = json.loads(result.stdout)
        except json.JSONDecodeError:
            log(f"Error: could not parse glab output on page {page}")
            break

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
                }

        log(f"Fetched page {page}, total projects: {len(all_projects)}")
        if len(projects) < per_page:
            break
        page += 1

    _write_caches(all_projects, cache_json, cache_file)
    log(f"Fetched {len(all_projects)} total projects")
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

def _build_clone_cmd(project_path, http_url, full_path, method):
    """Choose the clone command. Prefer glab (uses its auth) unless told otherwise."""
    use_glab = method == "glab" or (method == "auto" and shutil.which("glab") is not None)
    if use_glab and project_path:
        return ["glab", "repo", "clone", project_path, full_path]
    return ["git", "clone", http_url, full_path]


def _clone_once(clone_cmd, timeout):
    """Run a single clone attempt, raising on failure so retry can engage."""
    result = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=timeout)
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
    clone_cmd = _build_clone_cmd(gitlab_path, http, full_path, method)

    try:
        retry_with_backoff(
            _clone_once, clone_cmd, clone_timeout,
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

def _rev_parse(full_path, ref="HEAD"):
    res = subprocess.run(
        ["git", "rev-parse", ref], capture_output=True, text=True, cwd=full_path
    )
    return res.stdout.strip()


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
                    log(f"⚠ {local_path}: {stash_msg}")
                else:
                    return ("skip", local_path, f'Skipped (unsafe: {", ".join(warnings)})')
            else:
                return ("skip", local_path, f'Skipped (unsafe: {", ".join(warnings)})')

        curr_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=full_path,
        )
        current = curr_res.stdout.strip()
        if current == "HEAD":
            return ("skip", local_path, "Detached HEAD")

        if dry_run:
            return ("dry-run", local_path, f"Would update {current}")

        subprocess.run(
            ["git", "fetch", "--all", "--quiet"],
            capture_output=True, cwd=full_path, timeout=fetch_timeout,
        )

        before = _rev_parse(full_path, "HEAD")
        res = subprocess.run(
            ["git", "pull", "--quiet", "origin", current],
            capture_output=True, text=True, cwd=full_path, timeout=pull_timeout,
        )
        if res.returncode != 0:
            return ("error", local_path, (res.stderr or "pull failed").strip()[:200])

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

        subprocess.run(
            ["git", "fetch", "--all", "--quiet"],
            capture_output=True, cwd=full_path, timeout=fetch_timeout,
        )

        curr_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=full_path,
        )
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

        subprocess.run(
            ["git", "checkout", "--quiet", most_active],
            capture_output=True, cwd=full_path, timeout=branch_timeout,
        )
        subprocess.run(
            ["git", "pull", "--quiet", "origin", most_active],
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

    log("Clone complete: " + _summarize({
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

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(update_repository, p, work_dir, config): p for p in local_repos}
        for i, fut in enumerate(as_completed(futures), 1):
            status, path, message = fut.result()
            if status == "ok":
                buckets["updated"].append(path)
                log(f"[{i}/{total}] ✓ {path}: {message}")
            elif status == "nochange":
                buckets["unchanged"].append(path)
                log(f"[{i}/{total}] = {path}: {message}")
            elif status == "skip":
                buckets["skipped"].append(path)
                log(f"[{i}/{total}] ⊘ {path}: {message}")
            elif status == "dry-run":
                buckets["dry-run"].append(path)
                log(f"[{i}/{total}] ~ {path}: {message}")
            else:
                buckets["errors"].append(path)
                log(f"[{i}/{total}] ✗ {path}: {message}")

    log("Update complete: " + _summarize(buckets))


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

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(switch_repository_branch, p, projects, work_dir, config): p
            for p in local_repos
        }
        for i, fut in enumerate(as_completed(futures), 1):
            status, path, message = fut.result()
            if status == "switched":
                buckets["switched"].append(path)
                log(f"[{i}/{total}] ↝ {path}: {message}")
            elif status == "ok":
                buckets["already"].append(path)
                log(f"[{i}/{total}] ✓ {path}: {message}")
            elif status == "skip":
                buckets["skipped"].append(path)
                log(f"[{i}/{total}] ⊘ {path}: {message}")
            elif status == "dry-run":
                buckets["dry-run"].append(path)
                log(f"[{i}/{total}] ~ {path}: {message}")
            else:
                buckets["errors"].append(path)
                log(f"[{i}/{total}] ✗ {path}: {message}")

    log("Branch switch complete: " + _summarize(buckets))


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


def show_status(work_dir, config, gitlab_group):
    """Show a read-only summary of local vs GitLab state."""
    log("Current synchronization status:")

    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        log("No projects loaded - run 'fetch' first")
        return

    local_repos = set(get_local_repos(work_dir))
    active_projects = {k: v for k, v in projects.items() if not v["archived"]}

    synchronized = [p for p in active_projects if p in local_repos]
    missing = [p for p in active_projects if p not in local_repos]
    extra = [p for p in local_repos if p not in active_projects]

    log(f"GitLab projects (active): {len(active_projects)}")
    log(f"Local repositories: {len(local_repos)}")
    log(f"Synchronized: {len(synchronized)}")
    log(f"Missing: {len(missing)}")
    log(f"Extra: {len(extra)}")
    _report_list("Missing repositories", missing)
    _report_list("Extra repositories", extra)
