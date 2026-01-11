"""
Branch safety functions for gitlab_sync
"""

import subprocess
import os


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
    """
    Check repository safety before operations.
    Returns (safe, warning_message) tuple.
    """
    full_path = os.path.join(work_dir, local_path)
    protect_working_branches = config.get('protect_working_branches', 'true').lower() == 'true'
    require_clean_workspace = config.get('require_clean_workspace', 'true').lower() == 'true'
    
    warnings = []
    
    if protect_working_branches:
        current_branch = get_current_branch(full_path)
        if current_branch and not is_safe_branch(current_branch, config):
            warnings.append(f"On working branch: {current_branch}")
    
    if require_clean_workspace:
        if has_uncommitted_changes(full_path):
            warnings.append("Uncommitted changes detected")
    
    return len(warnings) == 0, warnings


def stash_changes(full_path, config):
    """Stash changes in a repository."""
    auto_stash = config.get('auto_stash', 'false').lower() == 'true'
    if not auto_stash:
        return False, "Auto-stash disabled in config"
    
    try:
        result = subprocess.run(
            ['git', 'stash', 'push', '-m', 'gitlab_sync_auto_stash'],
            capture_output=True, text=True, cwd=full_path
        )
        if result.returncode == 0:
            return True, "Changes stashed successfully"
        else:
            return False, result.stderr.strip()
    except Exception as e:
        return False, str(e)[:100]
