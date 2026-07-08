#!/usr/bin/env python3
"""contextlake — a local context layer for AI tools.

Mirrors the repositories you can access, indexes them into a local knowledge
graph, and serves it over MCP so agents answer from real source instead of
guessing. The mirror core keeps a workspace in sync with GitLab (clone what is
missing, update clones, follow each repo's most active branch) while protecting
local working branches; the optional [kb] extra adds the knowledge layer.

Entry points (all equivalent):
    contextlake <command>          # installed console script
    python -m contextlake <command>
    python3 contextlake.py <command>   # bare script, no install
"""

import argparse
import os
import sys

from . import __version__
from .config import DEFAULT_CONFIG, expand_path, get_cache_paths, load_config
from .core import (
    FetchError,
    clone_missing_repos,
    configure_network_resilience,
    fetch_gitlab_projects,
    show_status,
    switch_repository_branches,
    update_repositories,
    verify_structure,
)
from .logging_setup import log, setup_logging
from .metrics import run_audit

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


# CLI verb aliases: the MCP tools call these capabilities who_knows / blast_radius,
# so the CLI accepts the same vocabulary. Purely additive; the canonical verbs stay.
_ALIASES = {"who-knows": "owners", "blast-radius": "impact"}

# Verbs handled by the optional knowledge layer (the [kb] extra).
_KB_COMMANDS = frozenset({
    "index", "connect", "embed", "lint", "wiki", "steer", "serve", "query",
    "graph", "doctor", "eval", "owners", "impact", "ingest", "dashboard", "hook",
})

# Namespace defaults for every flag. Subparsers use SUPPRESS argument defaults so a
# flag given before the command survives the subparser pass; these seed the rest.
_DEFAULTS = {
    "command": None, "args": [],
    # global
    "verbose": False, "quiet": False, "log_file": None, "config": None,
    # mirror
    "work_dir": None, "group": None, "report": None, "no_audit": False,
    "max_retries": None, "backoff_initial": None, "backoff_max": None,
    "min_workers": None, "error_threshold": None, "safe_branches": None,
    # bootstrap
    "kb_config": None, "no_sync": False, "no_connect": False,
    "no_embed": False, "no_wiki": False,
    # knowledge layer
    "source": None, "workspace": None, "force": False, "out": None,
    "llm": None, "llm_model": None, "watch": False, "interval": None,
    "transport": None, "host": None, "port": None,
    "kind": None, "repo": None, "limit": None, "path": None, "source_type": None,
    "action": None,
    "golden": None, "retriever": None, "as_of": None,
    "node": None, "name": None, "search": None, "overview": False, "hops": None,
    "max_nodes": None, "max_fanout": None, "relation": None, "direction": None,
    "format": None, "layout": None, "output": None, "open": False, "cdn": False,
    "serve": False, "site": None, "repos": None, "group_depth": None,
    "anonymize": False, "sample": False,
    # tri-state booleans: unset on the command line -> None -> config wins
    **{name: None for name in _TRISTATE_FLAGS},
}

_S = argparse.SUPPRESS


def _add_global(p):
    g = p.add_argument_group("global options")
    g.add_argument("--config", default=_S,
                   help="config file (the sync INI for mirror commands, kb.toml for "
                        "knowledge commands)")
    g.add_argument("-v", "--verbose", action="store_true", default=_S,
                   help="verbose (debug) output")
    g.add_argument("-q", "--quiet", action="store_true", default=_S,
                   help="only warnings and errors")
    g.add_argument("--log-file", default=_S,
                   help="append a full timestamped log to this file")


def _add_mirror(p, hidden=False):
    def add(*names, **kw):
        if hidden:
            kw["help"] = _S
        kw.setdefault("default", _S)
        p.add_argument(*names, **kw)

    add("--work-dir", help="working directory (overrides config file)")
    add("--group", help="GitLab group (overrides config file)")
    add("--repos", metavar="PATTERN",
        help="mirror/index only repos matching this comma-separated glob/substring "
             "filter (e.g. 'team/api,billing,frontend/*') — great for a demo subset")
    add("--dry-run", action="store_true", dest="dry_run",
        help="show what would happen without cloning, updating, or switching branches")
    add("--clean-corrupted", action="store_true", dest="clean_corrupted",
        help="remove corrupted/incomplete directories before cloning (default: true)")
    add("--no-clean-corrupted", action="store_false", dest="clean_corrupted",
        help="do not remove corrupted/incomplete directories (fail instead)")
    add("--max-retries", type=int, help="max retry attempts for failed operations")
    add("--backoff-initial", type=float, help="initial backoff time in seconds")
    add("--backoff-max", type=float, help="maximum backoff time in seconds")
    add("--adaptive-workers", action="store_true", dest="adaptive_workers",
        help="enable adaptive worker pool (default: true)")
    add("--no-adaptive-workers", action="store_false", dest="adaptive_workers",
        help="disable adaptive worker pool (use static max_workers)")
    add("--min-workers", type=int, help="minimum workers for the adaptive pool")
    add("--error-threshold", type=float, help="error rate threshold (0.0-1.0)")
    add("--protect-working-branches", action="store_true", dest="protect_working_branches",
        help="enable branch protection (default: true)")
    add("--no-protect-working-branches", action="store_false", dest="protect_working_branches",
        help="disable branch protection (allow operations on any branch)")
    add("--safe-branches",
        help="comma-separated safe branches (default: main,master,develop,development)")
    add("--require-clean-workspace", action="store_true", dest="require_clean_workspace",
        help="require clean workspace before operations (default: true)")
    add("--no-require-clean-workspace", action="store_false", dest="require_clean_workspace",
        help="allow operations with uncommitted changes")
    add("--auto-stash", action="store_true", dest="auto_stash",
        help="automatically stash changes before operations (default: false)")
    add("--no-auto-stash", action="store_false", dest="auto_stash",
        help="disable automatic stashing")


def _add_report(p, *, no_audit=False):
    p.add_argument("--report", default=_S,
                   help="path for the per-repo audit report "
                        "(JSON + .csv; default <cache_dir>/repo_audit.json)")
    if no_audit:
        p.add_argument("--no-audit", dest="no_audit", action="store_true", default=_S,
                       help="skip the post-sync repo audit")


def _add_watch(p, what):
    p.add_argument("--watch", action="store_true", default=_S,
                   help=f"keep re-running {what} on an interval (Ctrl-C to stop)")
    p.add_argument("--interval", type=int, default=_S,
                   help="--watch: seconds between passes (default 60)")


def _add_net(p):
    p.add_argument("--host", default=_S, help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=_S, help="bind port")


def _root_hidden_flags(p):
    """Accept every per-command flag before the command too (the pre-subparser
    invocation style, e.g. `contextlake --workspace X index`) without cluttering
    the root help. Per-command help documents each flag where it belongs."""
    def add(*names, **kw):
        kw["help"] = _S
        kw.setdefault("default", _S)
        p.add_argument(*names, **kw)

    # NB: --repos is supplied on the root parser by _add_mirror(hidden=True), so it
    # must NOT be repeated here or argparse raises a conflicting-option error.
    for flag in ("--report", "--kb-config", "--source", "--workspace", "--out",
                 "--llm-model", "--host", "--kind", "--repo", "--path",
                 "--source-type", "--golden", "--as-of", "--node", "--name",
                 "--search", "--relation", "--output"):
        add(flag)
    for flag in ("--no-audit", "--no-sync", "--no-connect", "--no-embed", "--no-wiki",
                 "--force", "--watch", "--overview", "--open", "--cdn", "--serve",
                 "--anonymize", "--sample"):
        add(flag, action="store_true")
    for flag in ("--interval", "--port", "--limit", "--hops", "--max-nodes",
                 "--max-fanout", "--group-depth"):
        add(flag, type=int)
    add("--llm", choices=["auto", "ollama", "openai", "builtin", "anthropic", "cli"])
    add("--transport", choices=["stdio", "http"])
    add("--retriever", choices=("fts", "semantic", "hybrid"))
    add("--direction", choices=["in", "out", "both"])
    add("--format", choices=["html", "dot", "mermaid", "classdiagram", "json"])
    add("--layout", choices=["cose", "concentric", "breadthfirst", "circle", "grid"])
    add("--site", nargs="?", const="")


def build_parser():
    """Build the argument parser. Kept separate from main() so it is testable."""
    parser = argparse.ArgumentParser(
        prog="contextlake",
        description="A local context layer for AI tools: mirror your repositories, "
                    "index them into a knowledge graph, and serve it over MCP so agents "
                    "answer from real source instead of guessing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Get started:
  contextlake init                          guided setup: write your config (start here)
  contextlake bootstrap                     one command: mirror + index + connect + steer
  contextlake index .                       index the current repo into the local graph
  contextlake query "OrderService"          search the graph (cited file:line hits)
  contextlake serve                         expose the graph to your editor over MCP
  contextlake dashboard --serve --sample    explore a demo fleet, zero setup

Run 'contextlake <command> --help' for that command's flags and examples.
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_global(parser)
    _add_mirror(parser, hidden=True)
    _root_hidden_flags(parser)

    sub = parser.add_subparsers(dest="command", metavar="<command>",
                                title="commands", required=False)

    def command(name, help_, *, aliases=(), epilog=None):
        p = sub.add_parser(name, help=help_, description=help_, aliases=list(aliases),
                           epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
        _add_global(p)
        return p

    # ---- first run ---------------------------------------------------------
    p = command("init", "guided setup: write your mirror + knowledge-layer config",
                epilog="""
Examples:
  contextlake init                       interactive setup (prompts with defaults)
  contextlake init --yes                 non-interactive, all defaults
  contextlake init --platform github --group my-org --yes
                """)
    p.add_argument("--platform", default=_S,
                   help="gitlab (default) | github | bitbucket | gitea | codeberg | forgejo")
    p.add_argument("--group", default=_S, help="the group / org / workspace to mirror")
    p.add_argument("--work-dir", default=_S, help="local workspace directory")
    p.add_argument("--kb", dest="kb", action="store_true", default=_S,
                   help="set up the knowledge layer (default: yes)")
    p.add_argument("--no-kb", dest="kb", action="store_false", default=_S,
                   help="write only the mirror config")
    p.add_argument("--embeddings", action="store_true", default=_S,
                   help="enable semantic search in the generated kb config")
    p.add_argument("--yes", "-y", action="store_true", default=_S,
                   help="non-interactive: accept defaults / flags, no prompts")
    p.add_argument("--force", action="store_true", default=_S,
                   help="overwrite existing config files")

    # ---- mirror core -------------------------------------------------------
    for name, help_ in (
        ("fetch", "enumerate the GitLab projects you can access and cache the list"),
        ("clone", "clone repositories missing from the local workspace"),
        ("update", "fetch + fast-forward every existing clone"),
        ("branches", "switch each repo to its most active development branch"),
        ("verify", "compare the local workspace against GitLab (read-only)"),
        ("status", "show sync state without changing anything"),
    ):
        _add_mirror(command(name, help_))

    p = command("sync", "full mirror: fetch + clone + update + branches + verify",
                epilog="""
Examples:
  contextlake sync                    full synchronization
  contextlake sync --dry-run          show what would happen, change nothing
  contextlake sync --auto-stash       stash dirty trees before updating
                """)
    _add_mirror(p)
    _add_report(p, no_audit=True)

    p = command("audit", "per-repo health and age report (JSON + CSV)")
    _add_mirror(p)
    _add_report(p)

    p = command("bootstrap",
                "one command from nothing to a wired workspace: mirror, index, "
                "connect, embed, wiki, steering",
                epilog="""
Examples:
  contextlake bootstrap                          the full turnkey run
  contextlake bootstrap --no-sync                repos already cloned; skip the mirror
  contextlake bootstrap --no-embed --no-wiki     no model configured yet
  contextlake bootstrap --workspace ~/src        index this directory instead of work_dir
                """)
    _add_mirror(p)
    _add_report(p, no_audit=True)
    p.add_argument("--kb-config", dest="kb_config", default=_S,
                   help="knowledge-layer config (kb.toml), separate from the sync INI")
    p.add_argument("--workspace", default=_S,
                   help="index every git repo under this directory "
                        "(default: the mirror's work dir)")
    p.add_argument("--no-sync", dest="no_sync", action="store_true", default=_S,
                   help="skip the GitLab mirror step (index the workspace as-is)")
    p.add_argument("--no-connect", dest="no_connect", action="store_true", default=_S,
                   help="skip the connectors step")
    p.add_argument("--no-embed", dest="no_embed", action="store_true", default=_S,
                   help="skip the embeddings step")
    p.add_argument("--no-wiki", dest="no_wiki", action="store_true", default=_S,
                   help="skip the wiki-generation step")
    p.add_argument("--llm", default=_S, metavar="PROVIDER",
                   choices=["auto", "ollama", "openai", "builtin", "anthropic", "cli"],
                   help="power the wiki stage with this LLM provider; without it (and "
                        "without [llm] enabled in kb.toml) the wiki stage no-ops. "
                        "builtin = zero-setup CPU model, ollama | openai | auto")
    p.add_argument("--llm-model", dest="llm_model", default=_S, metavar="MODEL",
                   help="model name for --llm (e.g. llama3.1, gpt-4o-mini)")

    # ---- knowledge layer ---------------------------------------------------
    p = command("index", "parse repositories into the local knowledge graph",
                epilog="""
Examples:
  contextlake index                   index the current directory
  contextlake index path/to/repo      index one repo (same as --source)
  contextlake index --workspace ~/w   index every git repo under a folder
  contextlake index --force           full re-index (default is incremental)
                """)
    p.add_argument("path", nargs="?", default=_S,
                   help="a repo directory or graph-shard JSON to index (default: cwd)")
    p.add_argument("--source", default=_S, help="a repo directory or a graph shard JSON")
    p.add_argument("--workspace", default=_S,
                   help="index every git repo under this directory")
    p.add_argument("--repos", default=_S, metavar="PATTERN",
                   help="--workspace: index only repos matching this comma-separated "
                        "glob/substring filter (e.g. 'team/api,billing,frontend/*')")
    p.add_argument("--repo", default=_S,
                   help="repo id to index --source under (default: the directory name)")
    p.add_argument("--force", action="store_true", default=_S,
                   help="re-index every repo (default: only repos whose HEAD moved)")
    _add_watch(p, "the index")

    p = command("connect", "enrich the graph from configured sources "
                           "(GitLab MRs/issues, Atlassian, Figma)")
    p.add_argument("args", nargs="*", metavar="source",
                   help="only run these named sources (default: all configured)")
    _add_watch(p, "the connectors")

    p = command("embed", "build semantic vectors for the graph (needs [embeddings] config)")
    p.add_argument("--force", action="store_true", default=_S,
                   help="re-embed every repo (default: only changed repos)")
    p.add_argument("--limit", type=int, default=_S, help="max nodes to embed per repo")
    _add_watch(p, "the embedder")

    command("lint", "graph-health checks: stale repos and dangling edges")

    p = command("wiki", "generate provenance-stamped wiki pages, gated by a review council")
    p.add_argument("args", nargs="*", metavar="repo",
                   help="only these repo ids (default: all indexed)")
    p.add_argument("--llm", default=_S, metavar="PROVIDER",
                   choices=["auto", "ollama", "openai", "builtin", "anthropic", "cli"],
                   help="enable the LLM tier with this provider, overriding kb.toml "
                        "([llm] enabled+provider). builtin = CPU, no setup (needs the "
                        "llm-local extra); ollama | openai | auto")
    p.add_argument("--llm-model", dest="llm_model", default=_S, metavar="MODEL",
                   help="model name for --llm (e.g. llama3.1, gpt-4o-mini)")
    p.add_argument("--force", action="store_true", default=_S,
                   help="regenerate pages even when the graph is unchanged")

    p = command("steer", "write editor steering files (.mcp.json, AGENTS.md, skills, …)")
    p.add_argument("--out", default=_S,
                   help="directory to write steering files into (default: cwd)")
    p.add_argument("--force", action="store_true", default=_S,
                   help="overwrite non-managed files")

    p = command("hook", "install a git post-commit hook that re-indexes a repo on commit",
                epilog="""
Examples:
  contextlake hook install                    wire the repo in the current directory
  contextlake hook install --workspace ~/src  wire every git repo under a mirror
  contextlake hook status --workspace ~/src   show which repos are wired
  contextlake hook uninstall                  remove the hook (restores any prior one)

The hook runs `contextlake index <repo>` detached after each commit, so the graph
stays current without a manual re-index. It re-uses the repo's stored id (or the
directory name) so it updates the same node, never a duplicate.
                """)
    p.add_argument("action", nargs="?", default=_S,
                   choices=["install", "uninstall", "status"],
                   help="install (default) | uninstall | status")
    p.add_argument("path", nargs="?", default=_S,
                   help="repo directory to wire (default: the current directory)")
    p.add_argument("--workspace", default=_S,
                   help="wire every git repo under this directory (a whole mirror)")
    p.add_argument("--repo", default=_S,
                   help="repo id the hook re-indexes under (default: stored id, else dir name)")

    p = command("serve", "serve the knowledge graph to AI tools over MCP",
                epilog="""
Examples:
  contextlake serve                        stdio transport (editor-managed)
  contextlake serve --transport http       HTTP transport on --host/--port
                """)
    p.add_argument("--transport", choices=["stdio", "http"], default=_S,
                   help="MCP transport (default stdio)")
    _add_net(p)

    p = command("query", "search the graph from the terminal (cited file:line hits)",
                epilog="""
Examples:
  contextlake query "OrderService"
  contextlake query charge --kind function --repo billing-service
  contextlake query charge --repo billing-service --as-of a1b2c3
                """)
    p.add_argument("args", nargs="*", metavar="text", help="the search text")
    p.add_argument("--kind", default=_S, help="filter by node kind")
    p.add_argument("--repo", default=_S, help="filter by repo")
    p.add_argument("--limit", type=int, default=_S, help="max results (default 20)")
    p.add_argument("--as-of", dest="as_of", default=_S,
                   help="search a repo's snapshot at this indexed commit (needs --repo)")

    p = command("graph", "visualize a bounded subgraph (HTML/dot/mermaid/JSON)",
                epilog="""
Examples:
  contextlake graph --overview                        repos-as-nodes fleet view
  contextlake graph --name OrderService --hops 2      neighbourhood of a symbol
  contextlake graph --repo acme/app --format classdiagram   UML class diagram (Mermaid)
  contextlake graph --serve                           live click-to-expand UI
  contextlake graph --site                            offline cross-linked site
                """)
    p.add_argument("args", nargs="*", metavar="query",
                   help="full-text seed (same as --search)")
    p.add_argument("--node", default=_S, help="seed from this exact node id")
    p.add_argument("--name", default=_S, help="seed from nodes with this exact name (+ --kind)")
    p.add_argument("--search", default=_S, help="seed from a full-text search (+ --kind/--repo)")
    p.add_argument("--overview", action="store_true", default=_S,
                   help="repos-as-nodes with aggregated cross-repo edges")
    p.add_argument("--kind", default=_S, help="filter seeds by node kind")
    p.add_argument("--repo", default=_S, help="filter seeds by repo")
    p.add_argument("--limit", type=int, default=_S, help="max seed nodes")
    p.add_argument("--hops", type=int, default=_S, help="expansion radius (default 2)")
    p.add_argument("--max-nodes", dest="max_nodes", type=int, default=_S,
                   help="cap on rendered nodes (default 500)")
    p.add_argument("--max-fanout", dest="max_fanout", type=int, default=_S,
                   help="per-node neighbour cap, anti-hub (default 50)")
    p.add_argument("--relation", default=_S, help="only follow edges of this relation")
    p.add_argument("--direction", choices=["in", "out", "both"], default=_S,
                   help="edge direction to follow (default both)")
    p.add_argument("--format", default=_S,
                   choices=["html", "dot", "mermaid", "classdiagram", "json"],
                   help="output format (default html; classdiagram = UML Mermaid)")
    p.add_argument("--layout", default=_S,
                   choices=["cose", "concentric", "breadthfirst", "circle", "grid"],
                   help="html: initial layout (default cose; switchable in the page)")
    p.add_argument("--output", default=_S,
                   help="write to this path (default <store>/graphs/graph.html; "
                        "else stdout for non-html)")
    p.add_argument("--open", action="store_true", default=_S,
                   help="open the written HTML in a browser")
    p.add_argument("--cdn", action="store_true", default=_S,
                   help="load cytoscape.js from a CDN (smaller file, needs network)")
    p.add_argument("--serve", action="store_true", default=_S,
                   help="serve a live click-to-expand UI (uses --host/--port)")
    p.add_argument("--site", nargs="?", const="", default=_S, metavar="DIR",
                   help="build a cross-linked offline site (overview + per-repo pages "
                        "+ index) into DIR (default <store>/graphs/site)")
    p.add_argument("--repos", default=_S, metavar="PATTERN",
                   help="--site: only build repo pages whose id matches a pattern "
                        "(comma-separated glob/substring)")
    _add_net(p)

    command("doctor", "check the knowledge-layer install and configuration (✓/✗)")

    p = command("eval", "score a golden-query set against the index "
                        "(precision@k / recall@k / MRR)")
    p.add_argument("--golden", default=_S,
                   help="a golden-query JSON file "
                        "({queries:[{query, expected, kind?, repo?, match?}]})")
    p.add_argument("--retriever", choices=("fts", "semantic", "hybrid"), default=_S,
                   help="which retriever to score (default: fts; semantic/hybrid "
                        "need embeddings)")
    p.add_argument("--limit", type=int, default=_S, help="k for precision@k (default 10)")

    p = command("owners", "likely owners / SMEs for a repo or path, from git history",
                aliases=("who-knows",))
    p.add_argument("args", nargs="*", metavar="repo-or-path", help="a repo id or a path")
    p.add_argument("--path", default=_S, help="restrict to a sub-path")
    p.add_argument("--limit", type=int, default=_S, help="max owners listed (default 10)")

    p = command("impact", "reverse blast radius: what could break if a node changes",
                aliases=("blast-radius",))
    p.add_argument("args", nargs="*", metavar="node-or-symbol",
                   help="a node id or symbol name")
    p.add_argument("--repo", default=_S, help="disambiguate the symbol by repo")
    p.add_argument("--hops", type=int, default=_S, help="reverse depth (default 3)")
    p.add_argument("--limit", type=int, default=_S, help="max nodes listed (default 100)")

    p = command("ingest", "aggregate external documents (files/web/api/mcp sources) "
                          "into the graph")
    p.add_argument("--path", default=_S, help="the path (or URL/endpoint) to ingest")
    p.add_argument("--source-type", dest="source_type", default=_S,
                   help="source type for --path (default 'files')")

    p = command("dashboard", "the knowledge-system dashboard: fleet / repo / "
                             "relationships / impact / health / search",
                epilog="""
Examples:
  contextlake dashboard --serve --sample    explore a demo fleet, zero setup
  contextlake dashboard --serve             the live dashboard over your store
  contextlake dashboard --site out/         static offline export (see --anonymize)
                """)
    p.add_argument("--serve", action="store_true", default=_S,
                   help="serve the live dashboard (default; uses --host/--port)")
    p.add_argument("--open", action="store_true", default=_S,
                   help="open the dashboard in a browser")
    p.add_argument("--site", nargs="?", const="", default=_S, metavar="DIR",
                   help="build a static offline export into DIR")
    p.add_argument("--repos", default=_S, metavar="PATTERN",
                   help="--site: only include repos matching a pattern")
    p.add_argument("--group-depth", dest="group_depth", type=int, default=_S,
                   help="domain-grouping depth from repo-id path prefixes (default 1)")
    p.add_argument("--anonymize", action="store_true", default=_S,
                   help="--site: hash git-author identities + strip external link "
                        "URLs for a shareable export")
    p.add_argument("--sample", action="store_true", default=_S,
                   help="use the bundled demo fleet instead of your local store "
                        "(fictional data; works with --serve and --site)")
    _add_net(p)

    parser.set_defaults(**_DEFAULTS)
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


def _audit_report_path(args, config):
    """Where the per-repo audit report is written (CLI --report, else cache_dir)."""
    if getattr(args, "report", None):
        return expand_path(args.report)
    cache_file, _ = get_cache_paths(config)
    return os.path.join(os.path.dirname(cache_file) or ".", "repo_audit.json")


def _audit_workers(config):
    try:
        return int(config.get("max_workers", 8))
    except (TypeError, ValueError):
        return 8


def _bootstrap(args, config, work_dir, gitlab_group):
    """One-command turnkey setup: mirror repos, build the knowledge layer, and write
    editor steering. Optional/unconfigured stages are skipped; a failing stage warns
    but never aborts the rest."""
    import copy

    from . import style

    def _stage(title):
        log("")
        log(style.bold(style.cyan(f"▶ {title}")))

    failures = []
    if not getattr(args, "no_sync", False):
        _stage("Mirror repositories from GitLab")
        try:
            fetch_gitlab_projects(gitlab_group, config)
            clone_missing_repos(work_dir, config, gitlab_group)
            update_repositories(work_dir, config)
            switch_repository_branches(work_dir, config, gitlab_group)
            verify_structure(work_dir, config, gitlab_group)
        except FetchError as e:
            # Enumeration failed (often a VPN/proxy drop) after its own retries.
            # Existing clones are untouched, so the knowledge stages still run against
            # them — this is a *resumable* state, not a corrupt one. Make the fix +
            # resume unmistakable, since this notice is what the user returns to.
            resume = "contextlake bootstrap" + (
                f" --llm {args.llm}" if getattr(args, "llm", None) else "")
            log("")
            log(style.warn("⚠ Could not reach GitLab to mirror — likely a VPN/network drop."))
            log(f"    {e}")
            log("    → Continuing: building the knowledge layer from the repositories "
                "already on disk.")
            log(style.bold("    → When your connection is back, re-run to finish the mirror:"))
            log(style.bold(f"        {resume}"))
            log("      It is incremental and idempotent — it fetches/clones only what is "
                "still missing and re-indexes only what changed.")
            failures.append("Mirror repositories from GitLab (network)")
    else:
        log("Skipping the GitLab mirror step (--no-sync)")

    if not getattr(args, "no_audit", False):
        _stage("Audit repositories (health & age)")
        run_audit(work_dir, config, gitlab_group,
                  report_path=_audit_report_path(args, config),
                  max_workers=_audit_workers(config))

    try:
        from .kb import commands as kb
    except ImportError as e:
        # The knowledge layer's deps (mcp/pydantic/tree-sitter) are missing *for the
        # interpreter running this command*. The usual cause: bootstrap was launched
        # via the bare ./contextlake.py (which uses the system Python) while the
        # `[kb]` extra was installed into a virtualenv. Point at the exact interpreter
        # so the fix is unambiguous.
        log(style.warn("Knowledge layer not installed — skipping index/connect/embed/wiki/steer."))
        log(f"  Running under: {sys.executable}  (missing: {e})")
        log(f"  Fix (this interpreter): {sys.executable} -m pip install 'contextlake[kb]'")
        log("  Or, if you installed contextlake[kb] in a virtualenv, run bootstrap via that venv's "
            "executable (e.g. .venv/bin/contextlake bootstrap) instead of ./contextlake.py, which "
            "uses the system Python.")
        return

    # kb stages run against the workspace and the *kb* config (kb.toml), which is
    # distinct from the core sync INI passed as --config. An explicit --workspace
    # wins over the mirror's work_dir (it also receives the steering files).
    workspace = expand_path(args.workspace) if getattr(args, "workspace", None) else work_dir
    kb_args = copy.copy(args)
    kb_args.config = getattr(args, "kb_config", None)
    kb_args.workspace = workspace
    kb_args.source = None
    kb_args.out = workspace

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
            rc = fn(kb_args)
        except Exception as e:  # noqa: BLE001 - one stage must not abort bootstrap
            rc = 1
            log(f"  {style.warn(title + ' failed')} — {e}")
        if rc:
            failures.append(title)
            # The code graph is foundational — connect/embed/wiki/steer all read it.
            # If indexing failed there is nothing downstream to build on, so stop
            # honestly with a non-zero exit instead of reporting a hollow success.
            if fn is kb.cmd_index:
                log(style.warn("Bootstrap aborted — the code graph could not be built; "
                               "nothing downstream can run."))
                log(f"  Indexed workspace: {kb_args.workspace}. If that is not where "
                    "your repos live, pass --workspace DIR (or set work_dir in the config).")
                sys.exit(1)

    log("")
    serve = "contextlake serve" + (f" --config {kb_args.config}" if kb_args.config else "")
    if failures:
        log(style.warn(f"Bootstrap finished with {len(failures)} failed stage(s): "
                       f"{', '.join(failures)}."))
        log(f"  Workspace is at {work_dir}; re-run after fixing the above. "
            f"Start the server only once healthy: {serve}")
        sys.exit(1)
    log(style.ok(f"Bootstrap complete — workspace ready at {work_dir}."))
    log(f"  Editors are wired (.mcp.json + steering). Start the knowledge server: {serve}")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.command = _ALIASES.get(args.command, args.command)

    # Bare `contextlake` is a first keystroke, not an error: show the front door
    # (description, command list, getting-started examples) and exit clean.
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    setup_logging(verbose=args.verbose, quiet=args.quiet, log_file=args.log_file)

    # First-run setup writes the config the rest of the tool reads, so it must run
    # before load_config's "no config found" preamble. No [kb] extra needed.
    if args.command == "init":
        from .init_cmd import cmd_init
        sys.exit(cmd_init(args))

    # Knowledge-layer verbs are handled by the optional kb subsystem and don't
    # need the sync config/preamble. Imported lazily so the core tool runs
    # without the [kb] extra.
    if args.command in _KB_COMMANDS:
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
    # --repos scopes the whole mirror pipeline to a subset (fetch narrows the cache;
    # clone/update/branches/verify/status key off it; bootstrap also filters indexing).
    if getattr(args, "repos", None):
        config["repo_filter"] = args.repos

    work_dir = expand_path(args.work_dir) if args.work_dir else config.get(
        "work_dir", DEFAULT_CONFIG["work_dir"]
    )
    gitlab_group = (args.group or config.get("group")
                    or config.get("gitlab_group", DEFAULT_CONFIG["gitlab_group"]))

    # Widen child git/glab DNS budget for slow corporate resolvers (no-op if the
    # user already set RES_OPTIONS); harmless for non-network commands.
    configure_network_resilience(config)

    log(f"Working directory: {work_dir}")
    try:
        from .core import platform_name
        log(f"{platform_name(config).capitalize()} group: {gitlab_group}")
    except Exception:  # noqa: BLE001 - an unknown platform is reported by fetch itself
        log(f"Group: {gitlab_group}")
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
            if not getattr(args, "no_audit", False):
                run_audit(work_dir, config, gitlab_group,
                          report_path=_audit_report_path(args, config),
                          max_workers=_audit_workers(config))
        elif args.command == "audit":
            run_audit(work_dir, config, gitlab_group,
                      report_path=_audit_report_path(args, config),
                      max_workers=_audit_workers(config))
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
