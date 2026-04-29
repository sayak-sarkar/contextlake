#!/usr/bin/env python3
"""
GitLab Workspace Synchronization CLI Tool

Automatically keeps the local workspace synchronized with GitLab repositories.
This tool ensures all repositories are cloned locally, updated, and on their
most active development branches.

Usage:
    python3 gitlab_sync.py [command] [options]

Commands:
    fetch       Fetch all GitLab repositories from your GitLab group
    clone       Clone missing repositories
    update      Update all existing repositories
    branches    Switch all repos to most active branches
    verify      Verify repository structure
    sync        Run full sync (fetch + clone + update + branches + verify)
    status      Show current sync status
"""

import argparse
import configparser
import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Configuration file paths
CONFIG_FILE = os.path.expanduser('~/.gitlab_sync.ini')
LOCAL_CONFIG_FILE = '.gitlab_sync.ini'

# Default Configuration
DEFAULT_CONFIG = {
    'work_dir': os.path.expanduser('~/Work'),
    'gitlab_group': 'your-gitlab-group',
    'cache_dir': '/tmp',
    'cache_file': 'gitlab_projects.txt',
    'cache_json': 'gitlab_projects.json',
    'clone_timeout': '300',
    'fetch_timeout': '60',
    'branch_timeout': '30',
    'pull_timeout': '60',
    'max_workers': '8',
    'clean_corrupted': 'true',
    'max_retries': '3',
    'backoff_initial': '1',
    'backoff_max': '30',
    'adaptive_workers': 'true',
    'min_workers': '2',
    'error_threshold': '0.5'
}


def load_config():
    """Load configuration from config files with precedence: local > global > defaults."""
    config = DEFAULT_CONFIG.copy()
    parser = configparser.ConfigParser()
    
    # Try local config first
    if os.path.exists(LOCAL_CONFIG_FILE):
        parser.read(LOCAL_CONFIG_FILE)
        if 'gitlab_sync' in parser:
            config.update(parser['gitlab_sync'])
    
    # Then try global config
    elif os.path.exists(CONFIG_FILE):
        parser.read(CONFIG_FILE)
        if 'gitlab_sync' in parser:
            config.update(parser['gitlab_sync'])
    
    # Expand tilde for work_dir
    if 'work_dir' in config:
        config['work_dir'] = os.path.expanduser(config['work_dir'])
    
    return config


def get_cache_paths(config):
    """Get cache file paths from config."""
    cache_dir = config.get('cache_dir', '/tmp')
    cache_file = config.get('cache_file', 'gitlab_projects.txt')
    cache_json = config.get('cache_json', 'gitlab_projects.json')
    return os.path.join(cache_dir, cache_file), os.path.join(cache_dir, cache_json)


def log(message):
    """Print message with timestamp."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")


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
    """Execute function with exponential backoff retry."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt == max_retries:
                break
            
            error_type = classify_error(str(e))
            backoff = min(backoff_initial * (2 ** attempt) + random.uniform(0, 1), backoff_max)
            log(f"Retry {attempt + 1}/{max_retries} after {backoff:.1f}s ({error_type} error: {str(e)[:100]})")
            time.sleep(backoff)
    
    raise last_error


def fetch_gitlab_projects(gitlab_group, config):
    """Fetch all GitLab projects from GitLab group using glab API with pagination and retry logic."""
    log(f"Fetching all GitLab repositories from {gitlab_group}...")
    
    cache_file, cache_json = get_cache_paths(config)
    max_retries = int(config.get('max_retries', '3'))
    backoff_initial = float(config.get('backoff_initial', '1'))
    backoff_max = float(config.get('backoff_max', '30'))
    
    all_projects = []
    page = 1
    
    def _fetch_page(page_num):
        result = subprocess.run(
            ['glab', 'api', 
             f'projects?membership=true&per_page=100&page={page_num}&archived=false'],
            capture_output=True, text=True
        )
        
        if result.returncode != 0:
            raise Exception(f"API error on page {page_num}: {result.stderr}")
        
        try:
            data = json.loads(result.stdout)
        except Exception as e:
            raise Exception(f"Parse error page {page_num}: {e}")
        
        if not data:
            return None
        
        # Filter projects that belong to the target group
        group_prefix = f"{gitlab_group}/"
        filtered_projects = [
            p for p in data 
            if p.get('path_with_namespace', '').startswith(group_prefix)
        ]
        return filtered_projects, len(data)
    
    # Use projects API with membership to get all accessible projects, then filter by group
    while True:
        try:
            result = retry_with_backoff(_fetch_page, page, 
                                       max_retries=max_retries, 
                                       backoff_initial=backoff_initial, 
                                       backoff_max=backoff_max)
            
            if result is None:
                break
            
            filtered_projects, total_count = result
            all_projects.extend(filtered_projects)
            log(f"Page {page}: {len(filtered_projects)} projects from {total_count} total")
            
            if total_count < 100:
                break
            
            page += 1
            
        except Exception as e:
            log(f"Failed to fetch page {page} after retries: {e}")
            break
    
    log(f"Total projects fetched: {len(all_projects)}")
    
    # Save full JSON
    with open(cache_json, 'w') as f:
        json.dump(all_projects, f, indent=2)
    
    # Save list format
    with open(cache_file, 'w') as f:
        for p in all_projects:
            path = p.get('path_with_namespace', '')
            ssh = p.get('ssh_url_to_repo', '')
            http = p.get('http_url_to_repo', '')
            default_branch = p.get('default_branch') or 'master'
            archived = p.get('archived', False)
            f.write(f"{path}|{ssh}|{http}|{default_branch}|{archived}\n")
    
    log(f"Saved {len(all_projects)} projects to {cache_file}")
    return all_projects


def load_gitlab_projects(config, gitlab_group):
    """Load GitLab projects from cache file."""
    cache_file, _ = get_cache_paths(config)
    
    if not os.path.exists(cache_file):
        log(f"Cache file not found: {cache_file}")
        log("Run 'fetch' command first to populate cache")
        return {}
    
    projects = {}
    with open(cache_file, 'r') as f:
        for line in f:
            parts = line.strip().split('|')
            if len(parts) >= 5:
                path = parts[0]
                local_path = path.replace(f'{gitlab_group}/', '')
                projects[local_path] = {
                    'path': path,
                    'ssh': parts[1],
                    'http': parts[2],
                    'default_branch': parts[3],
                    'archived': parts[4] == 'True'
                }
    
    return projects


def get_local_repos(work_dir):
    """Get all local repositories."""
    result = subprocess.run(['find', '.', '-name', '.git', '-type', 'd'],
                           capture_output=True, text=True, cwd=work_dir)
    local_repos = set()
    for line in result.stdout.strip().split('\n'):
        if line:
            local_repos.add(line.replace('/.git', '').replace('./', ''))
    return local_repos


def is_valid_git_repo(directory):
    """Check if a directory is a valid git repository."""
    git_dir = os.path.join(directory, '.git')
    return os.path.isdir(git_dir)


def clone_repository(project, work_dir, config):
    """Clone a single repository using glab CLI for authentication with retry logic."""
    local_path = project['local_path']
    full_local = os.path.join(work_dir, local_path)
    parent = os.path.dirname(full_local)
    clone_timeout = int(config.get('clone_timeout', '300'))
    clean_corrupted = config.get('clean_corrupted', 'true').lower() == 'true'
    max_retries = int(config.get('max_retries', '3'))
    backoff_initial = float(config.get('backoff_initial', '1'))
    backoff_max = float(config.get('backoff_max', '30'))
    
    def _do_clone():
        os.makedirs(parent, exist_ok=True)
        
        # Check if directory already exists
        if os.path.exists(full_local):
            if is_valid_git_repo(full_local):
                # Valid git repo, skip cloning
                return ('skip', local_path, 'Already cloned')
            elif clean_corrupted:
                # Corrupted/incomplete directory, remove it
                log(f"Removing corrupted directory: {local_path}")
                import shutil
                shutil.rmtree(full_local)
            else:
                # Directory exists but not a git repo and cleaning disabled
                return ('fail', local_path, 'Directory exists but is not a git repo (use --clean-corrupted to remove)')
        
        # Use glab CLI to clone with proper authentication
        res = subprocess.run(['glab', 'repo', 'clone', project['http'], full_local],
                            capture_output=True, text=True, timeout=clone_timeout)
        if res.returncode == 0:
            return ('ok', local_path, '')
        else:
            error_msg = res.stderr.strip()
            raise Exception(f"Clone failed: {error_msg}")
    
    try:
        return retry_with_backoff(_do_clone, max_retries=max_retries, 
                                  backoff_initial=backoff_initial, backoff_max=backoff_max)
    except subprocess.TimeoutExpired:
        return ('timeout', local_path, f'Clone timed out ({clone_timeout}s)')
    except Exception as e:
        return ('error', local_path, str(e)[:200])


class AdaptiveWorkerPool:
    """Adaptive worker pool that adjusts parallelism based on error rate."""
    
    def __init__(self, initial_workers, min_workers, max_workers, error_threshold=0.5):
        self.current_workers = initial_workers
        self.min_workers = min_workers
        self.max_workers = max_workers
        self.error_threshold = error_threshold
        self.recent_results = []
        self.window_size = 10
    
    def record_result(self, success):
        """Record a result and update worker count if needed."""
        self.recent_results.append(success)
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
        """Get current worker count."""
        return self.current_workers


def clone_missing_repos(work_dir, config, gitlab_group):
    """Clone missing repositories with adaptive parallelism."""
    log("Cloning missing repositories...")
    
    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        return
    
    local_repos = get_local_repos(work_dir)
    max_workers = int(config.get('max_workers', '8'))
    adaptive_workers = config.get('adaptive_workers', 'true').lower() == 'true'
    min_workers = int(config.get('min_workers', '2'))
    error_threshold = float(config.get('error_threshold', '0.5'))
    
    to_clone = [{'local_path': path, 'http': p['http'], 'ssh': p['ssh']} 
                 for path, p in projects.items() 
                 if not p['archived'] and path not in local_repos]
    
    active_count = len([p for p in projects.values() if not p['archived']])
    log(f"Active GitLab projects: {active_count}")
    log(f"Already cloned locally: {len(local_repos)}")
    log(f"To clone: {len(to_clone)}")
    
    if not to_clone:
        log("No missing repositories to clone")
        return
    
    successes = []
    failures = []
    skipped = []
    
    if adaptive_workers and len(to_clone) > 5:
        # Use adaptive worker pool for larger batches
        pool = AdaptiveWorkerPool(min(max_workers, len(to_clone)), min_workers, max_workers, error_threshold)
        log(f"Using adaptive worker pool (initial: {pool.get_worker_count()}, range: {min_workers}-{max_workers})")
        
        # Process in batches with adaptive workers
        idx = 0
        while idx < len(to_clone):
            current_workers = pool.get_worker_count()
            batch_size = min(current_workers, len(to_clone) - idx)
            batch = to_clone[idx:idx + batch_size]
            
            with ThreadPoolExecutor(max_workers=current_workers) as ex:
                futures = {ex.submit(clone_repository, p, work_dir, config): p for p in batch}
                for fut in as_completed(futures):
                    status, path, err = fut.result()
                    pool.record_result(status == 'ok' or status == 'skip')
                    
                    idx += 1
                    if status == 'ok':
                        successes.append(path)
                        log(f"[{idx}/{len(to_clone)}] ✓ {path}")
                    elif status == 'skip':
                        skipped.append(path)
                        log(f"[{idx}/{len(to_clone)}] ⊘ {path}: {err}")
                    else:
                        failures.append((path, err))
                        log(f"[{idx}/{len(to_clone)}] ✗ {path}: {err[:80]}")
    else:
        # Use static worker pool for small batches or when adaptive is disabled
        log(f"Using static worker pool ({max_workers} workers)")
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(clone_repository, p, work_dir, config): p for p in to_clone}
            for i, fut in enumerate(as_completed(futures), 1):
                status, path, err = fut.result()
                if status == 'ok':
                    successes.append(path)
                    log(f"[{i}/{len(to_clone)}] ✓ {path}")
                elif status == 'skip':
                    skipped.append(path)
                    log(f"[{i}/{len(to_clone)}] ⊘ {path}: {err}")
                else:
                    failures.append((path, err))
                    log(f"[{i}/{len(to_clone)}] ✗ {path}: {err[:80]}")
    
    log(f"Clone complete: {len(successes)} successful, {len(skipped)} skipped, {len(failures)} failed")


def update_repository(local_path, work_dir, config):
    """Update a single repository."""
    full_path = os.path.join(work_dir, local_path)
    fetch_timeout = int(config.get('fetch_timeout', '60'))
    pull_timeout = int(config.get('pull_timeout', '60'))
    
    try:
        # Fetch all branches
        subprocess.run(['git', 'fetch', '--all', '--quiet'],
                      capture_output=True, cwd=full_path, timeout=fetch_timeout)
        
        # Get current branch
        curr_res = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                                 capture_output=True, text=True, cwd=full_path)
        current = curr_res.stdout.strip()
        
        if current == 'HEAD':
            return ('skip', local_path, 'Detached HEAD')
        
        # Pull latest changes
        res = subprocess.run(['git', 'pull', '--quiet', 'origin', current],
                           capture_output=True, text=True, cwd=full_path, timeout=pull_timeout)
        
        if res.returncode == 0:
            return ('ok', local_path, f'Updated {current}')
        else:
            return ('nochange', local_path, f'Already up to date on {current}')
            
    except subprocess.TimeoutExpired:
        return ('error', local_path, 'Timeout')
    except Exception as e:
        return ('error', local_path, str(e)[:100])


def update_repositories(work_dir, config):
    """Update all local repositories."""
    log("Updating all repositories...")
    
    local_repos = get_local_repos(work_dir)
    max_workers = int(config.get('max_workers', '8'))
    log(f"Found {len(local_repos)} local repositories")
    
    updated = []
    no_change = []
    errors = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(update_repository, path, work_dir, config): path for path in local_repos}
        for i, fut in enumerate(as_completed(futures), 1):
            status, path, msg = fut.result()
            if status == 'ok':
                updated.append((path, msg))
                log(f"[{i}/{len(local_repos)}] ✓ {path}: {msg}")
            elif status == 'nochange':
                no_change.append(path)
            elif status == 'skip':
                errors.append((path, msg))
                log(f"[{i}/{len(local_repos)}] ⊘ {path}: {msg}")
            else:
                errors.append((path, msg))
                log(f"[{i}/{len(local_repos)}] ✗ {path}: {msg}")
    
    log(f"Update complete: {len(updated)} updated, {len(no_change)} up to date, {len(errors)} errors")


def switch_repository_branch(local_path, projects, work_dir, config):
    """Switch a single repository to its most active branch."""
    if local_path not in projects:
        return ('skip', local_path, 'Not in GitLab list')
    
    info = projects[local_path]
    if info['archived']:
        return ('skip', local_path, 'Archived')
    
    full_path = os.path.join(work_dir, local_path)
    fetch_timeout = int(config.get('fetch_timeout', '60'))
    branch_timeout = int(config.get('branch_timeout', '30'))
    pull_timeout = int(config.get('pull_timeout', '60'))
    
    try:
        # Fetch all branches
        subprocess.run(['git', 'fetch', '--all', '--quiet'],
                      capture_output=True, cwd=full_path, timeout=fetch_timeout)
        
        # Get branch info with commit counts
        result = subprocess.run(
            ['git', 'for-each-ref', '--sort=-committerdate',
             '--format=%(refname:short)|%(committerdate:iso8601)|%(objectname)',
             'refs/remotes/origin/'],
            capture_output=True, text=True, cwd=full_path, timeout=branch_timeout
        )
        
        branch_info = []
        for line in result.stdout.strip().split('\n'):
            if line and 'HEAD' not in line:
                parts = line.split('|')
                if len(parts) == 3:
                    branch = parts[0].replace('origin/', '')
                    count_res = subprocess.run(
                        ['git', 'rev-list', '--count', f'origin/{branch}'],
                        capture_output=True, text=True, cwd=full_path, timeout=branch_timeout
                    )
                    count = int(count_res.stdout.strip()) if count_res.stdout.strip().isdigit() else 0
                    branch_info.append({
                        'name': branch,
                        'count': count,
                        'date': parts[1]
                    })
        
        if not branch_info:
            return ('skip', local_path, 'No branches found')
        
        # Sort by commit count
        branch_info.sort(key=lambda x: x['count'], reverse=True)
        most_active = branch_info[0]['name']
        
        # Get current branch
        curr_res = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                                 capture_output=True, text=True, cwd=full_path)
        current = curr_res.stdout.strip()
        
        if current == most_active:
            return ('ok', local_path, f'Already on {most_active}')
        
        # Switch to most active branch
        subprocess.run(['git', 'checkout', '--quiet', most_active],
                      capture_output=True, cwd=full_path, timeout=branch_timeout)
        subprocess.run(['git', 'pull', '--quiet', 'origin', most_active],
                      capture_output=True, cwd=full_path, timeout=pull_timeout)
        
        return ('switched', local_path, f'{current} -> {most_active}')
        
    except subprocess.TimeoutExpired:
        return ('error', local_path, 'Timeout')
    except Exception as e:
        return ('error', local_path, str(e)[:100])


def switch_active_branches(work_dir, config, gitlab_group):
    """Switch all repositories to their most active branches."""
    log("Switching repositories to most active branches...")
    
    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        return
    
    local_repos = get_local_repos(work_dir)
    max_workers = int(config.get('max_workers', '8'))
    log(f"Processing {len(local_repos)} local repositories")
    
    switched = []
    already_ok = []
    errors = []
    skipped = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(switch_repository_branch, path, projects, work_dir, config): path for path in local_repos}
        for i, fut in enumerate(as_completed(futures), 1):
            status, path, msg = fut.result()
            if status == 'switched':
                switched.append((path, msg))
                log(f"[{i}/{len(local_repos)}] ✓ {path}: {msg}")
            elif status == 'ok':
                already_ok.append(path)
            elif status == 'skip':
                skipped.append((path, msg))
            else:
                errors.append((path, msg))
                log(f"[{i}/{len(local_repos)}] ✗ {path}: {msg}")
    
    log(f"Branch switching complete: {len(switched)} switched, {len(already_ok)} already correct, {len(skipped)} skipped, {len(errors)} errors")


def verify_structure(work_dir, config, gitlab_group):
    """Verify repository structure matches GitLab."""
    log("Verifying repository structure...")
    
    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        return
    
    local_repos = get_local_repos(work_dir)
    
    # Check for nested git directories
    result = subprocess.run(['find', '.', '-name', '.git', '-type', 'd'],
                           capture_output=True, text=True, cwd=work_dir)
    all_git_dirs = []
    for line in result.stdout.strip().split('\n'):
        if line:
            all_git_dirs.append(line.replace('./', ''))
    
    nested_issues = []
    for git_dir in all_git_dirs:
        repo_path = git_dir.replace('/.git', '')
        parts = repo_path.split('/')
        for i in range(len(parts) - 1):
            parent_path = '/'.join(parts[:i+1])
            parent_git = parent_path + '/.git'
            if parent_git in all_git_dirs:
                nested_issues.append(repo_path)
                break
    
    # Compare with GitLab
    exact_matches = [r for r in projects if r in local_repos]
    extra_repos = local_repos - set(projects.keys())
    missing_repos = [r for r in projects if r not in local_repos]
    
    active_count = len([p for p in projects.values() if not p['archived']])
    
    log(f"GitLab projects: {len(projects)} (active: {active_count})")
    log(f"Local repositories: {len(local_repos)}")
    log(f"Exact matches: {len(exact_matches)}")
    log(f"Missing (non-archived): {len([r for r, p in projects.items() if r in missing_repos and not p['archived']])}")
    log(f"Extra local: {len(extra_repos)}")
    log(f"Nested structures: {len(nested_issues)}")
    
    if nested_issues:
        log(f"⚠ Nested structures found:")
        for issue in nested_issues:
            log(f"  {issue}")
    else:
        log("✓ No nested directory structures")
    
    if extra_repos:
        log(f"⚠ Extra local repos (not in GitLab): {len(extra_repos)}")
        for repo in sorted(extra_repos):
            log(f"  {repo}")
    
    missing_active = [r for r, p in projects.items() if r in missing_repos and not p['archived']]
    if missing_active:
        log(f"⚠ Missing non-archived repos: {len(missing_active)}")
        for repo in missing_active:
            log(f"  {repo}")
    
    if len(exact_matches) == active_count and not nested_issues:
        log("✓ Directory structure matches GitLab exactly for all accessible repositories")


def show_status(work_dir, config, gitlab_group):
    """Show current synchronization status."""
    log("Current synchronization status:")
    
    projects = load_gitlab_projects(config, gitlab_group)
    local_repos = get_local_repos(work_dir)
    
    if projects:
        active_count = len([p for p in projects.values() if not p['archived']])
        log(f"GitLab projects (cached): {len(projects)} (active: {active_count})")
    else:
        log("GitLab projects: Cache not found (run 'fetch' first)")
    
    log(f"Local repositories: {len(local_repos)}")
    
    if projects:
        exact_matches = [r for r in projects if r in local_repos]
        extra_repos = local_repos - set(projects.keys())
        missing_repos = [r for r in projects if r not in local_repos]
        missing_active = [r for r, p in projects.items() if r in missing_repos and not p['archived']]
        
        log(f"Synchronized: {len(exact_matches)}")
        log(f"Missing: {len(missing_active)}")
        log(f"Extra: {len(extra_repos)}")
        
        if missing_active:
            log(f"Missing repositories:")
            for repo in missing_active[:10]:
                log(f"  {repo}")
            if len(missing_active) > 10:
                log(f"  ... and {len(missing_active) - 10} more")


def main():
    parser = argparse.ArgumentParser(
        description='GitLab Workspace Synchronization CLI Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 gitlab_sync.py sync              # Run full synchronization
  python3 gitlab_sync.py fetch              # Fetch GitLab projects
  python3 gitlab_sync.py clone              # Clone missing repositories
  python3 gitlab_sync.py update             # Update existing repositories
  python3 gitlab_sync.py branches           # Switch to active branches
  python3 gitlab_sync.py verify             # Verify structure
  python3 gitlab_sync.py status             # Show status
        """
    )
    
    parser.add_argument('command', choices=['fetch', 'clone', 'update', 'branches', 'verify', 'sync', 'status'],
                       help='Command to execute')
    parser.add_argument('--work-dir', help='Working directory (overrides config file)')
    parser.add_argument('--group', help='GitLab group (overrides config file)')
    parser.add_argument('--config', help='Path to config file (overrides default search paths)')
    parser.add_argument('--clean-corrupted', action='store_true', dest='clean_corrupted',
                       help='Automatically remove corrupted/incomplete directories before cloning (default: true)')
    parser.add_argument('--no-clean-corrupted', action='store_false', dest='clean_corrupted',
                       help='Do not remove corrupted/incomplete directories (fail instead)')
    parser.add_argument('--max-retries', type=int, help='Maximum retry attempts for failed operations')
    parser.add_argument('--backoff-initial', type=float, help='Initial backoff time in seconds')
    parser.add_argument('--backoff-max', type=float, help='Maximum backoff time in seconds')
    parser.add_argument('--adaptive-workers', action='store_true', dest='adaptive_workers',
                       help='Enable adaptive worker pool (default: true)')
    parser.add_argument('--no-adaptive-workers', action='store_false', dest='adaptive_workers',
                       help='Disable adaptive worker pool (use static max_workers)')
    parser.add_argument('--min-workers', type=int, help='Minimum number of workers for adaptive pool')
    parser.add_argument('--error-threshold', type=float, help='Error rate threshold for adaptive workers (0.0-1.0)')
    
    args = parser.parse_args()
    
    # Load configuration
    config = load_config()
    
    # Override config with CLI arguments if provided
    work_dir = args.work_dir or config.get('work_dir', DEFAULT_CONFIG['work_dir'])
    gitlab_group = args.group or config.get('gitlab_group', DEFAULT_CONFIG['gitlab_group'])
    
    # Handle clean_corrupted flag (if not set on CLI, use config value)
    if hasattr(args, 'clean_corrupted') and args.clean_corrupted is not None:
        config['clean_corrupted'] = 'true' if args.clean_corrupted else 'false'
    
    # Handle retry options
    if args.max_retries is not None:
        config['max_retries'] = str(args.max_retries)
    if args.backoff_initial is not None:
        config['backoff_initial'] = str(args.backoff_initial)
    if args.backoff_max is not None:
        config['backoff_max'] = str(args.backoff_max)
    
    # Handle adaptive workers options
    if hasattr(args, 'adaptive_workers') and args.adaptive_workers is not None:
        config['adaptive_workers'] = 'true' if args.adaptive_workers else 'false'
    if args.min_workers is not None:
        config['min_workers'] = str(args.min_workers)
    if args.error_threshold is not None:
        config['error_threshold'] = str(args.error_threshold)
    
    log(f"Working directory: {work_dir}")
    log(f"GitLab group: {gitlab_group}")
    
    cache_file, cache_json = get_cache_paths(config)
    log(f"Cache file: {cache_file}")
    log('')
    
    try:
        if args.command == 'fetch':
            fetch_gitlab_projects(gitlab_group, config)
        elif args.command == 'clone':
            clone_missing_repos(work_dir, config, gitlab_group)
        elif args.command == 'update':
            update_repositories(work_dir, config)
        elif args.command == 'branches':
            switch_active_branches(work_dir, config, gitlab_group)
        elif args.command == 'verify':
            verify_structure(work_dir, config, gitlab_group)
        elif args.command == 'sync':
            log("Starting full synchronization...")
            fetch_gitlab_projects(gitlab_group, config)
            clone_missing_repos(work_dir, config, gitlab_group)
            update_repositories(work_dir, config)
            switch_active_branches(work_dir, config, gitlab_group)
            verify_structure(work_dir, config, gitlab_group)
            log("Full synchronization complete!")
        elif args.command == 'status':
            show_status(work_dir, config, gitlab_group)
    except KeyboardInterrupt:
        log("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        log(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
