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
import os
import sys

from .config import load_config, get_cache_paths, DEFAULT_CONFIG
from .core import (
    fetch_gitlab_projects, clone_missing_repos, update_repositories,
    switch_repository_branches, verify_structure, show_status
)
from .logging_setup import log, setup_logging


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
    
    # Branch safety arguments
    parser.add_argument('--protect-working-branches', action='store_true', dest='protect_working_branches',
                       help='Enable branch protection (default: true)')
    parser.add_argument('--no-protect-working-branches', action='store_false', dest='protect_working_branches',
                       help='Disable branch protection (allow operations on any branch)')
    parser.add_argument('--safe-branches', help='Comma-separated list of safe branches (default: main,master,develop,development)')
    parser.add_argument('--require-clean-workspace', action='store_true', dest='require_clean_workspace',
                       help='Require clean workspace before operations (default: true)')
    parser.add_argument('--no-require-clean-workspace', action='store_false', dest='require_clean_workspace',
                       help='Allow operations with uncommitted changes')
    parser.add_argument('--auto-stash', action='store_true', dest='auto_stash',
                       help='Automatically stash changes before operations (default: false)')
    parser.add_argument('--no-auto-stash', action='store_false', dest='auto_stash',
                       help='Disable automatic stashing')
    
    args = parser.parse_args()

    setup_logging()

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
    
    # Handle branch safety options
    if hasattr(args, 'protect_working_branches') and args.protect_working_branches is not None:
        config['protect_working_branches'] = 'true' if args.protect_working_branches else 'false'
    if args.safe_branches is not None:
        config['safe_branches'] = args.safe_branches
    if hasattr(args, 'require_clean_workspace') and args.require_clean_workspace is not None:
        config['require_clean_workspace'] = 'true' if args.require_clean_workspace else 'false'
    if hasattr(args, 'auto_stash') and args.auto_stash is not None:
        config['auto_stash'] = 'true' if args.auto_stash else 'false'
    
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
            switch_repository_branches(work_dir, config, gitlab_group)
        elif args.command == 'verify':
            verify_structure(work_dir, config, gitlab_group)
        elif args.command == 'sync':
            log("Starting full synchronization...")
            fetch_gitlab_projects(gitlab_group, config)
            clone_missing_repos(work_dir, config, gitlab_group)
            update_repositories(work_dir, config)
            switch_repository_branches(work_dir, config, gitlab_group)
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
