"""
Branch safety functions for contextlake
"""

import os
import subprocess


def has_uncommitted_changes(full_path):
    """Check if repository has uncommitted changes."""
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            capture_output=True, text=True, cwd=full_path
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def get_current_branch(full_path):
    """Get the current branch of a repository."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, cwd=full_path
        )
        return result.stdout.strip()
    except Exception:
        return None


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
        warnings.append("Uncommitted changes detected")

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
