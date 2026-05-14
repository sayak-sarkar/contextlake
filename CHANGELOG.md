# Changelog

All notable changes to gitlab_sync will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Code modularization: Split monolithic gitlab_sync.py into separate modules
  - config.py: Configuration loading and management
  - safety.py: Branch safety and workspace protection functions
  - core.py: Core git operations (fetch, clone, update, branches, verify, status)
  - gitlab_sync.py: Main CLI entry point

## [1.2.0] - 2026-06-16

### Added
- Branch safety checks to protect working branches from sync conflicts
- Workspace protection requiring clean workspace before operations
- Automatic stashing support for uncommitted changes
- Configurable safe branches list
- CLI arguments for branch safety control:
  - --protect-working-branches / --no-protect-working-branches
  - --safe-branches
  - --require-clean-workspace / --no-require-clean-workspace
  - --auto-stash / --no-auto-stash
- Enhanced error classification for better retry strategies
- Adaptive worker pool for dynamic parallelism
- Comprehensive branch safety documentation in README

### Changed
- Updated README with branch safety section including scenarios and examples

## [1.1.0] - 2026-05-24

### Added
- INI-based configuration file support
- Local and global config file support
- CLI arguments now override config file settings
- Improved security with externalized configuration
- Tilde expansion for home directory paths
- Configurable timeouts and worker counts
- Exponential backoff retry mechanism
- Adaptive worker pool for dynamic parallelism
- Enhanced error classification for better retry strategies

### Changed
- Removed all hardcoded company/personal identifiers
- Configuration files can be excluded from version control

## [1.0.0] - 2026-05-10

### Added
- Full synchronization pipeline
- Branch management with automatic active branch detection
- Structure verification
- Concurrent processing with ThreadPoolExecutor
- Error handling and timeout management
- Timestamped logging
