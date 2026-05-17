"""
Branch safety functions for contextlake
"""

import os
import subprocess
from subprocess import SubprocessError


def has_uncommitted_changes(full_path):
    """Return True if the repo has uncommitted changes -- OR if that can't be
    determined.

    This gates destructive operations (update / stash / merge), so it FAILS
    CLOSED: a git error, a timeout, a non-zero exit, or a path that is not a git
    repo is reported as "dirty" rather than "clean". Reading an unknown working
    tree as clean is exactly the bug that loses local work.
    """
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            capture_output=True, text=True, cwd=full_path, timeout=30,
        )
    except (OSError, SubprocessError):
        return True   # can't tell -> treat as unsafe to modify
    if result.returncode != 0:
        return True   # not a repo / git failed -> unsafe
    return bool(result.stdout.strip())


def get_current_branch(full_path):
    """Get the current branch of a repository, or None if it can't be read.

    None is the fail-closed value: callers feed it to ``is_safe_branch`` which
    treats None as unsafe, so an unreadable branch never enables a branch switch.
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, cwd=full_path, timeout=30,
        )
    except (OSError, SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_safe_branch(branch, config):
    """Check if branch is considered safe for automatic operations."""
    if branch is None or branch == 'HEAD':
        return False
    safe_branches = config.get('safe_branches', 'main,master,develop,development').split(',')
    return branch in safe_branches


def check_repository_safety(local_path, work_dir, config):
    """Check whether automated git operations may safely touch this repo.

    The only genuinely unsafe condition for a fetch/pull is a *dirty working
    tree* (uncommitted, unstaged, or untracked changes) -- those could be lost
    or block a merge. Simply being on a feature ("working") branch is NOT
    unsafe: pulling the branch you are already on is fine. Protecting a working
    branch from being *switched away* is a separate concern, enforced only in
    the branch-switch path (see ``switch_repository_branch`` via
    ``protect_working_branches`` + ``is_safe_branch``) -- that is the operation
    that would actually disrupt a checked-out working branch.

    Returns an ``(is_safe, warnings)`` tuple.
    """
    full_path = os.path.join(work_dir, local_path)
    require_clean_workspace = config.get('require_clean_workspace', 'true').lower() == 'true'

    warnings = []

    if require_clean_workspace and has_uncommitted_changes(full_path):
        warnings.append("Uncommitted changes (or indeterminate working-tree state)")

    return len(warnings) == 0, warnings


def stash_changes(full_path, config):
    """Stash changes in a repository."""
    auto_stash = config.get('auto_stash', 'false').lower() == 'true'
    if not auto_stash:
        return False, "Auto-stash disabled in config"

    try:
        result = subprocess.run(
            ['git', 'stash', 'push', '-m', 'contextlake_auto_stash'],
            capture_output=True, text=True, cwd=full_path
        )
        if result.returncode == 0:
            return True, "Changes stashed successfully"
        else:
            return False, result.stderr.strip()
    except Exception as e:
        return False, str(e)[:100]
