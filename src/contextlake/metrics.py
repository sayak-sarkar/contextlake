"""Post-sync repo audit: emptiness/skeleton classification + age/activity metrics.

After a sync (or on demand via ``contextlake audit``) this scans every local clone
and reports which repos are effectively empty (no commits, or only a template
README/boilerplate) and how old / how active each repo is. Creation and activity
dates come from the GitLab API (captured during fetch); the last-commit date comes
from the local clone (authoritative for what's checked out). All git work is local,
read-only, and parallelised.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import style
from .config import get_cache_paths
from .core import get_local_repos, to_local_path
from .logging_setup import log

# Files that don't count as "real content" — a repo with only these is a skeleton.
_META_STEMS = {"readme", "license", "licence", "copying", "contributing",
               "code_of_conduct", "changelog", "authors", "notice", "codeowners"}
_META_DOTFILES = {".gitignore", ".gitattributes", ".gitmodules", ".editorconfig", ".gitkeep"}

CLASS_ORDER = ("content", "boilerplate", "readme-only", "empty")


def _is_meta(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    if base in _META_DOTFILES:
        return True
    return base.split(".", 1)[0] in _META_STEMS


def _is_readme(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    return base.split(".", 1)[0] == "readme"


def classify(files: list[str], has_head: bool) -> str:
    """empty | readme-only | boilerplate | content (from the tracked file list)."""
    if not has_head or not files:
        return "empty"
    if any(not _is_meta(f) for f in files):
        return "content"
    # only meta files remain: distinguish the classic single-README template
    if len(files) == 1 and _is_readme(files[0]):
        return "readme-only"
    return "boilerplate"


def _parse_dt(s: str | None):
    """Tolerantly parse an ISO-8601 timestamp (git %cI or GitLab) to aware datetime."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    s = re.sub(r"\.\d+", "", s)  # drop fractional seconds (version-portable)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _git_facts(path: str, run, timeout: int = 15):
    """(has_head, tracked_files, last_commit_iso, root_commit_iso) for a clone."""
    head = run(["git", "-C", path, "rev-parse", "HEAD"], timeout=timeout)
    if head.returncode != 0:
        return False, [], None, None
    files = [ln for ln in run(["git", "-C", path, "ls-files"],
                              timeout=timeout).stdout.splitlines() if ln]
    last = run(["git", "-C", path, "log", "-1", "--format=%cI"],
               timeout=timeout).stdout.strip() or None
    roots = run(["git", "-C", path, "log", "--max-parents=0", "--format=%cI"],
                timeout=timeout).stdout.split()
    first = min(roots) if roots else None  # earliest root commit (merged histories)
    return True, files, last, first


def _run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:  # treat as a repo with no facts
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def scan_repo_metrics(work_dir: str, projects: dict | None = None,
                      max_workers: int = 8, run=_run) -> list[dict]:
    """Scan every local clone under ``work_dir`` and return a metric record each.

    ``projects`` is the GitLab project map (from the fetch cache); when present its
    ``created_at`` / ``last_activity_at`` enrich each record, else dates fall back to
    the local git history.
    """
    projects = projects or {}
    repos = get_local_repos(work_dir)

    def scan_one(rel: str) -> dict:
        has_head, files, last_commit, root = _git_facts(os.path.join(work_dir, rel), run)
        meta = projects.get(rel, {})
        created = meta.get("created_at") or root
        return {
            "repo": rel,
            "full_path": meta.get("full_path", rel),
            "classification": classify(files, has_head),
            "tracked_files": len(files),
            "created": created,
            "created_source": ("gitlab" if meta.get("created_at")
                               else "git" if root else "unknown"),
            "last_commit": last_commit,
            "last_activity": meta.get("last_activity_at"),
            "default_branch": meta.get("default_branch"),
            "archived": bool(meta.get("archived", False)),
        }

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        futs = [ex.submit(scan_one, rel) for rel in repos]
        for f in as_completed(futs):
            results.append(f.result())
    results.sort(key=lambda m: m["repo"])
    return results


def _age_days(iso: str | None, now: datetime) -> int | None:
    dt = _parse_dt(iso)
    return None if dt is None else (now - dt).days


def summarize(metrics: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    by_class = Counter(m["classification"] for m in metrics)
    created = sorted(d for m in metrics if (d := _parse_dt(m["created"])))
    last = sorted(d for m in metrics if (d := _parse_dt(m["last_commit"])))
    stale_1y = sum(1 for m in metrics
                   if (a := _age_days(m["last_commit"], now)) is not None and a > 365)
    stale_2y = sum(1 for m in metrics
                   if (a := _age_days(m["last_commit"], now)) is not None and a > 730)
    return {
        "total": len(metrics),
        "by_class": dict(by_class),
        "archived": sum(1 for m in metrics if m["archived"]),
        "no_commits": by_class.get("empty", 0),
        "oldest_created": created[0].date().isoformat() if created else None,
        "newest_created": created[-1].date().isoformat() if created else None,
        "created_over_2y": sum(1 for m in metrics
                               if (a := _age_days(m["created"], now)) is not None and a > 730),
        "freshest_commit": last[-1].date().isoformat() if last else None,
        "stale_over_1y": stale_1y,
        "stale_over_2y": stale_2y,
    }


def report_repo_metrics(metrics: list[dict], *, report_path: str | Path | None = None,
                        now: datetime | None = None) -> dict:
    """Log an aggregate summary and (optionally) write per-repo JSON + CSV files."""
    s = summarize(metrics, now=now)
    bc = s["by_class"]
    log("")
    log(style.bold("Repo audit") + f"  ({s['total']} repos scanned)")
    log(f"  {style.cyan('content')}: {bc.get('content', 0)} · "
        f"readme-only: {bc.get('readme-only', 0)} · "
        f"boilerplate: {bc.get('boilerplate', 0)} · "
        f"empty: {bc.get('empty', 0)}   (archived: {s['archived']})")
    skeleton = bc.get("empty", 0) + bc.get("readme-only", 0) + bc.get("boilerplate", 0)
    log(f"  near-empty (empty + readme-only + boilerplate): {skeleton}")
    if s["oldest_created"]:
        log(f"  created: oldest {s['oldest_created']} · newest {s['newest_created']} "
            f"· {s['created_over_2y']} older than 2y")
    if s["freshest_commit"]:
        log(f"  last commit: freshest {s['freshest_commit']} · "
            f"{s['stale_over_1y']} stale >1y ({s['stale_over_2y']} >2y) · "
            f"{s['no_commits']} with no commits")

    # name the near-empty repos (capped) so they're actionable from the console
    flagged = [m["repo"] for m in metrics
               if m["classification"] in ("empty", "readme-only")]
    if flagged:
        shown = ", ".join(flagged[:8])
        more = f" (+{len(flagged) - 8} more)" if len(flagged) > 8 else ""
        log(f"  empty/readme-only: {shown}{more}")

    if report_path:
        p = Path(report_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"summary": s, "repos": metrics}, indent=2))
        csv_path = p.with_suffix(".csv")
        cols = ["repo", "classification", "tracked_files", "created", "created_source",
                "last_commit", "last_activity", "default_branch", "archived"]
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(metrics)
        log(f"  {style.ok('Wrote')} per-repo audit to {p} and {csv_path}")
    return s


def _cached_projects(config: dict, gitlab_group: str) -> dict:
    """Read the fetch cache directly (no network) and key it by local path."""
    _, cache_json = get_cache_paths(config)
    if not os.path.exists(cache_json):
        return {}
    try:
        data = json.loads(Path(cache_json).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, val in data.items():
        if isinstance(val, dict):
            out[to_local_path(val.get("full_path", key), gitlab_group)] = val
    return out


def run_audit(work_dir: str, config: dict, gitlab_group: str,
              report_path: str | Path | None = None, max_workers: int = 8) -> dict:
    """Convenience entry point: read the project cache (offline), scan, and report."""
    projects = _cached_projects(config, gitlab_group)
    metrics = scan_repo_metrics(work_dir, projects, max_workers=max_workers)
    return report_repo_metrics(metrics, report_path=report_path)
