"""
Core git operations for gitlab_sync
"""

import os
import subprocess
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from config import get_cache_paths, DEFAULT_CONFIG
from safety import check_repository_safety, stash_changes, is_safe_branch


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
    """Retry function with exponential backoff."""
    last_error = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            error_type = classify_error(str(e))
            
            # Don't retry on certain error types
            if error_type in ['dns', 'tls']:
                break
            
            if attempt < max_retries - 1:
                backoff = min(backoff_initial * (2 ** attempt), backoff_max)
                jitter = random.uniform(0.5, 1.5)
                sleep_time = backoff * jitter
                time.sleep(sleep_time)
    
    raise last_error


class AdaptiveWorkerPool:
    """Manages adaptive worker pool based on error rate."""
    
    def __init__(self, max_workers, min_workers, error_threshold):
        self.max_workers = max_workers
        self.min_workers = min_workers
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


def fetch_gitlab_projects(gitlab_group, config):
    """Fetch all GitLab projects using glab API with pagination."""
    cache_file, cache_json = get_cache_paths(config)
    
    log(f"Fetching GitLab projects for group: {gitlab_group}")
    
    # Try to load from cache first
    if os.path.exists(cache_json):
        try:
            with open(cache_json, 'r') as f:
                all_projects = json.load(f)
            log(f"Loaded {len(all_projects)} projects from cache")
            return all_projects
        except:
            pass
    
    # Fetch from GitLab
    all_projects = {}
    page = 1
    per_page = 100
    
    while True:
        try:
            cmd = ['glab', 'api', 'groups', gitlab_group, 'projects', 
                   '--paginate', str(per_page), '--page', str(page)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                log(f"Error fetching projects: {result.stderr}")
                break
            
            if not result.stdout.strip():
                break
            
            try:
                projects = json.loads(result.stdout)
            except:
                break
            
            if not projects:
                break
            
            for p in projects:
                if p.get('path_with_namespace'):
                    path = p['path_with_namespace']
                    all_projects[path] = {
                        'http': p.get('http_url_to_repo', ''),
                        'ssh': p.get('ssh_url_to_repo', ''),
                        'archived': p.get('archived', False),
                        'default_branch': p.get('default_branch', 'main')
                    }
            
            log(f"Fetched page {page}, total projects: {len(all_projects)}")
            page += 1
            
        except Exception as e:
            log(f"Error fetching page {page}: {e}")
            break
    
    # Save to cache
    with open(cache_json, 'w') as f:
        json.dump(all_projects, f, indent=2)
    
    with open(cache_file, 'w') as f:
        for p in all_projects:
            f.write(f"{p}\n")
    
    log(f"Fetched {len(all_projects)} total projects")
    return all_projects


def load_gitlab_projects(config, gitlab_group):
    """Load GitLab projects from cache."""
    cache_file, cache_json = get_cache_paths(config)
    
    if os.path.exists(cache_json):
        try:
            with open(cache_json, 'r') as f:
                data = json.load(f)
                # Handle both dict and list formats
                if isinstance(data, list):
                    # Convert list to dict if needed
                    return {}
                return data
        except:
            pass
    
    # If JSON cache doesn't exist or is invalid, try to regenerate
    log("Cache not found or invalid, fetching fresh data...")
    return fetch_gitlab_projects(gitlab_group, config)


def get_local_repos(work_dir):
    """Get list of local repositories."""
    local_repos = []
    for root, dirs, files in os.walk(work_dir):
        if '.git' in dirs:
            rel_path = os.path.relpath(root, work_dir)
            local_repos.append(rel_path)
    return local_repos


def clone_repository(local_path, http, ssh, work_dir, config):
    """Clone a single repository."""
    full_path = os.path.join(work_dir, local_path)
    clone_timeout = int(config.get('clone_timeout', '300'))
    
    try:
        if os.path.exists(full_path):
            return ('skip', local_path, 'Already exists')
        
        # Create directory structure
        os.makedirs(full_path, exist_ok=True)
        
        # Clone using HTTPS
        result = subprocess.run(
            ['git', 'clone', http, full_path],
            capture_output=True, text=True, timeout=clone_timeout
        )
        
        if result.returncode == 0:
            return ('ok', local_path, 'Cloned')
        else:
            # Remove failed directory
            import shutil
            shutil.rmtree(full_path, ignore_errors=True)
            return ('error', local_path, result.stderr.strip()[:100])
            
    except subprocess.TimeoutExpired:
        import shutil
        shutil.rmtree(full_path, ignore_errors=True)
        return ('error', local_path, 'Timeout')
    except Exception as e:
        import shutil
        shutil.rmtree(full_path, ignore_errors=True)
        return ('error', local_path, str(e)[:100])


def update_repository(local_path, work_dir, config):
    """Update a single repository."""
    full_path = os.path.join(work_dir, local_path)
    fetch_timeout = int(config.get('fetch_timeout', '60'))
    pull_timeout = int(config.get('pull_timeout', '60'))
    
    try:
        # Check repository safety before updating
        safe, warnings = check_repository_safety(local_path, work_dir, config)
        if not safe:
            # Try to stash if there are uncommitted changes
            has_changes = any('Uncommitted changes' in w for w in warnings)
            if has_changes:
                stash_success, stash_msg = stash_changes(full_path, config)
                if stash_success:
                    log(f"⚠ {local_path}: {stash_msg}")
                else:
                    return ('skip', local_path, f'Skipped (unsafe: {", ".join(warnings)})')
            else:
                return ('skip', local_path, f'Skipped (unsafe: {", ".join(warnings)})')
        
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
    protect_working_branches = config.get('protect_working_branches', 'true').lower() == 'true'
    
    try:
        # Check repository safety before switching
        safe, warnings = check_repository_safety(local_path, work_dir, config)
        if not safe:
            # For branch switching, always skip if unsafe
            return ('skip', local_path, f'Skipped (unsafe: {", ".join(warnings)})')
        
        # Fetch all branches
        subprocess.run(['git', 'fetch', '--all', '--quiet'],
                      capture_output=True, cwd=full_path, timeout=fetch_timeout)
        
        # Get current branch
        curr_res = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                                 capture_output=True, text=True, cwd=full_path)
        current = curr_res.stdout.strip()
        
        # Skip branch switching if on a working branch and protection is enabled
        if protect_working_branches and not is_safe_branch(current, config):
            return ('skip', local_path, f'Skipped branch switch (on working branch: {current})')
        
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


def verify_repository(local_path, projects, work_dir, config):
    """Verify a single repository structure."""
    if local_path not in projects:
        return ('extra', local_path, 'Extra local repo')
    
    full_path = os.path.join(work_dir, local_path)
    
    if not os.path.exists(full_path):
        return ('missing', local_path, 'Missing local repo')
    
    if not os.path.exists(os.path.join(full_path, '.git')):
        return ('invalid', local_path, 'Not a git repository')
    
    return ('ok', local_path, 'Valid')


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
    
    if adaptive_workers:
        pool = AdaptiveWorkerPool(max_workers, min_workers, error_threshold)
        pool.current_workers = max_workers
    else:
        pool = None
        current_workers = max_workers
    
    successes = []
    skipped = []
    failures = []
    
    with ThreadPoolExecutor(max_workers=max_workers if pool else max_workers) as ex:
        futures = {ex.submit(clone_repository, item['local_path'], item['http'], item['ssh'], work_dir, config): item for item in to_clone}
        
        for i, fut in enumerate(as_completed(futures), 1):
            result = fut.result()
            status, local_path, message = result
            
            if status == 'ok':
                successes.append(local_path)
                if pool:
                    pool.record_result(True)
            elif status == 'skip':
                skipped.append(local_path)
            else:
                failures.append(local_path)
                if pool:
                    pool.record_result(False)
            
            if pool:
                current_workers = pool.get_worker_count()
            
            log(f"[{i}/{len(to_clone)}] {local_path}: {message}")
    
    log(f"Clone complete: {len(successes)} successful, {len(skipped)} skipped, {len(failures)} failed")


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
            result = fut.result()
            status, local_path, message = result
            
            if status == 'ok':
                updated.append(local_path)
                log(f"[{i}/{len(local_repos)}] ✓ {local_path}: {message}")
            elif status == 'nochange':
                no_change.append(local_path)
                log(f"[{i}/{len(local_repos)}] = {local_path}: {message}")
            elif status == 'skip':
                log(f"[{i}/{len(local_repos)}] ⊘ {local_path}: {message}")
            else:
                errors.append(local_path)
                log(f"[{i}/{len(local_repos)}] ✗ {local_path}: {message}")
    
    log(f"Update complete: {len(updated)} updated, {len(no_change)} unchanged, {len(errors)} errors, {len(local_repos) - len(updated) - len(no_change) - len(errors)} skipped")


def switch_repository_branches(work_dir, config, gitlab_group):
    """Switch all repositories to most active branches."""
    log("Switching repositories to most active branches...")
    
    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        log("No projects loaded")
        return
    
    local_repos = get_local_repos(work_dir)
    max_workers = int(config.get('max_workers', '8'))
    
    switched = []
    already_ok = []
    skipped = []
    errors = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(switch_repository_branch, path, projects, work_dir, config): path for path in local_repos}
        for i, fut in enumerate(as_completed(futures), 1):
            result = fut.result()
            status, local_path, message = result
            
            if status == 'switched':
                switched.append(local_path)
                log(f"[{i}/{len(local_repos)}] ↝ {local_path}: {message}")
            elif status == 'ok':
                already_ok.append(local_path)
                log(f"[{i}/{len(local_repos)}] ✓ {local_path}: {message}")
            elif status == 'skip':
                skipped.append(local_path)
                log(f"[{i}/{len(local_repos)}] ⊘ {local_path}: {message}")
            else:
                errors.append(local_path)
                log(f"[{i}/{len(local_repos)}] ✗ {local_path}: {message}")
    
    log(f"Branch switch complete: {len(switched)} switched, {len(already_ok)} already on target, {len(skipped)} skipped, {len(errors)} errors")


def verify_structure(work_dir, config, gitlab_group):
    """Verify repository structure matches GitLab."""
    log("Verifying repository structure...")
    
    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        log("No projects loaded")
        return
    
    local_repos = get_local_repos(work_dir)
    
    valid = []
    missing = []
    extra = []
    invalid = []
    
    all_paths = set(local_repos) | set(projects.keys())
    
    for path in all_paths:
        result = verify_repository(path, projects, work_dir, config)
        status, local_path, message = result
        
        if status == 'ok':
            valid.append(local_path)
        elif status == 'missing':
            missing.append(local_path)
        elif status == 'extra':
            extra.append(local_path)
        elif status == 'invalid':
            invalid.append(local_path)
    
    log(f"Verification complete: {len(valid)} valid, {len(missing)} missing, {len(extra)} extra, {len(invalid)} invalid")
    
    if missing:
        log(f"Missing repositories:")
        for path in missing[:10]:
            log(f"  {path}")
        if len(missing) > 10:
            log(f"  ... and {len(missing) - 10} more")
    
    if extra:
        log(f"Extra repositories:")
        for path in extra[:10]:
            log(f"  {path}")
        if len(extra) > 10:
            log(f"  ... and {len(extra) - 10} more")


def show_status(work_dir, config, gitlab_group):
    """Show current synchronization status."""
    log("Current synchronization status:")
    
    projects = load_gitlab_projects(config, gitlab_group)
    if not projects:
        log("No projects loaded - run 'fetch' first")
        return
    
    local_repos = get_local_repos(work_dir)
    
    active_projects = {k: v for k, v in projects.items() if not v['archived']}
    
    synchronized = []
    missing = []
    extra = []
    
    for path in active_projects.keys():
        if path in local_repos:
            synchronized.append(path)
        else:
            missing.append(path)
    
    for path in local_repos:
        if path not in active_projects:
            extra.append(path)
    
    log(f"GitLab projects (cached): {len(active_projects)} (active: {len(active_projects)})")
    log(f"Local repositories: {len(local_repos)}")
    log(f"Synchronized: {len(synchronized)}")
    log(f"Missing: {len(missing)}")
    log(f"Extra: {len(extra)}")
    
    if missing:
        log(f"Missing repositories:")
        for path in missing[:10]:
            log(f"  {path}")
        if len(missing) > 10:
            log(f"  ... and {len(missing) - 10} more")
    
    if extra:
        log(f"Extra repositories:")
        for path in extra[:10]:
            log(f"  {path}")
        if len(extra) > 10:
            log(f"  ... and {len(extra) - 10} more")
