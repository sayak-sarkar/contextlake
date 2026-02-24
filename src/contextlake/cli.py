#!/usr/bin/env python3
"""GitLab Workspace Synchronization CLI Tool.

Keeps a local workspace mirrored with the GitLab repositories you can access:
clones what is missing, updates existing clones, and moves each repo onto its
most active development branch -- while protecting any local working branches.

Entry points (all equivalent):
    contextlake <command>          # installed console script
    python -m contextlake <command>
    python3 contextlake.py <command>   # bare script, no install
"""

import argparse
import sys

from . import __version__
from .config import DEFAULT_CONFIG, expand_path, get_cache_paths, load_config
from .core import (
    clone_missing_repos,
    fetch_gitlab_projects,
    show_status,
    switch_repository_branches,
    update_repositories,
    verify_structure,
)
from .logging_setup import log, setup_logging

# Boolean flags backed by paired --x / --no-x switches. They must default to
# None so we can tell "user passed a flag" from "user said nothing" -- otherwise
# the store_true default (False) silently overrides the config file every run.
_TRISTATE_FLAGS = (
    "clean_corrupted",
    "adaptive_workers",
    "protect_working_branches",
    "require_clean_workspace",
    "auto_stash",
    "dry_run",
)

# Scalar CLI options that map 1:1 onto config keys.
_SCALAR_FLAGS = (
    "max_retries",
    "backoff_initial",
    "backoff_max",
    "min_workers",
    "error_threshold",
    "safe_branches",
)


def build_parser():
    """Build the argument parser. Kept separate from main() so it is testable."""
    parser = argparse.ArgumentParser(
        prog="contextlake",
        description="GitLab Workspace Synchronization CLI Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  contextlake sync              # Run full synchronization
  contextlake status            # Show status (read-only)
  contextlake --dry-run sync    # Show what sync would do, change nothing
        """,
    )

    parser.add_argument(
        "command",
        choices=[
            "fetch", "clone", "update", "branches", "verify", "sync", "status",
            # one-command turnkey setup (sync + knowledge layer + steering)
            "bootstrap",
            # knowledge layer (optional [kb] extra)
            "index", "connect", "embed", "lint", "wiki", "steer",
            "serve", "query", "doctor",
        ],
        help="Command to execute",
    )
    parser.add_argument("args", nargs="*", help="Positional arguments (e.g. query text)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--work-dir", help="Working directory (overrides config file)")
    parser.add_argument("--group", help="GitLab group (overrides config file)")
    parser.add_argument("--config", help="Path to config file (overrides default search paths)")

    # Knowledge-layer options (used by index/serve/query/doctor)
    kb = parser.add_argument_group("knowledge layer")
    kb.add_argument("--source", help="index: a repo directory or a graph shard JSON")
    kb.add_argument("--workspace", help="index: index every git repo under this directory")
    kb.add_argument("--force", action="store_true",
                    help="index: re-index every repo; steer: overwrite non-managed files")
    kb.add_argument("--out", help="steer: directory to write steering files into (default: cwd)")
    kb.add_argument("--kb-config", dest="kb_config",
                    help="bootstrap: knowledge-layer config (kb.toml), separate from the sync INI")
    kb.add_argument("--no-sync", dest="no_sync", action="store_true",
                    help="bootstrap: skip the GitLab mirror step (index the workspace as-is)")
    kb.add_argument("--no-connect", dest="no_connect", action="store_true",
                    help="bootstrap: skip the connectors step")
    kb.add_argument("--no-embed", dest="no_embed", action="store_true",
                    help="bootstrap: skip the embeddings step")
    kb.add_argument("--no-wiki", dest="no_wiki", action="store_true",
                    help="bootstrap: skip the wiki-generation step")
    kb.add_argument("--watch", action="store_true",
                    help="index --workspace: keep re-indexing on an interval (Ctrl-C to stop)")
    kb.add_argument("--interval", type=int,
                    help="index --watch: seconds between passes (default 60)")
    kb.add_argument("--transport", choices=["stdio", "http"], help="serve: MCP transport")
    kb.add_argument("--host", help="serve: bind host (http transport)")
    kb.add_argument("--port", type=int, help="serve: bind port (http transport)")
    kb.add_argument("--kind", help="query: filter by node kind")
    kb.add_argument("--repo", help="query: filter by repo")
    kb.add_argument("--limit", type=int, help="query: max results")
    kb.add_argument("--as-of", dest="as_of",
                    help="query: search a repo's snapshot at this indexed commit (needs --repo)")

    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Show what would happen without cloning, updating, or switching branches",
    )

    # Logging / verbosity
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose (debug) output")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only warnings and errors")
    parser.add_argument("--log-file", help="Append a full timestamped log to this file")

    # Clone / corruption handling
    parser.add_argument(
        "--clean-corrupted", action="store_true", dest="clean_corrupted",
        help="Remove corrupted/incomplete directories before cloning (default: true)",
    )
    parser.add_argument(
        "--no-clean-corrupted", action="store_false", dest="clean_corrupted",
        help="Do not remove corrupted/incomplete directories (fail instead)",
    )

    # Retry / backoff
    parser.add_argument("--max-retries", type=int, help="Max retry attempts for failed operations")
    parser.add_argument("--backoff-initial", type=float, help="Initial backoff time in seconds")
    parser.add_argument("--backoff-max", type=float, help="Maximum backoff time in seconds")

    # Adaptive parallelism
    parser.add_argument(
        "--adaptive-workers", action="store_true", dest="adaptive_workers",
        help="Enable adaptive worker pool (default: true)",
    )
    parser.add_argument(
        "--no-adaptive-workers", action="store_false", dest="adaptive_workers",
        help="Disable adaptive worker pool (use static max_workers)",
    )
    parser.add_argument("--min-workers", type=int, help="Minimum workers for the adaptive pool")
    parser.add_argument("--error-threshold", type=float, help="Error rate threshold (0.0-1.0)")

    # Branch safety
    parser.add_argument(
        "--protect-working-branches", action="store_true", dest="protect_working_branches",
        help="Enable branch protection (default: true)",
    )
    parser.add_argument(
        "--no-protect-working-branches", action="store_false", dest="protect_working_branches",
        help="Disable branch protection (allow operations on any branch)",
    )
    parser.add_argument(
        "--safe-branches",
        help="Comma-separated safe branches (default: main,master,develop,development)",
    )
    parser.add_argument(
        "--require-clean-workspace", action="store_true", dest="require_clean_workspace",
        help="Require clean workspace before operations (default: true)",
    )
    parser.add_argument(
        "--no-require-clean-workspace", action="store_false", dest="require_clean_workspace",
        help="Allow operations with uncommitted changes",
    )
    parser.add_argument(
        "--auto-stash", action="store_true", dest="auto_stash",
        help="Automatically stash changes before operations (default: false)",
    )
    parser.add_argument(
        "--no-auto-stash", action="store_false", dest="auto_stash",
        help="Disable automatic stashing",
    )

    # Tri-state booleans: unset on the command line -> None -> config wins.
    parser.set_defaults(**{name: None for name in _TRISTATE_FLAGS})
    return parser


def apply_cli_overrides(args, config):
    """Overlay CLI arguments onto a loaded config dict. Returns the same dict.

    Only values the user actually supplied override the config file; everything
    else is left untouched so config-file (and built-in default) values survive.
    """
    for name in _TRISTATE_FLAGS:
        value = getattr(args, name, None)
        if value is not None:
            config[name] = "true" if value else "false"

    for name in _SCALAR_FLAGS:
        value = getattr(args, name, None)
        if value is not None:
            config[name] = str(value)

    return config


def _bootstrap(args, config, work_dir, gitlab_group):
    """One-command turnkey setup: mirror repos, build the knowledge layer, and write
    editor steering. Optional/unconfigured stages are skipped; a failing stage warns
    but never aborts the rest."""
    import copy

    from . import style

    def _stage(title):
        log("")
        log(style.bold(style.cyan(f"▶ {title}")))

    if not getattr(args, "no_sync", False):
        _stage("Mirror repositories from GitLab")
        fetch_gitlab_projects(gitlab_group, config)
        clone_missing_repos(work_dir, config, gitlab_group)
        update_repositories(work_dir, config)
        switch_repository_branches(work_dir, config, gitlab_group)
        verify_structure(work_dir, config, gitlab_group)
    else:
        log("Skipping the GitLab mirror step (--no-sync)")

    try:
        from .kb import commands as kb
    except ImportError as e:
        log(f"Knowledge layer not installed — skipping index/connect/embed/wiki/steer. "
            f"Install it with: pip install 'contextlake[kb]'  ({e})")
        return

    # kb stages run against the workspace and the *kb* config (kb.toml), which is
    # distinct from the core sync INI passed as --config.
    kb_args = copy.copy(args)
    kb_args.config = getattr(args, "kb_config", None)
    kb_args.workspace = work_dir
    kb_args.source = None
    kb_args.out = work_dir

    stages = [("Index the code graph", kb.cmd_index)]
    if not getattr(args, "no_connect", False):
        stages.append(("Connect knowledge sources", kb.cmd_connect))
    if not getattr(args, "no_embed", False):
        stages.append(("Build semantic vectors", kb.cmd_embed))
    if not getattr(args, "no_wiki", False):
        stages.append(("Generate the curated wiki", kb.cmd_wiki))
    stages.append(("Write editor steering (.mcp.json, AGENTS.md, …)", kb.cmd_steer))

    for title, fn in stages:
        _stage(title)
        try:
            fn(kb_args)
        except Exception as e:  # noqa: BLE001 - one stage must not abort bootstrap
            log(f"  {style.warn(title + ' failed')} — {e}")

    log("")
    serve = "contextlake serve" + (f" --config {kb_args.config}" if kb_args.config else "")
    log(style.ok(f"Bootstrap complete — workspace ready at {work_dir}."))
    log(f"  Editors are wired (.mcp.json + steering). Start the knowledge server: {serve}")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose, quiet=args.quiet, log_file=args.log_file)

    # Knowledge-layer verbs are handled by the optional kb subsystem and don't
    # need the sync config/preamble. Imported lazily so the core tool runs
    # without the [kb] extra.
    if args.command in ("index", "connect", "embed", "lint", "wiki", "steer",
                        "serve", "query", "doctor"):
        try:
            from .kb import commands as kb_commands
        except ImportError as e:
            log(f"The '{args.command}' command needs the knowledge-layer extra: "
                f"pip install 'contextlake[kb]'  ({e})")
            sys.exit(1)
        sys.exit(kb_commands.dispatch(args.command, args))

    # Load configuration (honouring an explicit --config path if given), then
    # overlay any CLI overrides on top.
    config = load_config(args.config)
    config = apply_cli_overrides(args, config)

    work_dir = expand_path(args.work_dir) if args.work_dir else config.get(
        "work_dir", DEFAULT_CONFIG["work_dir"]
    )
    gitlab_group = args.group or config.get("gitlab_group", DEFAULT_CONFIG["gitlab_group"])

    log(f"Working directory: {work_dir}")
    log(f"GitLab group: {gitlab_group}")
    cache_file, _ = get_cache_paths(config)
    log(f"Cache file: {cache_file}")
    if config.get("dry_run", "false").lower() == "true":
        log("DRY RUN: no repositories will be cloned, updated, or switched")
    log("")

    try:
        if args.command == "fetch":
            fetch_gitlab_projects(gitlab_group, config)
        elif args.command == "clone":
            clone_missing_repos(work_dir, config, gitlab_group)
        elif args.command == "update":
            update_repositories(work_dir, config)
        elif args.command == "branches":
            switch_repository_branches(work_dir, config, gitlab_group)
        elif args.command == "verify":
            verify_structure(work_dir, config, gitlab_group)
        elif args.command == "sync":
            log("Starting full synchronization...")
            fetch_gitlab_projects(gitlab_group, config)
            clone_missing_repos(work_dir, config, gitlab_group)
            update_repositories(work_dir, config)
            switch_repository_branches(work_dir, config, gitlab_group)
            verify_structure(work_dir, config, gitlab_group)
            log("Full synchronization complete!")
        elif args.command == "status":
            show_status(work_dir, config, gitlab_group)
        elif args.command == "bootstrap":
            _bootstrap(args, config, work_dir, gitlab_group)
    except KeyboardInterrupt:
        log("Operation cancelled by user")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - top-level guard reports and exits
        log(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
