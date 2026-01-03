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
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Default Configuration
DEFAULT_WORK_DIR = '/home/user/Work'
DEFAULT_GITLAB_GROUP = 'your-gitlab-group'
CACHE_FILE = '/tmp/gitlab_projects.txt'
CACHE_JSON = '/tmp/gitlab_projects.json'

# Global config (set from command line args)
WORK_DIR = DEFAULT_WORK_DIR
GITLAB_GROUP = DEFAULT_GITLAB_GROUP


def log(message):
    """Print message with timestamp."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")


def fetch_gitlab_projects(gitlab_group=DEFAULT_GITLAB_GROUP):
    """Fetch all GitLab projects from your GitLab group using glab API with pagination."""
    log("Fetching all GitLab repositories from your GitLab group...")
    
    all_projects = []
    page = 1
    
    while True:
        result = subprocess.run(
            ['glab', 'api', 
             f'groups/{gitlab_group}/projects?include_subgroups=true&per_page=100&page={page}&archived=false'],
            capture_output=True, text=True
        )
        
        if result.returncode != 0:
            log(f"Error on page {page}: {result.stderr}")
            break
        
        try:
            data = json.loads(result.stdout)
        except Exception as e:
            log(f"Parse error page {page}: {e}")
            break
        
        if not data:
            break
        
        all_projects.extend(data)
        log(f"Page {page}: {len(data)} projects")
        
        if len(data) < 100:
            break
        
        page += 1
    
    log(f"Total projects fetched: {len(all_projects)}")
    
    # Save full JSON
    with open(CACHE_JSON, 'w') as f:
        json.dump(all_projects, f, indent=2)
    
    # Save list format
    with open(CACHE_FILE, 'w') as f:
        for p in all_projects:
            path = p.get('path_with_namespace', '')
            ssh = p.get('ssh_url_to_repo', '')
            http = p.get('http_url_to_repo', '')
            default_branch = p.get('default_branch') or 'master'
            archived = p.get('archived', False)
            f.write(f"{path}|{ssh}|{http}|{default_branch}|{archived}\n")
    
    log(f"Saved {len(all_projects)} projects to {CACHE_FILE}")
    return all_projects


def load_gitlab_projects():
    """Load GitLab projects from cache file."""
    if not os.path.exists(CACHE_FILE):
        log(f"Cache file not found: {CACHE_FILE}")
        log("Run 'fetch' command first to populate cache")
        return {}
    
    projects = {}
    with open(CACHE_FILE, 'r') as f:
        for line in f:
            parts = line.strip().split('|')
            if len(parts) >= 5:
                path = parts[0]
                local_path = path.replace(f'{GITLAB_GROUP}/', '')
                projects[local_path] = {
                    'path': path,
                    'ssh': parts[1],
                    'http': parts[2],
                    'default_branch': parts[3],
                    'archived': parts[4] == 'True'
                }
    
    return projects


def get_local_repos(work_dir=DEFAULT_WORK_DIR):
    """Get all local repositories."""
    result = subprocess.run(['find', '.', '-name', '.git', '-type', 'd'],
                           capture_output=True, text=True, cwd=work_dir)
    local_repos = set()
    for line in result.stdout.strip().split('\n'):
        if line:
            local_repos.add(line.replace('/.git', '').replace('./', ''))
    return local_repos


def clone_repository(project, work_dir=DEFAULT_WORK_DIR):
    """Clone a single repository."""
    local_path = project['local_path']
    full_local = os.path.join(work_dir, local_path)
    parent = os.path.dirname(full_local)
    
    try:
        os.makedirs(parent, exist_ok=True)
        res = subprocess.run(['git', 'clone', '--quiet', project['http'], full_local],
                            capture_output=True, text=True, timeout=300)
        if res.returncode == 0:
            return ('ok', local_path, '')
        else:
            return ('fail', local_path, res.stderr.strip()[:200])
    except subprocess.TimeoutExpired:
        return ('timeout', local_path, 'Clone timed out (300s)')
    except Exception as e:
        return ('error', local_path, str(e)[:200])


def clone_missing_repos(work_dir=DEFAULT_WORK_DIR):
    """Clone missing repositories."""
    log("Cloning missing repositories...")
    
    projects = load_gitlab_projects()
    if not projects:
        return
    
    local_repos = get_local_repos(work_dir)
    
    to_clone = [{'local_path': path, 'http': p['http']} 
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
    
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(clone_repository, p, work_dir): p for p in to_clone}
        for i, fut in enumerate(as_completed(futures), 1):
            status, path, err = fut.result()
            if status == 'ok':
                successes.append(path)
                log(f"[{i}/{len(to_clone)}] ✓ {path}")
            else:
                failures.append((path, err))
                log(f"[{i}/{len(to_clone)}] ✗ {path}: {err[:80]}")
    
    log(f"Clone complete: {len(successes)} successful, {len(failures)} failed")


def update_repository(local_path, work_dir=DEFAULT_WORK_DIR):
    """Update a single repository."""
    full_path = os.path.join(work_dir, local_path)
    
    try:
        # Fetch all branches
        subprocess.run(['git', 'fetch', '--all', '--quiet'],
                      capture_output=True, cwd=full_path, timeout=60)
        
        # Get current branch
        curr_res = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                                 capture_output=True, text=True, cwd=full_path)
        current = curr_res.stdout.strip()
        
        if current == 'HEAD':
            return ('skip', local_path, 'Detached HEAD')
        
        # Pull latest changes
        res = subprocess.run(['git', 'pull', '--quiet', 'origin', current],
                           capture_output=True, text=True, cwd=full_path, timeout=60)
        
        if res.returncode == 0:
            return ('ok', local_path, f'Updated {current}')
        else:
            return ('nochange', local_path, f'Already up to date on {current}')
            
    except subprocess.TimeoutExpired:
        return ('error', local_path, 'Timeout')
    except Exception as e:
        return ('error', local_path, str(e)[:100])


def update_repositories(work_dir=DEFAULT_WORK_DIR):
    """Update all local repositories."""
    log("Updating all repositories...")
    
    local_repos = get_local_repos(work_dir)
    log(f"Found {len(local_repos)} local repositories")
    
    updated = []
    no_change = []
    errors = []
    
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(update_repository, path, work_dir): path for path in local_repos}
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


def switch_repository_branch(local_path, projects, work_dir=DEFAULT_WORK_DIR):
    """Switch a single repository to its most active branch."""
    if local_path not in projects:
        return ('skip', local_path, 'Not in GitLab list')
    
    info = projects[local_path]
    if info['archived']:
        return ('skip', local_path, 'Archived')
    
    full_path = os.path.join(work_dir, local_path)
    
    try:
        # Fetch all branches
        subprocess.run(['git', 'fetch', '--all', '--quiet'],
                      capture_output=True, cwd=full_path, timeout=60)
        
        # Get branch info with commit counts
        result = subprocess.run(
            ['git', 'for-each-ref', '--sort=-committerdate',
             '--format=%(refname:short)|%(committerdate:iso8601)|%(objectname)',
             'refs/remotes/origin/'],
            capture_output=True, text=True, cwd=full_path, timeout=30
        )
        
        branch_info = []
        for line in result.stdout.strip().split('\n'):
            if line and 'HEAD' not in line:
                parts = line.split('|')
                if len(parts) == 3:
                    branch = parts[0].replace('origin/', '')
                    count_res = subprocess.run(
                        ['git', 'rev-list', '--count', f'origin/{branch}'],
                        capture_output=True, text=True, cwd=full_path, timeout=30
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
                      capture_output=True, cwd=full_path, timeout=30)
        subprocess.run(['git', 'pull', '--quiet', 'origin', most_active],
                      capture_output=True, cwd=full_path, timeout=60)
        
        return ('switched', local_path, f'{current} -> {most_active}')
        
    except subprocess.TimeoutExpired:
        return ('error', local_path, 'Timeout')
    except Exception as e:
        return ('error', local_path, str(e)[:100])


def switch_active_branches(work_dir=DEFAULT_WORK_DIR):
    """Switch all repositories to their most active branches."""
    log("Switching repositories to most active branches...")
    
    projects = load_gitlab_projects()
    if not projects:
        return
    
    local_repos = get_local_repos(work_dir)
    log(f"Processing {len(local_repos)} local repositories")
    
    switched = []
    already_ok = []
    errors = []
    skipped = []
    
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(switch_repository_branch, path, projects, work_dir): path for path in local_repos}
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


def verify_structure(work_dir=DEFAULT_WORK_DIR):
    """Verify repository structure matches GitLab."""
    log("Verifying repository structure...")
    
    projects = load_gitlab_projects()
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


def show_status(work_dir=DEFAULT_WORK_DIR):
    """Show current synchronization status."""
    log("Current synchronization status:")
    
    projects = load_gitlab_projects()
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
    parser.add_argument('--work-dir', default=DEFAULT_WORK_DIR, help=f'Working directory (default: {DEFAULT_WORK_DIR})')
    parser.add_argument('--group', default=DEFAULT_GITLAB_GROUP, help=f'GitLab group (default: {DEFAULT_GITLAB_GROUP})')
    
    args = parser.parse_args()
    
    work_dir = args.work_dir
    gitlab_group = args.group
    
    log(f"Working directory: {work_dir}")
    log(f"GitLab group: {gitlab_group}")
    log('')
    
    try:
        if args.command == 'fetch':
            fetch_gitlab_projects(gitlab_group)
        elif args.command == 'clone':
            clone_missing_repos(work_dir)
        elif args.command == 'update':
            update_repositories(work_dir)
        elif args.command == 'branches':
            switch_active_branches(work_dir)
        elif args.command == 'verify':
            verify_structure(work_dir)
        elif args.command == 'sync':
            log("Starting full synchronization...")
            fetch_gitlab_projects(gitlab_group)
            clone_missing_repos(work_dir)
            update_repositories(work_dir)
            switch_active_branches(work_dir)
            verify_structure(work_dir)
            log("Full synchronization complete!")
        elif args.command == 'status':
            show_status(work_dir)
    except KeyboardInterrupt:
        log("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        log(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
