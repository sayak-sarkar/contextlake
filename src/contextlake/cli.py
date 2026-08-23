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
    python3 run-contextlake.py <command>   # bare script, no install
"""

import argparse
import difflib
import logging
import os
import re
import sys
import textwrap
import time

from . import __version__, netguard, observability
from .config import DEFAULT_CONFIG, ConfigError, expand_path, get_cache_paths, load_config
from .core import (
    FetchError,
    StageResult,
    clone_missing_repos,
    configure_network_resilience,
    fetch_gitlab_projects,
    fetch_result,
    show_status,
    switch_repository_branches,
    update_repositories,
    verify_structure,
)
from .logging_setup import LOG_FORMATS, TEXT, log, setup_logging
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
    "branch",
    "branch_strategy",
)

# The three selections `core.select_most_active_branch` implements. Listed here because the
# value is validated in `apply_cli_overrides` rather than by argparse -- see the comment
# there for why -- and a name that is not one of these silently ran the hybrid selection.
_BRANCH_STRATEGIES = frozenset({"commits", "recency", "hybrid"})


# CLI verb aliases: the MCP tools call these capabilities who_knows / blast_radius,
# so the CLI accepts the same vocabulary. Purely additive; the canonical verbs stay.
_ALIASES = {"who-knows": "owners", "blast-radius": "impact"}

# The two command namespaces. The CLI does two unrelated jobs -- mirroring git
# repositories and building/serving the knowledge layer over them -- so each verb
# now lives under the noun it belongs to (`contextlake mirror fetch`,
# `contextlake kb index`). `kb` is the same word already user-visible in kb.toml,
# the `contextlake[kb]` extra, and _KB_COMMANDS below.
_MIRROR_NS = "mirror"
_KB_NS = "kb"
_NAMESPACES = (_MIRROR_NS, _KB_NS)

# Groups the command list by task, for the root --help's categorized listing
# (replacing argparse's own alphabetical-ish flat dump). Each entry is
# (namespace-or-None, title, names); a namespace of None means the command stays
# top-level. Every command name must appear in exactly one group -- see
# test_every_registered_command_is_categorized_exactly_once. This stays five
# groups even though there are only two namespaces: the task grouping is what a
# reader navigates by, the namespace is only how it is typed.
_COMMAND_CATEGORIES = (
    (None, "Get started", ("version", "init", "completion", "bootstrap", "doctor")),
    (_MIRROR_NS, "Mirror a fleet", ("fetch", "clone", "update", "branches", "verify",
                                    "status", "sync", "audit")),
    (_KB_NS, "Build the knowledge graph", ("index", "source", "connect", "embed",
                                           "ingest", "enrich", "wiki", "docs", "lint",
                                           "forget", "eval")),
    (_KB_NS, "Explore & search", ("query", "graph", "owners", "impact", "dashboard")),
    (_KB_NS, "Serve to editors", ("serve", "steer", "hook", "refresh")),
)

# canonical command name -> the namespace it is typed under. Commands absent from
# this map (version, init, completion, bootstrap, doctor) are top-level: init and
# bootstrap span BOTH tiers, version/completion belong to neither, and doctor is
# the diagnostic you reach for when nothing else works -- none of them should sit
# behind a namespace.
_NAMESPACE_OF = {name: ns for ns, _, names in _COMMAND_CATEGORIES if ns for name in names}


def _qualified(name):
    """``fetch`` -> ``mirror fetch``: the only spelling that parses. Used wherever
    an error message names a command, so a user who types the old flat form is
    always shown where the verb actually lives now."""
    ns = _NAMESPACE_OF.get(_ALIASES.get(name, name))
    return f"{ns} {name}" if ns else name


def _categorized_commands_text(choices, *, namespace=None):
    """A grouped-by-task replacement for the subparsers action's own flat
    listing (suppressed by _RootHelpFormatter), built from the live subparser
    objects so each description always matches the command's own --help -- no
    separate copy to drift out of sync. Long descriptions (bootstrap,
    dashboard, ...) are wrapped and re-indented to the terminal width instead
    of hard-wrapping mid-word past column 80 -- RawDescriptionHelpFormatter
    never touches the epilog, so nothing else does this for us.

    ``namespace`` restricts the listing to one namespace's groups (for that
    namespace parser's own --help, where the prefix is already in the usage
    line); the root listing shows every group and spells the prefix out.
    """
    from . import style

    _REVERSE_ALIASES = {canon: alias for alias, canon in _ALIASES.items()}
    indent, label_col = "    ", 22
    wrap_width = max(40, style.terminal_width() - len(indent) - label_col - 1)
    cont_indent = " " * (len(indent) + label_col + 1)

    lines = ["Commands, by task:"]
    for ns, title, names in _COMMAND_CATEGORIES:
        if namespace is not None and ns != namespace:
            continue
        heading = title if (namespace is not None or ns is None) else \
            f"{title} (contextlake {ns} <command>)"
        lines.append(f"\n  {heading}:")
        for name in names:
            alias = _REVERSE_ALIASES.get(name)
            label = f"{name} ({alias})" if alias else name
            desc = (choices[name].description or "").strip()
            wrapped = textwrap.wrap(desc, wrap_width) or [""]
            lines.append(f"{indent}{label:<{label_col}} {wrapped[0]}")
            lines.extend(f"{cont_indent}{cont}" for cont in wrapped[1:])
    return "\n".join(lines)


def _best_command_match(bad, choices):
    """The command name nearest to the mistyped ``bad``, or None if nothing is
    close enough. Same 0.5 similarity floor difflib.get_close_matches used, so
    this never suggests anything that was not already suggestible.

    What changes is the *tie-break*. get_close_matches ranks with
    ``heapq.nlargest`` over ``(ratio, name)`` pairs, so an exact score tie is
    settled by comparing the candidate strings -- arbitrary, and wrong here:
    ``inti`` scores 0.75 against both ``init`` and ``lint``, ``'lint' > 'init'``,
    and a one-transposition typo of a top-level command was answered with
    "Did you mean: kb lint?". Break ties toward a top-level command instead:
    those are reached by typing the bare verb, so a near-exact match on one is
    much the likelier intent. The two namespace tokens (``mirror``/``kb``) are
    deliberately NOT counted as top-level -- answering a mistyped verb with a
    namespace is not an answer.
    """
    ranked = []
    for name in choices:
        ratio = difflib.SequenceMatcher(a=bad, b=name).ratio()
        if ratio < 0.5:
            continue
        canonical = _ALIASES.get(name, name)
        top_level = canonical not in _NAMESPACE_OF and canonical not in _NAMESPACES
        ranked.append((-ratio, 0 if top_level else 1, name))
    return min(ranked)[2] if ranked else None


class _RootArgumentParser(argparse.ArgumentParser):
    """Replaces argparse's raw N-choice dump for a mistyped subcommand with a
    concise 'did you mean' suggestion -- the same difflib pattern the knowledge
    layer already uses for unknown repo ids (see _repo_id_suggestions).
    """

    # This is the only interception point argparse offers here: ArgumentError
    # is always stringified before reaching error() (see
    # ArgumentParser.parse_known_args's `except ArgumentError as err:
    # self.error(str(err))`), so there is no structured object to match on
    # instead. The message text itself is stable across supported Python
    # versions (3.9-3.14: only 3.14 added an opt-in suggest_on_error variant
    # of this same string, which we don't enable). If a translated locale
    # ever changes the wording, this degrades gracefully to the fallback
    # super().error() dump below -- never a crash, just the old UX.
    _BAD_COMMAND_RE = re.compile(r"^argument <command>: invalid choice: '([^']+)'")
    # Set on each namespace parser (`mirror` / `kb`) so its own error text can
    # tell "typo inside this namespace" from "that command lives at the top
    # level". None on the root and on every leaf.
    _namespace_name = None
    # parse_args() (called once, on the root parser -- see main()) is where argparse
    # reports leftover tokens no subparser consumed; a flag that exists on a DIFFERENT
    # subcommand than the one invoked (e.g. `bootstrap --local`, --local is init/source
    # add only) ends up here, indistinguishable at this point from a genuine typo.
    _BAD_FLAGS_RE = re.compile(r"^unrecognized arguments: (.+)$")
    # Raised by the SUBPARSER that owns the flag (unlike the two patterns above,
    # both root-only) when a value-taking flag is immediately followed by another
    # recognized flag instead of a value -- e.g. `dashboard --workspace --open`:
    # argparse never learns --workspace's value was meant to come first, and
    # reports the flag as simply missing its argument, which reads as "what
    # value?" rather than the real issue, flag ordering.
    _MISSING_VALUE_RE = re.compile(r"^argument (-[^:]+): expected one argument$")

    def parse_known_args(self, args=None, namespace=None):
        # Stashed so error() can tell "genuinely mistyped command" apart from
        # "an unrecognized flag ate the token before the command" (see
        # _preceding_unrecognized_flag) -- error() only gets the final message
        # string, never the original argv, so this is the only way to recover it.
        self._last_argv = list(sys.argv[1:] if args is None else args)
        return super().parse_known_args(args, namespace)

    def error(self, message):
        m = self._BAD_COMMAND_RE.match(message)
        if m:
            bad = m.group(1)
            flag = self._preceding_unrecognized_flag(bad)
            if flag:
                # `--work-d /tmp doctor`: --work-d isn't a real root flag, so
                # argparse never learns it takes a value -- /tmp falls into the
                # <command> positional slot instead and fails as an "invalid
                # choice" before argparse ever reports the actual problem. The
                # real issue is the unrecognized flag, not a mistyped command.
                super().error(f"unrecognized arguments: {flag}")
            self.exit(2, self._unknown_command_text(bad))
        m = self._BAD_FLAGS_RE.match(message)
        if m:
            text = self._unrecognized_flag_text(m.group(1))
            if text is not None:
                self.exit(2, text)
        m = self._MISSING_VALUE_RE.match(message)
        if m:
            text = self._missing_value_text(m.group(1))
            if text is not None:
                self.exit(2, text)
        super().error(message)

    def _preceding_unrecognized_flag(self, bad):
        """If ``bad`` failed as the ``<command>`` choice only because an
        unrecognized flag immediately before it in argv consumed argparse's
        attention, return that flag; else None."""
        argv = getattr(self, "_last_argv", None) or []
        try:
            idx = argv.index(bad)
        except ValueError:
            return None
        if idx == 0:
            return None
        prev = argv[idx - 1]
        if prev.startswith("-") and prev not in self._option_string_actions:
            return prev
        return None

    def _unknown_command_text(self, bad):
        from . import style

        choices = getattr(self, "_command_choices", {})
        # Match against every name INCLUDING aliases -- a typo of "blast-radius"
        # is lexically close to that alias, not to "impact" -- then translate
        # the winner to the canonical verb for display, matching what --help
        # teaches (a mistyped "blast-radiu" should suggest "impact").
        match = _best_command_match(bad, choices)
        suggestion = _ALIASES.get(match, match) if match else None

        lines = [style.fail(f"Unknown command: {bad!r}")]
        root_choices = getattr(self, "_root_choices", {})
        if self._namespace_name and bad in root_choices and bad not in _NAMESPACE_OF:
            # `contextlake kb doctor`: not a typo at all -- doctor/init/bootstrap/
            # version/completion deliberately stay top-level, and reaching for one
            # a level too deep is the single most predictable namespace mistake.
            lines.append("")
            lines.append(f"{bad!r} is a top-level command: run 'contextlake {bad}'.")
        elif suggestion:
            # At the root, teach the namespaced spelling. This is also what
            # answers a bare `contextlake fetch` after the namespacing cutover:
            # the old flat name is still in the suggestion pool (it names a real
            # command, just one level down), so an exact match on it resolves to
            # "Did you mean: mirror fetch?" through the ordinary unknown-command
            # path -- no compatibility shim involved.
            shown = suggestion if self._namespace_name else _qualified(suggestion)
            lines.append("")
            lines.append(f"Did you mean: {shown}?")
        lines.append("")
        lines.append(f"Run '{self.prog} --help' to see all commands.")
        return "\n".join(lines) + "\n"

    # --help-advanced only exists on the 8 mirror commands by design (the other
    # 21 have no hidden tier to reveal) -- excluded from the cross-command
    # "used by" registry below so running it on the wrong command falls
    # through to argparse's own plain "unrecognized arguments" message instead
    # of a noisy 9-command "used by" list. -h/--help are NOT included here:
    # they exist on every subparser already, so `cmd != canonical` can never
    # fire for them, and stripping them would break the same-command fuzzy-typo
    # suggestion for a transposition like `--hepl` (very much wanted).
    _META_FLAGS = frozenset({"--help-advanced"})

    def _flags_by_command(self):
        """``{command_name: {every registered flag string}}``, canonical names
        only (aliases like 'who-knows'/'blast-radius' point at the same
        subparser object as their canonical verb, so they'd otherwise show up
        as a spurious extra "used by" entry for the same flags). Built once
        and cached at the class level -- every subcommand's flags are fixed
        for the lifetime of the process, and this is reachable from a
        subparser's own error() too (not just the root's), since argparse
        defaults every add_parser() to this same class (see build_parser()).

        Always built from `_all_parsers` (every leaf across both tiers, set
        identically on the root and on each namespace parser), never from a
        parser's own `_command_choices` subset -- otherwise whichever parser
        happened to error first would poison the class-level cache with a
        partial registry. The two namespace parsers themselves are skipped:
        they re-declare the whole pre-verb flag surface, so leaving them in
        would report e.g. --local as "used by: init, kb, mirror, source".
        """
        cached = getattr(_RootArgumentParser, "_flags_by_command_cache", None)
        if cached is not None:
            return cached
        reg = {}
        for name, subparser in getattr(self, "_all_parsers", {}).items():
            if name in _ALIASES or name in _NAMESPACES:
                continue
            reg[name] = set(subparser._option_string_actions.keys()) - self._META_FLAGS
        _RootArgumentParser._flags_by_command_cache = reg
        return reg

    def _unrecognized_flag_text(self, raw):
        """``raw`` is argparse's own leftover-token dump for "unrecognized
        arguments" -- e.g. ``bootstrap --local`` leaves ``--local`` unconsumed
        because that flag exists on init/source add, not bootstrap. Returns a
        message naming which command(s) the flag DOES belong to (or, for a
        genuine typo, the nearby valid flag on the command actually invoked);
        None if there's nothing better to say than argparse's own message.
        """
        from . import style

        bad = next((t for t in raw.split() if t.startswith("-")), None)
        if bad is None:
            return None
        argv = getattr(self, "_last_argv", None) or []
        choices = getattr(self, "_all_parsers", {})
        # Skip the namespace token: in `contextlake mirror fetch --local` the flag
        # belongs to (or doesn't belong to) `fetch`, not to `mirror`.
        command = next((t for t in argv if t in choices and t not in _NAMESPACES), None)
        canonical = _ALIASES.get(command, command)
        flags_by_command = self._flags_by_command()

        # An EXACT match on some other command is a certain, unambiguous signal --
        # checked first so it can't be shadowed by a same-command fuzzy guess (e.g.
        # `bootstrap --local` fuzzy-matching bootstrap's own --llm is a much worse
        # answer than the true one: --local is real, just on init/source instead).
        used_by = sorted(cmd for cmd, flags in flags_by_command.items()
                         if bad in flags and cmd != canonical)
        if used_by and canonical:
            lines = [style.fail(f"{bad!r} isn't a flag on {canonical!r}"), "",
                     f"It's used by: {', '.join(_qualified(c) for c in used_by)}.", "",
                     f"Run 'contextlake {_qualified(canonical)} --help' to see "
                     f"{canonical}'s own flags."]
            return "\n".join(lines) + "\n"

        this_commands_flags = flags_by_command.get(canonical, set())
        typo = difflib.get_close_matches(bad, this_commands_flags, n=1, cutoff=0.75)
        if typo:
            lines = [style.fail(f"Unknown flag: {bad!r}"), "",
                     f"Did you mean: {typo[0]}?"]
            return "\n".join(lines) + "\n"

        # No other command owns the flag and nothing on this one is close enough
        # to guess. Falling through to argparse here dumped the ROOT parser's
        # usage line: leftover tokens are always reported by the single
        # parse_args() call on the root, whichever subcommand actually ran, so
        # `contextlake kb index --nosuchflag` answered with `usage: contextlake
        # [-h] [--version] ...` -- a command the user never typed. Say which
        # command rejected the flag, and show that command's usage.
        leaf = choices.get(canonical)
        if leaf is not None:
            lines = [style.fail(f"Unknown flag: {bad!r} (on "
                                f"'{_qualified(canonical)}')"), "",
                     leaf.format_usage().rstrip(), "",
                     f"Run 'contextlake {_qualified(canonical)} --help' to see "
                     f"{canonical}'s own flags."]
            return "\n".join(lines) + "\n"
        return None  # no command identified -- argparse's own root message stands

    def _missing_value_text(self, flag):
        """``flag`` (e.g. ``--workspace``) reported "expected one argument" --
        if the very next token in argv is itself a recognized flag (not a
        value the user forgot to quote or that happens to start with '-'),
        say so plainly instead of leaving the reader to guess why a value they
        DID seem to provide "wasn't" one. None otherwise (a truly missing
        value, or one that just happens to start with '-', gets no better
        explanation than argparse's own).
        """
        from . import style

        argv = getattr(self, "_last_argv", None) or []
        try:
            idx = argv.index(flag)
        except ValueError:
            return None
        nxt = argv[idx + 1] if idx + 1 < len(argv) else None
        if nxt is None or not nxt.startswith("-") or nxt not in self._option_string_actions:
            return None
        lines = [style.fail(f"{flag!r} needs a value, but the next token ({nxt!r}) is "
                            "itself a recognized flag"), "",
                 f"Put the value right after {flag}, e.g. '{flag} <value> {nxt}'."]
        return "\n".join(lines) + "\n"

# Verbs handled by the optional knowledge layer (the [kb] extra).
_KB_COMMANDS = frozenset({
    "index", "connect", "embed", "lint", "forget", "wiki", "docs", "steer", "serve",
    "query",
    "graph", "doctor", "eval", "owners", "impact", "ingest", "enrich", "dashboard", "hook",
    "source", "refresh",
})

# Namespace defaults for every flag. Subparsers use SUPPRESS argument defaults so a
# flag given before the command survives the subparser pass; these seed the rest.
_DEFAULTS = {
    "command": None, "subcommand": None, "args": [],
    # global
    "verbose": False, "quiet": False, "log_file": None, "config": None,
    "offline": False,
    # observability. redact is tri-state (None = the per-handler default:
    # scrub the log file, leave the console alone) -- deliberately NOT in
    # _TRISTATE_FLAGS, which is about mirror-config keys, not log routing.
    "log_format": TEXT, "metrics_file": None, "redact": None, "access_log": False,
    # completion
    "shell": None,
    # mirror
    "work_dir": None, "group": None, "report": None, "no_audit": False,
    "exit_zero_on_partial": False,
    "max_retries": None, "backoff_initial": None, "backoff_max": None,
    "min_workers": None, "error_threshold": None, "safe_branches": None,
    "branch": None, "branch_strategy": None,
    # bootstrap
    "kb_config": None, "no_sync": False, "no_connect": False,
    "no_embed": False, "no_enrich": False, "no_wiki": False,
    "no_diagrams": False,
    "no_docs": False,
    # knowledge layer
    "source": None, "workspace": None, "force": False, "out": None,
    "llm": None, "llm_model": None, "watch": False, "interval": None,
    "transport": None, "host": None, "port": None,
    "kind": None, "repo": None, "limit": None, "path": None, "source_type": None,
    "action": None,
    "golden": None, "retriever": None, "as_of": None,
    "node": None, "name": None, "search": None, "overview": False, "hops": None,
    "max_nodes": None, "max_edges": None, "max_fanout": None, "relation": None, "direction": None,
    "format": None, "layout": None, "output": None, "open": False, "cdn": False,
    "serve": False, "site": None, "repos": None, "group_depth": None,
    "max_symbols": None,
    "anonymize": False, "sample": False, "c4": False,
    # tri-state booleans: unset on the command line -> None -> config wins
    **{name: None for name in _TRISTATE_FLAGS},
}

_S = argparse.SUPPRESS


def _bounded_int(minimum: int, maximum: int):
    """An argparse ``type`` for a numeric bound, checked where the user typed it.

    Every one of these flags used to be a plain ``int`` applied as ``value or
    default``, which made three separate lies possible: ``--limit 0`` meant "the
    default" rather than none, ``--limit -1`` reached SQLite as ``LIMIT -1`` and
    returned *every* row (the one input a user expects to be refused disabled the
    safety rail), and ``--max-nodes -1`` wrote an empty graph and reported
    success. Bounds that can only produce a nonsense result are refused up front
    with the range in the message, instead of being reinterpreted downstream.
    """
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid int value: {raw!r}") from None
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be between {minimum} and {maximum} (got {value})")
        return value
    # argparse prints this in "invalid <name> value" errors, so name it usefully.
    parse.__name__ = f"int[{minimum}..{maximum}]"
    return parse


# A count of things to return or render. Zero is refused: it reads as "none",
# which no caller wants, and it used to silently mean "the default" instead.
_COUNT = _bounded_int(1, 1_000_000)
# Traversal depth: at least one hop, or there is nothing to report.
_HOPS = _bounded_int(1, 1_000)
# The one bound where zero is a real request: a nodes-only view with no edges
# drawn. Negatives stay refused.
_MAX_EDGES = _bounded_int(0, 1_000_000)
# Total attempts, not retries-after-the-first, despite the flag's name: the retry loop
# is `for attempt in range(max_retries)`, so 3 means three tries. Zero was refused after
# a reviewer traced it: zero iterations leaves `last_error` as None and the loop ends at
# `raise last_error`, which fails with "exceptions must derive from BaseException" and
# never attempts the operation at all. "Try once, do not retry" is spelled 1.
_RETRIES = _bounded_int(1, 1_000)
# Path-prefix grouping depth (repo ids are paths, not arbitrarily deep).
_DEPTH = _bounded_int(1, 64)
# A polling interval in whole seconds, up to a day.
_SECONDS = _bounded_int(1, 86_400)
# A TCP port. Zero is kept legal because it is a real request -- bind an ephemeral
# port -- but anything outside the range only ever reached the socket layer, which
# raised OverflowError from inside asyncio/socketserver AFTER the server had done
# its startup work and printed the bearer token. A successful-looking banner
# followed by a stack trace was the one numeric flag this validation missed.
_PORT = _bounded_int(0, 65_535)
# Tool worker slots. resolve_tool_concurrency() deliberately ignores a nonsense
# ENV value and serves at the default, arguing that refusing to start over a typo
# in a shell profile is worse -- which is right for an env var an editor inherits,
# and wrong for a flag someone typed just now. Typed explicitly, 0 / -1 / 10**9
# silently became 2 with nothing printed. The env var keeps its lenient path; the
# flag gets the same treatment as every other numeric option.
_CONCURRENCY = _bounded_int(1, 1_024)


class _RootHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """The root parser's own flat, 29-entry "commands:" listing (one line per
    subparser, alphabetical-ish registration order) is replaced by the epilog's
    hand-built "Commands, by task:" section (see _categorized_commands_text) --
    dropping the subparsers action here means an empty "commands:" group, which
    argparse's own HelpFormatter already omits header-and-all when empty."""

    def add_argument(self, action):
        if isinstance(action, argparse._SubParsersAction):
            return
        super().add_argument(action)


class _RevealAdvancedHelp(argparse.Action):
    """``--help-advanced``: the same help as ``-h``, plus the resilience/tuning
    flags _add_mirror() otherwise keeps out of the default listing (each carries
    its real text on ``action._advanced_help``, stashed at registration time
    since the visible ``help=`` is SUPPRESS)."""

    def __init__(self, option_strings, dest, **kwargs):
        kwargs.setdefault("nargs", 0)
        kwargs.setdefault("default", _S)
        kwargs.setdefault("help", "also show this command's resilience/tuning flags")
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        for action in parser._actions:
            real_help = getattr(action, "_advanced_help", None)
            if real_help is not None:
                action.help = real_help
        parser.print_help()
        parser.exit()


_NETWORK_MIRROR_COMMANDS = frozenset({"fetch", "clone", "update", "branches", "sync"})
"""Mirror stages that talk to a forge, and so cannot run offline.

`branches` is in the list because picking the most active branch consults the remote;
`verify` and `status` only read the local workspace, so they stay available -- checking
what you already have is exactly what somebody offline wants."""


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
    g.add_argument("--log-format", dest="log_format", choices=list(LOG_FORMATS), default=_S,
                   help="text (default, human) or json (one JSON object per line, "
                        "carrying the run id, command, repo and duration — for "
                        "unattended runs shipped to a log collector)")
    g.add_argument("--metrics-file", dest="metrics_file", default=_S, metavar="PATH",
                   help="after the run, write Prometheus textfile-collector metrics "
                        "(run duration, repo counts, graph size, last success) to this "
                        "path — point node_exporter's textfile collector at its "
                        "directory to monitor the systemd timer in examples/")
    g.add_argument("--redact", dest="redact", action="store_true", default=_S,
                   help="scrub workspace paths and group names out of the console too "
                        "(the --log-file copy is scrubbed by default, so it can be "
                        "attached to a bug report as-is)")
    g.add_argument("--no-redact", dest="redact", action="store_false", default=_S,
                   help="scrub nothing, including the --log-file copy")
    g.add_argument("--access-log", dest="access_log", action="store_true", default=_S,
                   help="log every request the local HTTP servers (dashboard, "
                        "graph --serve, MCP http/sse) answer; off by default")
    g.add_argument("--plain", action="store_true", default=_S,
                   help="no colour, even on a TTY (same effect as NO_COLOR=1); "
                        "unicode status glyphs (✓⚠✗...) still render")
    g.add_argument("--offline", action="store_true", default=_S,
                   help="refuse every network connection except loopback, so you can "
                        "check for yourself that a command stays local (also "
                        "CONTEXTLAKE_OFFLINE=1); commands that fetch from a forge or a "
                        "hosted model say so and stop")


def _add_mirror(p, hidden=False):
    def add(*names, **kw):
        if hidden:
            kw["help"] = _S
        kw.setdefault("default", _S)
        p.add_argument(*names, **kw)

    add("--work-dir", help="working directory (overrides config file)")
    add("--group", help="GitLab group (overrides config file)")
    add("--repos", metavar="PATTERN",
        help="mirror/index only repos matching this comma-separated glob filter "
             "(e.g. 'team/api,catalog-api,frontend/*') — great for a demo subset. "
             "Patterns are anchored: a plain name matches that repo exactly, never "
             "one that merely contains it. For a substring match, glob it: '*api*'")
    add("--branch", metavar="NAME",
        help="put every repository on this branch instead of picking its most active one. "
             "A repository that does not have it is reported as not having it and stays "
             "on its most active branch, so 'which repos carry release/24.1' is answered "
             "rather than assumed")
    add("-n", "--dry-run", action="store_true", dest="dry_run",
        help="show what would happen without cloning, updating, or switching branches")
    add("--exit-zero-on-partial", action="store_true", dest="exit_zero_on_partial",
        help="exit 0 even when some repositories failed; the default is to exit 1 so "
             "an unattended run (cron, systemd OnFailure=) can see a broken mirror")

    # Resilience/tuning knobs: real automation levers (retry/backoff/worker-pool/
    # safety-check overrides), but nobody guesses these from a bare --help -- every
    # one already has a .contextlake.ini equivalent, so hiding them here removes zero
    # capability. Kept out of the default listing; --help-advanced reveals them.
    def add_advanced(*names, **kw):
        real_help = kw.pop("help", None)
        kw["help"] = _S
        kw.setdefault("default", _S)
        action = p.add_argument(*names, **kw)
        if not hidden and real_help is not None:
            action._advanced_help = real_help

    add_advanced("--clean-corrupted", action="store_true", dest="clean_corrupted",
        help="remove corrupted/incomplete directories before cloning (default: true)")
    add_advanced("--no-clean-corrupted", action="store_false", dest="clean_corrupted",
        help="do not remove corrupted/incomplete directories (fail instead)")
    add_advanced("--max-retries", type=_RETRIES,
        help="total attempts for a failed operation (1 = try once, no retry)")
    add_advanced("--backoff-initial", type=float, help="initial backoff time in seconds")
    add_advanced("--backoff-max", type=float, help="maximum backoff time in seconds")
    add_advanced("--adaptive-workers", action="store_true", dest="adaptive_workers",
        help="enable adaptive worker pool (default: true)")
    add_advanced("--no-adaptive-workers", action="store_false", dest="adaptive_workers",
        help="disable adaptive worker pool (use static max_workers)")
    add_advanced("--min-workers", type=_COUNT, help="minimum workers for the adaptive pool")
    add_advanced("--error-threshold", type=float, help="error rate threshold (0.0-1.0)")
    add_advanced("--protect-working-branches", action="store_true", dest="protect_working_branches",
        help="enable branch protection (default: true)")
    add_advanced("--no-protect-working-branches", action="store_false",
        dest="protect_working_branches",
        help="disable branch protection (allow operations on any branch)")
    add_advanced("--safe-branches",
        help="comma-separated safe branches (default: main,master,develop,development)")
    # No choices=[...] here, for the reason the `completion` positional gives: this
    # codebase defaults advanced flags to SUPPRESS, and older argparse validates that
    # sentinel against choices. The value is checked in apply_cli_overrides instead, which
    # also catches a bad value coming from the config file rather than only from the flag.
    add_advanced("--branch-strategy",
        help="how the most active branch is picked when --branch is not given: "
             "commits | recency | hybrid (default: hybrid)")
    add_advanced("--require-clean-workspace", action="store_true", dest="require_clean_workspace",
        help="require clean workspace before operations (default: true)")
    add_advanced("--no-require-clean-workspace", action="store_false",
        dest="require_clean_workspace",
        help="allow operations with uncommitted changes")
    add_advanced("--auto-stash", action="store_true", dest="auto_stash",
        help="stash uncommitted changes before updating and restore them "
             "afterwards (default: false)")
    add_advanced("--no-auto-stash", action="store_false", dest="auto_stash",
        help="disable automatic stashing")

    if not hidden:
        p.add_argument("--help-advanced", action=_RevealAdvancedHelp)


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
    p.add_argument("--interval", type=_SECONDS, default=_S,
                   help="--watch: seconds between passes (default 60)")


def _add_net(p):
    p.add_argument("--host", default=_S, help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=_PORT, default=_S, help="bind port")


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
    for flag in ("--no-audit", "--no-sync", "--no-connect", "--no-embed", "--no-enrich",
                 "--no-wiki", "--no-diagrams", "--no-docs",
                 "--force", "--watch", "--overview", "--open", "--cdn",
                 "--serve", "--anonymize", "--sample", "--c4", "--c1"):
        add(flag, action="store_true")
    # Same validating types as the leaf parsers: this pre-subparser spelling
    # (`contextlake --limit -1 kb query x`) is a real way in, so a bound checked
    # only on the leaf would leave it wide open.
    for flag, kind in (("--interval", _SECONDS), ("--limit", _COUNT), ("--hops", _HOPS),
                       ("--max-nodes", _COUNT), ("--max-edges", _MAX_EDGES),
                       ("--max-fanout", _COUNT), ("--group-depth", _DEPTH)):
        add(flag, type=kind)
    add("--port", type=_PORT)
    add("--llm", choices=["auto", "ollama", "openai", "builtin", "anthropic", "cli"])
    add("--transport", choices=["stdio", "http", "sse"])
    add("--retriever", choices=("fts", "semantic", "hybrid"))
    add("--direction", choices=["in", "out", "both"])
    add("--format", choices=["html", "dot", "mermaid", "classdiagram", "sequencediagram",
                             "statediagram", "erdiagram", "deploymentdiagram", "graphml",
                             "cypher", "json"])
    add("--layout", choices=["cose", "concentric", "breadthfirst", "circle", "grid", "dagre"])
    add("--site", nargs="?", const="")


def build_parser():
    """Build the argument parser. Kept separate from main() so it is testable."""
    # The flag registry is a process-wide cache of a fixed parser shape; drop it
    # so a rebuilt parser can never be described by a previous build's registry.
    _RootArgumentParser._flags_by_command_cache = None
    parser = _RootArgumentParser(
        prog="contextlake",
        description="A local context layer for AI tools: mirror your repositories, "
                    "index them into a knowledge graph, and serve it over MCP so agents "
                    "answer from real source instead of guessing.",
        formatter_class=_RootHelpFormatter,
        allow_abbrev=False,
        epilog="""
Get started:
  contextlake init                           guided setup: write your config (start here)
  contextlake bootstrap                      one command: mirror + index + connect + steer
  contextlake kb index .                     index the current repo into the local graph
  contextlake kb query "CatalogService"      search the graph (cited file:line hits)
  contextlake kb serve                       expose the graph to your editor over MCP
  contextlake kb dashboard --serve --sample  explore a demo fleet, zero setup

Run 'contextlake mirror' or 'contextlake kb' to list a namespace's commands, and
'contextlake [mirror|kb] <command> --help' for that command's flags and examples.

Docs:   https://sayak.in/contextlake
Issues: https://github.com/sayak-sarkar/contextlake/issues
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_global(parser)
    _add_mirror(parser, hidden=True)
    _root_hidden_flags(parser)

    sub = parser.add_subparsers(dest="command", metavar="<command>",
                                title="commands", required=False)
    # Every leaf parser across BOTH tiers, keyed by verb (aliases included), plus
    # the two namespace parsers -- the flat lookup table the help listing, the
    # "did you mean" suggester and the cross-command flag registry all read. It
    # is deliberately NOT the root's parse choices: since the namespacing cutover
    # only `mirror`/`kb` and the five top-level commands parse at the root.
    # Mutated in place below, so every reference to it is fully populated by the
    # time a parser can error or print help.
    all_parsers = {}
    # `_command_choices` = what THIS level describes and suggests from (the root
    # covers every verb, a namespace parser only its own); `_root_choices` = what
    # actually parses at the root. At the root the two now differ.
    parser._command_choices = all_parsers
    parser._root_choices = sub.choices
    parser._all_parsers = all_parsers

    def namespace(name, description, epilog):
        """A command namespace (`contextlake mirror ...`): its own parser, its
        own subparsers, and the same pre-verb flag surface the root carries --
        so `contextlake mirror --dry-run fetch` parses exactly like the
        `contextlake --dry-run mirror fetch` form always has. Order matters:
        _add_mirror(hidden=True) is what supplies --repos, which is why
        _root_hidden_flags deliberately does not re-add it (see its own note)."""
        np = sub.add_parser(name, help=description, description=description,
                            epilog=epilog, formatter_class=_RootHelpFormatter,
                            allow_abbrev=False)
        _add_global(np)
        _add_mirror(np, hidden=True)
        _root_hidden_flags(np)
        nsub = np.add_subparsers(dest="subcommand", metavar="<command>",
                                 title="commands", required=False)
        np._command_choices = nsub.choices
        np._root_choices = sub.choices
        np._all_parsers = all_parsers
        np._namespace_name = name
        all_parsers[name] = np
        return np, nsub

    mirror_ns, mirror_sub = namespace(
        _MIRROR_NS,
        "mirror repositories from your forge into a local workspace",
        "\nRun 'contextlake mirror <command> --help' for that command's flags "
        "and examples.\n")
    kb_ns, kb_sub = namespace(
        _KB_NS,
        "build, explore, and serve the local knowledge layer over your repositories",
        "\nRun 'contextlake kb <command> --help' for that command's flags "
        "and examples.\n")
    parser._namespace_parsers = {_MIRROR_NS: mirror_ns, _KB_NS: kb_ns}
    _namespace_subs = {_MIRROR_NS: mirror_sub, _KB_NS: kb_sub}

    def command(name, help_, *, aliases=(), epilog=None):
        """Register a leaf under its namespace -- so its prog, usage line, and
        --help all read `contextlake kb index` -- and record it in `all_parsers`
        so the root's help listing and error messages can still describe a verb
        they no longer parse."""
        ns = _NAMESPACE_OF.get(name)
        target = _namespace_subs[ns] if ns else sub
        p = target.add_parser(name, help=help_, description=help_, aliases=list(aliases),
                              epilog=epilog,
                              formatter_class=argparse.RawDescriptionHelpFormatter,
                              allow_abbrev=False)
        _add_global(p)
        for spelling in (name, *aliases):
            all_parsers[spelling] = p
        return p

    # `--version` (a flag, exits during parsing) is the documented way to check
    # the version, but `contextlake version` is a very natural first guess
    # (docker/npm/kubectl all support both spellings) and previously errored as
    # an unknown command; a bare subcommand alias costs nothing to keep in sync
    # since it prints the exact same f-string main() already has.
    command("version", "show contextlake's version number and exit")

    # ---- first run ---------------------------------------------------------
    p = command("init", "guided setup: write your mirror + knowledge-layer config",
                epilog="""
Examples:
  contextlake init                     interactive setup (prompts with defaults)
  contextlake init --skip-interactive  non-interactive, all defaults
  contextlake init --platform github --group my-org --skip-interactive
  contextlake init --local  write config to THIS directory, not ~/

Without --local or --config, init writes ~/.contextlake.ini + ~/.contextlake/kb.toml
(the global config every directory falls back to). --local writes
.contextlake.ini + .contextlake.kb.toml in the current directory instead -- a
project-scoped config that this directory and every subdirectory underneath
it will inherit (nearest ancestor wins; see docs/configuration.md).
                """)
    p.add_argument("--platform", default=_S,
                   help="gitlab (default) | github | bitbucket | gitea | codeberg | forgejo")
    p.add_argument("--group", default=_S, help="the group / org / workspace to mirror")
    p.add_argument("--work-dir", default=_S, help="local workspace directory")
    p.add_argument("--store-dir", default=_S,
                   help="knowledge-layer store directory (default: a .contextlake/kb "
                        "next to the workspace with --local, ~/.contextlake/kb otherwise)")
    p.add_argument("--local", action="store_true", default=_S,
                   help="write config to the current directory instead of ~/ "
                        "(inherited by every subdirectory underneath it)")
    p.add_argument("--kb", dest="kb", action="store_true", default=_S,
                   help="set up the knowledge layer (default: yes)")
    p.add_argument("--no-kb", dest="kb", action="store_false", default=_S,
                   help="write only the mirror config")
    p.add_argument("--no-mirror", dest="no_mirror", action="store_true", default=_S,
                   help="no forge to mirror from: write only the knowledge config, and "
                        "skip the --group requirement. For repositories already on disk")
    p.add_argument("--embeddings", dest="embeddings", action="store_true", default=_S,
                   help="enable semantic search in the generated kb config (the default)")
    p.add_argument("--no-embeddings", dest="embeddings", action="store_false", default=_S,
                   help="write a kb config with semantic search off")
    p.add_argument("--completion", dest="completion", action="store_true", default=_S,
                   help="register shell tab-completion (default: yes, on bash/zsh)")
    p.add_argument("--no-completion", dest="completion", action="store_false", default=_S,
                   help="skip registering shell tab-completion")
    # Not --yes/-y: unlike apt/npm/gh, most of init's prompts aren't yes/no
    # questions at all (platform, group, work_dir, store_dir all take a typed
    # value with a default) -- --yes would misdescribe what it skips. No
    # deprecation window: pre-1.0-in-spirit tool, no external users yet, and
    # the CLI namespacing cutover already established the precedent of a
    # direct rename over an aliased one.
    p.add_argument("--skip-interactive", dest="skip_interactive",
                   action="store_true", default=_S,
                   help="non-interactive: accept defaults / flags, no prompts")
    p.add_argument("--force", action="store_true", default=_S,
                   help="overwrite existing config files")

    p = command("completion",
                "register shell tab-completion for the current shell (bash/zsh/fish)",
                epilog="""
Examples:
  contextlake completion      auto-detect $SHELL and register
  contextlake completion zsh  register for zsh explicitly

Also happens automatically, once, the first time any command runs in a real
interactive terminal (skip this with CONTEXTLAKE_NO_AUTO_COMPLETION=1) -- this
command is for running it again explicitly, on demand, or for a shell other
than $SHELL. Uses the exact mechanism `init` uses (see
docs/cli-reference.md#shell-completion), so both stay idempotent and in sync.
                """)
    # No choices=[...] here (deliberately): combined with nargs="?" and this
    # codebase's SUPPRESS-default convention, older argparse (3.9-3.11; fixed
    # by 3.12) validates the SUPPRESS sentinel itself against choices when the
    # positional is omitted, raising "invalid choice: '==SUPPRESS=='" -- a
    # real cross-version break caught by CI, not local testing (this dev venv
    # runs 3.14). cmd_completion() validates the value itself instead.
    p.add_argument("shell", nargs="?", default=_S,
                   help="override $SHELL auto-detection (bash/zsh/fish)")

    # ---- mirror core -------------------------------------------------------
    for name, help_, epilog in (
        ("fetch", "enumerate the GitLab projects you can access and cache the list", """
Examples:
  contextlake mirror fetch                             list every accessible GitLab project
  contextlake mirror fetch --repos 'team/api,billing'  only projects matching this filter
                """),
        ("clone", "clone repositories missing from the local workspace", """
Examples:
  contextlake mirror clone            clone whatever's missing from the workspace
  contextlake mirror clone --dry-run  show what would be cloned, change nothing
                """),
        ("update", "fetch + fast-forward every existing clone", """
Examples:
  contextlake mirror update                             fetch + fast-forward every clone
  contextlake mirror update --repos 'team/api,billing'  only these repos
                """),
        ("branches", "switch each repo to its most active development branch", """
Examples:
  contextlake mirror branches            switch every repo to its most active branch
  contextlake mirror branches --dry-run  show what would switch, change nothing
                """),
        ("verify", "compare the local workspace against GitLab (read-only)", """
Examples:
  contextlake mirror verify  compare the workspace against GitLab, change nothing
                """),
        ("status", "show sync state without changing anything", """
Examples:
  contextlake mirror status  show sync state, change nothing
                """),
    ):
        _add_mirror(command(name, help_, epilog=epilog))

    p = command("sync", "full mirror: fetch + clone + update + branches + verify",
                epilog="""
Examples:
  contextlake mirror sync               full synchronization
  contextlake mirror sync --dry-run     show what would happen, change nothing
  contextlake mirror sync --auto-stash  stash dirty trees, update, restore them
                """)
    _add_mirror(p)
    _add_report(p, no_audit=True)

    p = command("audit", "per-repo health and age report (JSON + CSV)", epilog="""
Examples:
  contextlake mirror audit                           per-repo health/age report (JSON + CSV)
  contextlake mirror audit --report /tmp/audit.json  write the report to a custom path
                """)
    _add_mirror(p)
    _add_report(p)

    p = command("bootstrap",
                # Every stage the run actually performs, in order. It has drifted twice now:
                # the diagram stage was added and not listed, and the docs stage grew a
                # second output. Both times the one-line description quietly under-reported
                # what the command does, which is the same defect as a surface reporting a
                # partial run as a complete one.
                "one command from nothing to a wired workspace: mirror, index, "
                "connect, embed, enrich, wiki, diagrams, API reference, design notes, "
                "steering",
                epilog="""
Examples:
  contextlake bootstrap                       the full turnkey run
  contextlake bootstrap --no-sync             repos already cloned; skip the mirror
  contextlake bootstrap --no-embed --no-wiki  no model configured yet
  contextlake bootstrap --workspace ~/src     index this directory instead of work_dir
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
    p.add_argument("--no-enrich", dest="no_enrich", action="store_true", default=_S,
                   help="skip the connector-enrichment step")
    p.add_argument("--no-wiki", dest="no_wiki", action="store_true", default=_S,
                   help="skip the wiki-generation step")
    p.add_argument("--no-diagrams", dest="no_diagrams", action="store_true", default=_S,
                   help="skip the architecture-diagram step")
    p.add_argument("--no-docs", dest="no_docs", action="store_true", default=_S,
                   help="skip the API-reference step")
    p.add_argument("--llm", default=_S, metavar="PROVIDER",
                   choices=["auto", "ollama", "openai", "builtin", "anthropic", "cli"],
                   help="power the wiki stage with this LLM provider; without it (and "
                        "without [llm] enabled in kb.toml) the wiki stage no-ops. "
                        "builtin = zero-setup CPU model, ollama | openai | anthropic | cli | auto")
    p.add_argument("--llm-model", dest="llm_model", default=_S, metavar="MODEL",
                   help="model name for --llm (e.g. llama3.1, gpt-4o-mini)")

    # ---- knowledge layer ---------------------------------------------------
    p = command("index", "parse repositories into the local knowledge graph",
                epilog="""
Examples:
  contextlake kb index                  index the current directory
  contextlake kb index path/to/repo     index one repo (same as --source)
  contextlake kb index --workspace ~/w  index every git repo under a folder
  contextlake kb index --force          full re-index (default is incremental)
                """)
    p.add_argument("path", nargs="?", default=_S,
                   help="a repo directory or graph-shard JSON to index (default: cwd)")
    p.add_argument("--source", default=_S,
                   help="a repo directory, a graph shard JSON, or an indexed repo id "
                        "(the id lint and the dashboard print)")
    p.add_argument("--workspace", default=_S,
                   help="index every git repo under this directory")
    p.add_argument("--repos", default=_S, metavar="PATTERN",
                   help="--workspace: index only repos matching this comma-separated "
                        "glob/substring filter (e.g. 'team/api,billing,frontend/*')")
    p.add_argument("--repo", default=_S,
                   help="repo id to index --source under (default: the directory name)")
    p.add_argument("--force", action="store_true", default=_S,
                   help="re-index every repo (default: only repos whose HEAD moved)")
    p.add_argument("--bundle", action="store_true", default=_S,
                   help="index a directory that holds git repos as ONE repo anyway "
                        "(without it, that is refused and the right command printed)")
    _add_watch(p, "the index")

    p = command("source",
                "manage knowledge-source connectors "
                "(Atlassian / Jira / Figma / GitLab / MCP)",
                epilog="""
Examples:
  contextlake kb source add jira --type atlassian --mcp https://mcp.atlassian.com/v1/mcp/authv2
  contextlake kb source add jira --type atlassian --local  scope it to this project, not global
  contextlake kb source list
  contextlake kb source test jira
  contextlake kb source disable jira

`list` and `test` show the effective (merged) config -- the same precedence
chain `connect`/`ingest`/`wiki` use -- so a source defined in a local
.contextlake.kb.toml is visible even if it is not in the file `add`/`remove`
write to. `remove`/`enable`/`disable` mutate a single target file: the
--config path if given, else the nearest ancestor directory's
.contextlake.kb.toml if one already exists (walking up from cwd), else the
global kb.toml; if the named source isn't found there, the message names
that file. --local forces the nearest-ancestor (or a fresh one in cwd if none
exists yet) even when a --config path isn't given. Note the asymmetric exit
codes for a missing name: `remove` is a no-op (exit 0), `enable`/`disable`
fail (exit 1).
                """)
    p.add_argument("action", choices=["add", "list", "remove", "test", "enable", "disable"])
    p.add_argument("name", nargs="?", help="source name (required for all actions except list)")
    p.add_argument("--local", action="store_true", default=_S,
                   help="add/remove/enable/disable: target the nearest ancestor "
                        "directory's .contextlake.kb.toml (or create one in cwd) "
                        "instead of the global config")
    # Spelled out rather than read from kb.source_cmd.known_source_types(): the
    # parser is built on every invocation and importing the source registry to
    # name it would put five module imports on the startup path. A test pins
    # this string against that registry, so the two cannot drift again.
    p.add_argument("--type", default=_S,
                   help="atlassian | api | figma | files | gitlab | graphql | mcp | "
                        "slack | web | zendesk (or an installed plugin's type)")
    p.add_argument("--mcp", default=_S, help="MCP server URL (atlassian/figma/mcp)")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="extra connector option (repeatable)")
    p.add_argument("--from-stdin", default=_S, metavar="KEY",
                   help="add: read this option's value from stdin instead of the "
                        "command line, so it never lands in shell history "
                        "(e.g. printf '%%s' \"$MCP_URL\" | contextlake kb source add jira "
                        "--type atlassian --from-stdin mcp). Literal credentials are "
                        "refused -- name the env var instead: --set token_env=MY_TOKEN")

    p = command("connect", "enrich the graph from configured sources "
                           "(GitLab MRs/issues, Atlassian, Figma)")
    p.add_argument("args", nargs="*", metavar="source",
                   help="only run these named sources (default: all configured)")
    _add_watch(p, "the connectors")

    p = command("embed", "build semantic vectors for the graph (needs [embeddings] config)")
    p.add_argument("--force", action="store_true", default=_S,
                   help="re-embed every repo (default: only changed repos)")
    p.add_argument("--limit", type=_COUNT, default=_S, help="max nodes to embed per repo")
    _add_watch(p, "the embedder")

    p = command("lint", "graph-health checks: stale repos and dangling edges")
    p.add_argument("--json", action="store_true", default=_S,
                   help="machine-readable JSON on stdout instead of formatted text")

    p = command("forget", "remove one repository from the store (graph, vectors, wiki)")
    p.add_argument("repo", metavar="repo", help="the repo id to remove (see `kb lint`)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=_S,
                   help="print what would be removed, remove nothing")

    p = command("docs",
                "generate documentation from the graph (API reference and design notes, "
                "no model)",
                epilog="""
Two documents per repository, neither involving a model, so neither needs an LLM configured.

The API reference lists each repository's callable symbols with their signatures and
THEIR REAL CALL SITES -- every file and line in this repository that calls them, read
from the graph.

Two numbers accompany each symbol and they are not the same: call SITES counts places
that call it, CALLERS counts the distinct definitions those places belong to. A function
called fifty times from one loop has fifty sites and one caller.

The design notes record what the repository's own files say about how it was built: the
dependencies its manifests declare, with each constraint and the line it was written on,
and the values its code reads in the most places. Nothing on that page was ratified by
anyone, and it states counts without explaining them, because a count of readings is
evidence a value is load-bearing and no evidence at all about why.

Examples:
  contextlake kb docs                        every indexed repository
  contextlake kb docs team/app               just this one
  contextlake kb docs --max-symbols 2000     a larger reference for a big repository
""")
    p.add_argument("args", nargs="*", metavar="repo",
                   help="only these repo ids (default: all indexed)")
    p.add_argument("--max-symbols", dest="max_symbols", type=_COUNT, default=_S, metavar="N",
                   help="cap each reference at N symbols, the most-called first "
                        "(default 500). Whatever is left out is stated in the document.")

    p = command("wiki", "generate provenance-stamped wiki pages, gated by a review council")
    p.add_argument("args", nargs="*", metavar="repo",
                   help="only these repo ids (default: all indexed)")
    p.add_argument("--llm", default=_S, metavar="PROVIDER",
                   choices=["auto", "ollama", "openai", "builtin", "anthropic", "cli"],
                   help="enable the LLM tier with this provider, overriding kb.toml "
                        "([llm] enabled+provider). builtin = CPU, no setup (needs the "
                        "llm-local extra); ollama | openai | anthropic | cli | auto")
    p.add_argument("--llm-model", dest="llm_model", default=_S, metavar="MODEL",
                   help="model name for --llm (e.g. llama3.1, gpt-4o-mini)")
    p.add_argument("--namespace", default=_S, metavar="PREFIX",
                   help="generate ONE cluster page for the repos under this repo-id "
                        "prefix (e.g. team/api), narrating their cross-repo coupling")
    p.add_argument("--namespaces", action="store_true", default=_S,
                   help="generate a cluster page for every namespace at --depth")
    p.add_argument("--depth", type=_DEPTH, default=_S, metavar="N",
                   help="--namespaces: repo-id prefix depth to group by (default 2)")
    p.add_argument("--force", action="store_true", default=_S,
                   help="regenerate pages even when the graph is unchanged")

    p = command("steer", "write editor steering files (.mcp.json, AGENTS.md, skills, …)")
    p.add_argument("--out", default=_S,
                   help="directory to write steering files into (default: cwd)")
    p.add_argument("--force", action="store_true", default=_S,
                   help="overwrite non-managed files")

    p = command("refresh", "report whether the graph is current, and update it in the background",
                epilog="""
Examples:
  contextlake kb refresh                         is the graph current? (reads only)
  contextlake kb refresh --refresh               ...and start an update if it is not
  contextlake kb refresh --hook --refresh        the form `kb steer` installs as a
                                                 Claude Code SessionStart hook
""")
    p.add_argument("--refresh", action="store_true", default=_S,
                   help="when something is stale, start `kb index` then `kb steer` in the "
                        "background (detached, so nothing waits on it) instead of only "
                        "reporting")
    p.add_argument("--budget", type=float, default=_S,
                   help="seconds the freshness check may spend (default 3); repos left "
                        "unchecked are reported as unchecked")
    p.add_argument("--hook", action="store_true", default=_S,
                   help="print the one-line summary as Claude Code SessionStart JSON, so "
                        "the answer reaches the agent as context")
    p.add_argument("--json", action="store_true", default=_S,
                   help="machine-readable JSON on stdout instead of formatted text")

    p = command("hook", "install a git post-commit hook that re-indexes a repo on commit",
                epilog="""
Examples:
  contextlake kb hook install                    wire the repo in the current directory
  contextlake kb hook install --workspace ~/src  wire every git repo under a mirror
  contextlake kb hook status --workspace ~/src   show which repos are wired
  contextlake kb hook uninstall                  remove the hook (restores any prior one)

The hook runs `contextlake kb index <repo>` detached after each commit, so the graph
stays current without a manual re-index. It re-uses the repo's stored id (or the
directory name) so it updates the same node, never a duplicate.
                """)
    # No choices=[...] here, for the same reason the `completion` positional
    # skips it (see its own note): nargs="?" + choices= + this codebase's
    # SUPPRESS-default convention makes argparse on Python 3.9-3.11 validate the
    # SUPPRESS sentinel itself against choices when the positional is omitted,
    # so a bare `contextlake kb hook` died with "invalid choice: '==SUPPRESS=='"
    # instead of defaulting to install. cmd_hook() already rejects an unknown
    # action with its own message, so nothing is lost.
    p.add_argument("action", nargs="?", default=_S,
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
  contextlake kb serve                   stdio transport (editor-managed)
  contextlake kb serve --transport http  Streamable HTTP on --host/--port
  contextlake kb serve --transport sse   legacy HTTP+SSE, for clients that
                                          don't yet speak Streamable HTTP

The http/sse transports print a bearer token to stderr at startup and require
it on every request (Authorization: Bearer <token>); set CONTEXTLAKE_MCP_TOKEN
to pin one across restarts. stdio needs no token. See docs/serving-over-mcp.md.
                """)
    p.add_argument("--transport", choices=["stdio", "http", "sse"], default=_S,
                   help="MCP transport (default stdio; http = Streamable HTTP, "
                        "sse = legacy HTTP+SSE for older clients)")
    p.add_argument("--allow-remote", dest="allow_remote", action="store_true", default=_S,
                   help="--transport http/sse: permit a non-loopback --host "
                        "(refused otherwise; the graph would face the network)")
    p.add_argument("--tool-concurrency", dest="tool_concurrency", type=_CONCURRENCY,
                   default=_S, metavar="N",
                   help="how many tool calls may run at once (default 2; also "
                        "$CONTEXTLAKE_MCP_TOOL_CONCURRENCY). Raising it past a few "
                        "slows the server down -- the tools contend on the store")
    _add_net(p)

    p = command("query", "search the graph from the terminal (cited file:line hits)",
                epilog="""
Examples:
  contextlake kb query "CatalogService"
  contextlake kb query charge --kind function --repo billing-service
  contextlake kb query charge --repo billing-service --as-of a1b2c3
  contextlake kb query "how do we charge a card" --retriever semantic
                """)
    p.add_argument("args", nargs="*", metavar="text", help="the search text")
    p.add_argument("--kind", default=_S, help="filter by node kind")
    p.add_argument("--repo", default=_S, help="filter by repo")
    p.add_argument("--limit", type=_COUNT, default=_S, help="max results (default 20)")
    p.add_argument("--as-of", dest="as_of", default=_S,
                   help="search a repo's snapshot at this indexed commit (needs --repo)")
    p.add_argument("--retriever", choices=("fts", "semantic", "hybrid"), default=_S,
                   help="fts (default, keyword) / semantic (embeddings) / hybrid "
                        "(semantic seed + graph rerank) -- semantic and hybrid need "
                        "`contextlake kb embed` to have run first, or this degrades to fts")
    p.add_argument("--json", action="store_true", default=_S,
                   help="machine-readable JSON on stdout instead of formatted text")

    p = command("graph", "visualize a bounded subgraph (HTML/dot/mermaid/JSON)",
                epilog="""
Examples:
  contextlake kb graph --overview                                  repos-as-nodes fleet view
  contextlake kb graph --name CatalogService --hops 2              neighbourhood of a symbol
  contextlake kb graph --repo acme/app --format classdiagram       UML class diagram (Mermaid)
  contextlake kb graph --node ID --format sequencediagram          call-order trace (Mermaid)
  contextlake kb graph --repo acme/app --format statediagram       entity state machine (Mermaid)
  contextlake kb graph --repo acme/app --format erdiagram          table/view ER diagram (Mermaid)
  contextlake kb graph --repo acme/app --format deploymentdiagram  Terraform diagram (Mermaid)
  contextlake kb graph --serve                                     live click-to-expand UI
  contextlake kb graph --site                                      offline cross-linked site
  contextlake kb graph --c4 --group-depth 2                        composed namespace (C4) diagram
  contextlake kb graph --c4 --c1                                   + external-system boxes (C1)
                """)
    p.add_argument("args", nargs="*", metavar="query",
                   help="full-text seed (same as --search)")
    p.add_argument("--node", default=_S, help="seed from this exact node id")
    p.add_argument("--name", default=_S, help="seed from nodes with this exact name (+ --kind)")
    p.add_argument("--search", default=_S, help="seed from a full-text search (+ --kind/--repo)")
    p.add_argument("--overview", action="store_true", default=_S,
                   help="repos-as-nodes with aggregated cross-repo edges")
    p.add_argument("--c4", action="store_true", default=_S,
                   help="composed namespace (C4-style) diagram: repos bucketed into "
                        "namespace boundaries with aggregated cross-repo edges "
                        "(--format dot|html|json; not mermaid/classdiagram/"
                        "sequencediagram/statediagram/erdiagram/deploymentdiagram)")
    p.add_argument("--c1", action="store_true", default=_S,
                   help="with --c4: add external-system boxes for HTTP calls that "
                        "never resolve to any indexed repo's exposed route "
                        "(unclassified -- could be a real third party or just an "
                        "unindexed internal service). Requires --c4.")
    p.add_argument("--kind", default=_S, help="filter seeds by node kind")
    p.add_argument("--repo", default=_S, help="filter seeds by repo")
    p.add_argument("--limit", type=_COUNT, default=_S, help="max seed nodes")
    p.add_argument("--hops", type=_HOPS, default=_S, help="expansion radius (default 2)")
    p.add_argument("--max-nodes", dest="max_nodes", type=_COUNT, default=_S,
                   help="cap on rendered nodes (default 500)")
    p.add_argument("--max-edges", dest="max_edges", type=_MAX_EDGES, default=_S,
                   help="cap on rendered edges for --repo views (default 400 -- a dense "
                        "repo can pack well over 500 edges into 500 nodes, which used to "
                        "exceed Mermaid's own hard maxEdges limit and fail to render)")
    p.add_argument("--max-fanout", dest="max_fanout", type=_COUNT, default=_S,
                   help="per-node neighbour cap, anti-hub (seeded views default to 50; "
                        "a --repo view is uncapped unless you pass this)")
    p.add_argument("--relation", default=_S, help="only follow edges of this relation")
    p.add_argument("--direction", choices=["in", "out", "both"], default=_S,
                   help="edge direction to follow (default both)")
    p.add_argument("--format", default=_S,
                   choices=["html", "dot", "mermaid", "classdiagram", "sequencediagram",
                           "statediagram", "erdiagram", "deploymentdiagram", "graphml",
                           "cypher", "json"],
                   help="output format (default html; classdiagram = UML Mermaid; "
                        "sequencediagram = Mermaid call-order trace, needs --node/--name/--search; "
                        "statediagram = Mermaid entity state machine from guarded field "
                        "assignments, best with --repo (a --name/--node seed reaches "
                        "state nodes but not their transitions past --hops 2); "
                        "erdiagram = Mermaid table/view ER diagram from SQL DDL, no "
                        "attribute data, empty for ORM-only schemas; deploymentdiagram = "
                        "Mermaid Terraform resource diagram grouped by inferred category "
                        "(network/compute/storage/database/security), Terraform-only; "
                        "graphml = Gephi/yEd import; cypher = Neo4j/FalkorDB CREATE statements)")
    p.add_argument("--layout", default=_S,
                   choices=["cose", "concentric", "breadthfirst", "circle", "grid", "dagre"],
                   help="html: initial layout (default cose; switchable in the page; "
                        "dagre is a preview -- layered/directed, renders nodes as HTML "
                        "cards below 400 nodes)")
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
                        "(comma-separated glob/substring); --c4: only include "
                        "matching repos in the namespace boundaries")
    p.add_argument("--group-depth", dest="group_depth", type=_DEPTH, default=_S,
                   help="--c4: namespace-grouping depth from repo-id path prefixes "
                        "(default 1)")
    _add_net(p)

    p = command("doctor", "check the knowledge-layer install and configuration (✓/✗)",
                epilog="""
Examples:
  contextlake doctor                     report only, exactly as it always has
  contextlake doctor --fix               install what your config calls for
  contextlake doctor --fix llm-local     install one capability, whatever the config says
  contextlake doctor --fix --dry-run     print the full plan, change nothing

--fix installs Python packages into THIS interpreter (sys.executable -m pip).
A system package is only ever printed and offered with a y/N prompt at a real
terminal: with --skip-interactive, or when stdin is not a terminal, the command
is printed and nothing privileged runs. git is the only such package --fix
offers; a missing C++ toolchain is reported with advice instead, since the
supported route for llm-local is a prebuilt wheel that needs no compiler.
                """)
    # No choices=[...]: combined with nargs="?" and the SUPPRESS default,
    # argparse on 3.9-3.11 validates the SUPPRESS sentinel itself against
    # choices when the option is omitted (same trap documented on `completion`'s
    # positional below). run_fix() validates the value and lists the valid keys.
    p.add_argument("--fix", nargs="?", const="auto", default=_S, metavar="CAPABILITY",
                   help="install missing dependencies: with no value, only what the "
                        "resolved config calls for; or name one (git, embedder, "
                        "vectors, llm-local)")
    p.add_argument("-n", "--dry-run", action="store_true", dest="dry_run", default=_S,
                   help="--fix: print the full plan without installing anything")
    p.add_argument("--skip-interactive", dest="skip_interactive", action="store_true",
                   default=_S,
                   help="--fix: never prompt; privileged commands are printed, not run")

    p = command("eval", "score a golden-query set against the index "
                        "(precision@k / recall@k / MRR)",
                epilog="""
The golden file, in full. `expected` is a LIST, and `match` decides what it holds:

  {"queries": [
     {"query": "CatalogService", "expected": ["demo_app_catalogservice"]},
     {"query": "charge", "expected": ["charge"], "match": "name", "kind": "function"}
  ]}

  match "id"   (the default) -- `expected` holds node ids, as `kb query --json` prints them
  match "name" -- `expected` holds bare symbol names

Get the ids for a query you already trust with `contextlake kb query <term> --json`.

A malformed file is REJECTED (exit 1, "bad_golden_set"), never scored as zero: this command
exists to gate CI on a metric, so a typo that reported 0.0 would read as a real regression.

Examples:
  contextlake kb eval --golden queries.json
  contextlake kb eval --golden queries.json --retriever hybrid --json
""")
    p.add_argument("--golden", default=_S,
                   help="a golden-query JSON file; see the examples below for its exact "
                        "shape ({queries:[{query, expected:[...], kind?, repo?, match?}]})")
    p.add_argument("--retriever", choices=("fts", "semantic", "hybrid"), default=_S,
                   help="which retriever to score (default: fts; semantic/hybrid "
                        "need embeddings)")
    p.add_argument("--limit", type=_COUNT, default=_S, help="k for precision@k (default 10)")
    p.add_argument("--verify-citations", action="store_true", default=_S,
                   help="also check every returned node's file:line still contains that "
                        "symbol on disk — retrieval metrics say the right node came back, "
                        "this says the citation an agent is told to go read is real")
    p.add_argument("--json", action="store_true", default=_S,
                   help="machine-readable JSON on stdout instead of formatted text "
                        "(for CI: parse a metric and gate on a threshold)")

    p = command("owners", "likely owners / SMEs for a repo or path, from git history",
                aliases=("who-knows",))
    p.add_argument("args", nargs="*", metavar="repo-or-path", help="a repo id or a path")
    p.add_argument("--path", default=_S, help="restrict to a sub-path")
    p.add_argument("--limit", type=_COUNT, default=_S, help="max owners listed (default 10)")
    p.add_argument("--json", action="store_true", default=_S,
                   help="machine-readable JSON on stdout instead of formatted text")

    p = command("impact", "reverse blast radius: what could break if a node changes",
                aliases=("blast-radius",))
    p.add_argument("args", nargs="*", metavar="node-or-symbol",
                   help="a node id or symbol name")
    p.add_argument("--repo", default=_S, help="disambiguate the symbol by repo")
    p.add_argument("--hops", type=_HOPS, default=_S, help="reverse depth (default 3)")
    p.add_argument("--limit", type=_COUNT, default=_S, help="max nodes listed (default 100)")
    p.add_argument("--json", action="store_true", default=_S,
                   help="machine-readable JSON on stdout instead of formatted text")

    p = command("ingest", "aggregate external documents (files/web/api/graphql/mcp sources) "
                          "into the graph")
    p.add_argument("--path", default=_S, help="the path (or URL/endpoint) to ingest")
    p.add_argument("--source-type", dest="source_type", default=_S,
                   help="source type for --path (default 'files')")
    p.add_argument("--for-repo", dest="for_repo", default=_S,
                   help="the indexed repo these documents are ABOUT: every symbol a "
                        "document mentions by name gets linked to it (per-source "
                        "equivalent: for_repo = \"…\" on a [[sources]] entry)")

    p = command("enrich", "query connected sources with codebase terms and store "
                          "enrichment docs",
                epilog="""
Examples:
  contextlake kb enrich                    enrich every indexed repo
  contextlake kb enrich group/app          enrich just this repo
  contextlake kb enrich --workspace ~/src  enrich every repo under a mirror

Unlike `connect` (which reconciles issue keys/links found *in* a repo), enrich
never inspects the repo's text -- it turns the repo's own name and top symbols
into search terms and asks each configured `mcp` (with a `tool`) or `atlassian`
source what it has. Results land in an isolated `@enrich:<repo>` partition.
                """)
    p.add_argument("args", nargs="*", metavar="repo",
                   help="only enrich these repos (default: all indexed)")
    p.add_argument("--workspace", default=_S,
                   help="enrich every git repo under this directory instead of the "
                        "store's indexed repos")

    p = command("dashboard", "the knowledge-system dashboard: fleet / repo / "
                             "relationships / impact / health / search",
                epilog="""
Examples:
  contextlake kb dashboard --serve --sample  explore a demo fleet, zero setup
  contextlake kb dashboard --serve           the live dashboard over your store
  contextlake kb dashboard --site out/       static offline export (see --anonymize)
                """)
    p.add_argument("--serve", action="store_true", default=_S,
                   help="serve the live dashboard (default; uses --host/--port)")
    p.add_argument("--open", action="store_true", default=_S,
                   help="open the dashboard in a browser")
    p.add_argument("--site", nargs="?", const="", default=_S, metavar="DIR",
                   help="build a static offline export into DIR")
    p.add_argument("--repos", default=_S, metavar="PATTERN",
                   help="--site: only include repos matching a pattern")
    p.add_argument("--group-depth", dest="group_depth", type=_DEPTH, default=_S,
                   help="domain-grouping depth from repo-id path prefixes (default 1)")
    p.add_argument("--anonymize", action="store_true", default=_S,
                   help="hash git-author identities + strip external link URLs. "
                        "Works with --site (a shareable export) AND --serve (a live "
                        "dashboard you are about to screen-share)")
    p.add_argument("--sample", action="store_true", default=_S,
                   help="use the bundled demo fleet instead of your local store "
                        "(fictional data; works with --serve and --site)")
    p.add_argument("--allow-mutations", dest="allow_mutations", action="store_true",
                   default=_S,
                   help="--serve: also expose sync/add-repo/MCP-server actions "
                        "(loopback host only; refused with --sample)")
    p.add_argument("--workspace", default=_S,
                   help="--allow-mutations: where 'add repo' clones new repos "
                        "(default: alongside the store)")
    p.add_argument("--llm-chat", dest="llm_chat", action="store_true", default=_S,
                   help="the dashboard's Chat tab always answers via the free graph "
                        "router; this additionally sends its structured result to "
                        "the configured [llm] provider for a prose answer (real "
                        "time/token cost per question, opt in explicitly; "
                        "loopback host only)")
    _add_net(p)

    parser.set_defaults(**_DEFAULTS)
    # The full categorized map goes first (what's available), the hand-picked
    # "Get started" recipes and doc links (already in `epilog`) follow. Built
    # from `all_parsers`, which carries every leaf across both tiers, so one
    # lookup table serves both levels.
    parser.epilog = ("\n" + _categorized_commands_text(parser._command_choices)
                     + "\n" + parser.epilog)
    for ns_name, ns_parser in parser._namespace_parsers.items():
        ns_parser.epilog = ("\n"
                            + _categorized_commands_text(parser._command_choices,
                                                         namespace=ns_name)
                            + "\n" + (ns_parser.epilog or ""))
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

    # Checked here rather than by argparse `choices`, so a bad value is caught whether it
    # arrived on the command line or from the config file. An unrecognised strategy used to
    # fall through `select_most_active_branch` to the hybrid branch, so a typed
    # `--branch-strategy recentcy` silently ran a different selection than the one asked for.
    strategy = config.get("branch_strategy")
    if strategy and strategy not in _BRANCH_STRATEGIES:
        raise ConfigError(
            f"branch_strategy: {strategy!r} is not one of "
            f"{', '.join(sorted(_BRANCH_STRATEGIES))}")

    return config


# Commands whose whole job is defined by the forge group: they enumerate it, or
# they read the project cache keyed on it. `update` is the one mirror verb that
# works purely from what is already on disk (update_repositories takes no group),
# so it stays usable in a workspace whose config never named one.
_GROUP_COMMANDS = frozenset({
    "fetch", "clone", "branches", "verify", "sync", "status", "audit", "bootstrap",
})


def _needs_group(args):
    """Whether this invocation cannot produce an honest result without a group.

    `bootstrap` is the one conditional case: with both --no-sync and --no-audit
    every remaining stage is a knowledge-layer build over repositories already on
    disk, which never touches the group -- refusing that would break a legitimate
    offline workflow.
    """
    if args.command not in _GROUP_COMMANDS:
        return False
    if args.command == "bootstrap":
        return not (getattr(args, "no_sync", False) and getattr(args, "no_audit", False))
    return True


def _group_is_usable(group):
    """Whether a resolved group names a real group rather than nothing at all or
    the shipped placeholder."""
    group = (group or "").strip()
    return bool(group) and group != DEFAULT_CONFIG["gitlab_group"]


def _audit_report_path(args, config):
    """Where the per-repo audit report is written (CLI --report, else cache_dir)."""
    if getattr(args, "report", None):
        return expand_path(args.report)
    cache_file, _ = get_cache_paths(config)
    return os.path.join(os.path.dirname(cache_file) or ".", "repo_audit.json")


# Forge hosts that name nobody. Anything else in `gitlab_host`/`api_base` is a
# self-hosted instance whose hostname identifies the organisation as plainly as
# the group name does, so redaction covers it; redacting the public ones would
# only make a shared log harder to follow while hiding nothing.
_PUBLIC_FORGE_HOSTS = frozenset({
    "gitlab.com", "github.com", "api.github.com", "bitbucket.org",
    "api.bitbucket.org", "gitea.com", "codeberg.org",
})


def _forge_host(config):
    """The self-hosted forge hostname this config points at, else ``""``."""
    raw = (os.environ.get("GITLAB_HOST") or config.get("gitlab_host")
           or config.get("api_base") or "").strip()
    if not raw:
        return ""
    host = raw.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return "" if host in _PUBLIC_FORGE_HOSTS else host


def _audit_workers(config):
    try:
        return int(config.get("max_workers", 8))
    except (TypeError, ValueError):
        return 8


def _diagram_stage(kb):
    """`cmd_graph` bound to bootstrap's own argument shape (see `_diagrams_args`)."""
    def _run(kb_args):
        return kb.cmd_graph(_diagrams_args(kb_args))
    return _run


def _diagrams_args(kb_args):
    """A dedicated namespace for bootstrap's diagram stage.

    Built explicitly rather than by inheriting `kb_args`, because `cmd_graph` reads
    `--format`, `--output`, `--serve`, `--site`, `--c4`, `--c1` and `--overview`, and
    several of those are pre-command globals a user may have passed for a different
    reason entirely. A stage that silently changes what it produces based on an unrelated
    flag is not a stage anyone can rely on.

    **Single-repo stores get the repo view, not the fleet view.** `--overview` is the
    FLEET map -- repos as nodes -- so on a store holding one repository it is correct and
    useless: one node, no edges. README pairs those two commands with a screenshot of a
    dense symbol graph, which is the picture `--repo` produces. Choosing by store shape
    means the stage emits the diagram that has something in it.
    """
    import copy

    a = copy.copy(kb_args)
    a.format = "html"
    a.output = None
    a.serve = False
    a.site = None
    a.c4 = False
    a.c1 = False
    a.open = False
    a.overview = True
    a.repo = None
    try:
        from .kb.cmds._common import _open_store

        store, _ = _open_store(a)
        try:
            repos = [r.id for r in store.list_repos()]
        finally:
            store.close()
        if len(repos) == 1:
            a.overview = False
            a.repo = repos[0]
    except Exception as exc:  # noqa: BLE001 - the fleet view still renders; say why it was chosen
        # Not silent: choosing the fleet view because the shape could not be read is a
        # different outcome from choosing it because the store holds many repos, and a
        # near-empty diagram with no explanation is the exact confusion this stage exists
        # to remove.
        log(f"  Could not tell how many repos this store holds ({exc}); "
            f"drawing the fleet view.")
    return a


def _mirror_stage_label(config) -> str:
    """"Mirror repositories from <forge>", named for the forge actually configured.

    Hardcoding "GitLab" here told a GitHub user their run had used the wrong platform,
    two lines above "Enumerating via the GitHub REST API" proving it had not. The label
    machinery already existed (`core.PLATFORM_LABELS`, added after this same confusion
    was reported once before) and these banners simply never called it.
    """
    from .core import platform_label

    return f"Mirror repositories from {platform_label(config)}"


def _store_has_repos(kb_args) -> bool:
    """Did indexing leave anything downstream can read?

    Deliberately asks the store rather than trusting the stage's exit code: `cmd_index`
    returns non-zero if ANY repo failed, so a fleet where one directory is unreadable and
    every other repo indexed cleanly looks identical to a total failure from the outside.

    Any error here answers True. This gate exists only to stop a *hollow* success, and
    failing to read the store is not evidence that the store is empty -- refusing to run
    the remaining stages because the check itself broke would be a worse outcome than the
    bug it guards.
    """
    try:
        from .kb.cmds._common import _open_store

        store, _ = _open_store(kb_args)
        try:
            return bool(store.list_repos())
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - see the docstring: unknown means "keep going"
        return True


def _bootstrap(args, config, work_dir, gitlab_group, metrics=None):
    """One-command turnkey setup: mirror repos, build the knowledge layer, and write
    editor steering. Optional/unconfigured stages are skipped; a failing stage warns
    but never aborts the rest."""
    import copy

    # ARGUMENT VALIDATION FIRST, before the mirror or the audit runs. Placed after
    # them originally, which cost the user a full mirror pass before the refusal --
    # and, worse, sat after the `from .kb import commands` block, so on a core-only
    # install the ImportError path returned before this ever ran and the refusal
    # silently exited 0. CI's core tier caught exactly that.
    #
    # `--config` is the mirror INI and `--kb-config` is the knowledge config, and a
    # reasonable person passes their kb.toml to `--config`. That used to be silent in
    # BOTH directions: the TOML was parsed as an INI (yielding the default work_dir and
    # group), and the kb stages fell back to ~/.contextlake/kb.toml -- so bootstrap
    # indexed into a store the user had not named. Measured: six repository rows written
    # into a production store by a command carrying an explicit --config.
    #
    # Refusing rather than guessing. Silently reinterpreting the flag would be a second
    # guess about which store the user meant, and guessing wrong is the whole defect.
    if getattr(args, "config", None) and not getattr(args, "kb_config", None):
        from pathlib import Path

        from . import style

        given = Path(expand_path(args.config))
        looks_like_kb = given.suffix.lower() == ".toml"
        if not looks_like_kb and given.is_file():
            try:
                looks_like_kb = "[kb]" in given.read_text(encoding="utf-8", errors="replace")
            except OSError:
                looks_like_kb = False
        if looks_like_kb:
            log(style.fail(
                f"--config {args.config} looks like a knowledge config, but on "
                f"`bootstrap` --config is the mirror INI."))
            log("  The knowledge stages take --kb-config, so as written this would "
                "index into whatever store the default kb.toml names.")
            log(f"  Re-run with: contextlake bootstrap --kb-config {args.config}")
            return 2


    from . import style

    def _stage(title):
        log("")
        log(style.header(title))

    failures = []
    # Offline skips the mirror rather than failing it. bootstrap composes the same five
    # stages the `mirror` verbs run, so without this branch it walked straight into the
    # forge: the socket guard did stop the enumeration, but only after ~26s of retries,
    # and then blamed "a VPN/network drop" for a restriction the user had asked for. The
    # kb stages below are all local, so this is a skip and not an abort -- building the
    # knowledge layer from what is already mirrored is exactly what somebody offline
    # wants, and it is the same resumable state the FetchError path already produces.
    offline_mirror = netguard.offline(args)
    if not getattr(args, "no_sync", False) and offline_mirror:
        _stage(_mirror_stage_label(config))
        log(style.warn(netguard.refuse("mirroring")))
        log("    → Continuing with the local stages; the mirror is left exactly as it is.")
    if not getattr(args, "no_sync", False) and not offline_mirror:
        _stage(_mirror_stage_label(config))
        try:
            mirror = fetch_result(fetch_gitlab_projects(gitlab_group, config), config)
            mirror += clone_missing_repos(work_dir, config, gitlab_group)
            mirror += update_repositories(work_dir, config)
            mirror += switch_repository_branches(work_dir, config, gitlab_group)
            mirror += verify_structure(work_dir, config, gitlab_group)
            # bootstrap owns its own exit, so it also owns reporting its repo
            # counts -- and it is what the shipped systemd unit runs, i.e. the
            # single most important run to have metrics for.
            if metrics is not None:
                metrics.record(mirror)
            # Same rule the mirror commands' exit code follows: a fleet that
            # failed to clone is not a completed stage. Recorded (not raised) so
            # the knowledge stages still run against what did land.
            if mirror.failed and not getattr(args, "exit_zero_on_partial", False):
                failures.append(f"{_mirror_stage_label(config)} "
                                f"({mirror.failed} failed)")
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
            failures.append(f"{_mirror_stage_label(config)} (network)")
    elif not offline_mirror:
        # Only when the *user* asked to skip. Saying "(--no-sync)" for an offline run
        # names a flag they never passed, which is how a clear message becomes a
        # confusing one.
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
        # via the bare ./run-contextlake.py (which uses the system Python) while the
        # `[kb]` extra was installed into a virtualenv. Point at the exact interpreter
        # so the fix is unambiguous.
        log(style.warn("Knowledge layer not installed — skipping index/connect/embed/wiki/steer."))
        log(f"  Running under: {sys.executable}  (missing: {e})")
        log(f"  Fix (this interpreter): {sys.executable} -m pip install 'contextlake[kb]'")
        log("  Or, if you installed contextlake[kb] in a virtualenv, run bootstrap via that venv's "
            "executable (e.g. .venv/bin/contextlake bootstrap) instead of ./run-contextlake.py, "
            "which uses the system Python.")
        return

    # kb stages run against the workspace and the *kb* config (kb.toml), which is
    # distinct from the core sync INI passed as --config. An explicit --workspace
    # wins over the mirror's work_dir (it also receives the steering files).
    workspace = expand_path(args.workspace) if getattr(args, "workspace", None) else work_dir
    kb_args = copy.copy(args)
    kb_args.config = getattr(args, "kb_config", None)
    kb_args.workspace = workspace
    kb_args.source = None
    kb_args.args = []  # defensive: bootstrap has no positional args; _connect_targets
                       # short-circuits on workspace before consulting this
    kb_args.out = workspace

    stages = [("Index the code graph", kb.cmd_index)]
    if not getattr(args, "no_connect", False):
        stages.append(("Connect knowledge sources", kb.cmd_connect))
    if not getattr(args, "no_embed", False):
        stages.append(("Build semantic vectors", kb.cmd_embed))
    if not getattr(args, "no_enrich", False):
        stages.append(("Enrich from connected sources", kb.cmd_enrich))
    if not getattr(args, "no_wiki", False):
        stages.append(("Generate the curated wiki", kb.cmd_wiki))
    if not getattr(args, "no_diagrams", False):
        # Architecture drawings are one of the six outputs this product promises, and
        # bootstrap -- the "one command from nothing to a wired workspace" -- never
        # produced one. `cmd_graph` already reports its own node/edge counts, so an
        # empty diagram announces itself rather than looking finished.
        stages.append(("Draw the architecture", _diagram_stage(kb)))
    if not getattr(args, "no_docs", False):
        # The cheapest of the promised outputs: no model, no network, one pass over shards
        # already on disk. Leaving it out of the one command that goes "from nothing to a
        # wired workspace" would mean the output nobody has to configure is the one nobody
        # gets by default.
        stages.append(("Write the API reference", kb.cmd_docs))
    stages.append(("Write editor steering (.mcp.json, AGENTS.md, …)", kb.cmd_steer))

    for title, fn in stages:
        _stage(title)
        try:
            rc = fn(kb_args)
        except Exception as e:  # noqa: BLE001 - one stage must not abort bootstrap
            rc = 1
            log(f"  {style.warn(title + ' failed')} — {e}",
                error_type=type(e).__name__, error=str(e))
            # Re-raising is not an option here (the remaining stages must still
            # run), so the traceback goes out at DEBUG instead: --verbose shows
            # it, a normal run keeps the one-line summary it has always had.
            log(f"  {title}: traceback", level=logging.DEBUG, exc_info=True)
        if rc:
            failures.append(title)
            # The code graph is foundational — connect/embed/wiki/steer all read it.
            # But "indexing returned non-zero" is not the same as "there is no graph":
            # `cmd_index` fails the run if ANY repo failed, so one unreadable directory
            # among many used to abort the whole bootstrap. Measured: 4 of 6 repos
            # indexed perfectly and connect/embed/wiki/steer never ran.
            #
            # So the abort now turns on the question that actually matters downstream --
            # is there a graph to build on? -- rather than on the exit code of the stage
            # that built it. A partial graph is a usable one, and the failed repos are
            # already reported by `cmd_index` itself.
            if fn is kb.cmd_index and not _store_has_repos(kb_args):
                log(style.warn("Bootstrap aborted — the code graph could not be built; "
                               "nothing downstream can run."))
                log(f"  Indexed workspace: {kb_args.workspace}. If that is not where "
                    "your repos live, pass --workspace DIR (or set work_dir in the config).")
                sys.exit(1)

    log("")
    serve = "contextlake kb serve" + (f" --config {kb_args.config}" if kb_args.config else "")
    if failures:
        log(style.warn(f"Bootstrap finished with {len(failures)} failed stage(s): "
                       f"{', '.join(failures)}."))
        log(f"  Workspace is at {work_dir}; re-run after fixing the above. "
            f"Start the server only once healthy: {serve}")
        sys.exit(1)
    log(style.ok(f"Bootstrap complete — workspace ready at {work_dir}."))
    log(f"  Editors are wired (.mcp.json + steering). Start the knowledge server: {serve}")


def _resolve_command(args, parser):
    """Collapse the two-level command tree onto the single dispatch key the rest
    of the CLI already keys off (``args.command``), so _KB_COMMANDS, the mirror
    elif chain, and kb.cmds.dispatch all keep working unchanged: `contextlake
    mirror fetch` comes out of here as ``command == "fetch"``.
    """
    if args.command in _NAMESPACES:
        if args.subcommand is None:
            # `contextlake mirror` with no verb is a first keystroke, not an
            # error -- show that namespace's front door, exactly as bare
            # `contextlake` shows the root's.
            parser._namespace_parsers[args.command].print_help()
            sys.exit(0)
        args.command = args.subcommand


class _RunMetrics:
    """Collects the numbers ``--metrics-file`` publishes, and writes them once.

    Only ever *reads* counters the run already produced -- ``StageResult``'s
    ok/failed/skipped and the store's own node/edge tables -- so the metrics can
    never disagree with the exit code or the summary line by recounting the same
    work a second time.
    """

    def __init__(self):
        self.started = time.monotonic()
        self.path = None
        self.command = ""
        self.repos = None

    def configure(self, path, command):
        self.path = path
        self.command = command

    def record(self, result):
        if result is None:
            return
        self.repos = {"ok": result.ok, "failed": result.failed, "skipped": result.skipped}

    def write(self, exit_code):
        if not self.path:
            return
        try:
            nodes, edges = observability.graph_counts()
            observability.write_textfile(
                self.path, command_name=self.command,
                duration_seconds=time.monotonic() - self.started,
                exit_code=exit_code, repos=self.repos, nodes=nodes, edges=edges)
        except Exception as e:  # noqa: BLE001 - see below
            # This runs in main()'s `finally`, where an exception would *replace*
            # whatever the run was actually reporting: an unwritable metrics path
            # would turn a clean "Error: <the real problem>" into a traceback
            # about a gauge. Losing the metrics is the lesser failure, so say so
            # and let the original outcome stand.
            log(f"Could not write metrics to {self.path}: {e}")


def main(argv=None):
    """Entry point. Wraps :func:`_run` so that however the run ends -- a normal
    return, ``sys.exit()``, a Ctrl-C, or an unhandled crash re-raised by
    ``--verbose`` -- the metrics file still gets written with the real exit code.
    """
    observability.set_run_id(observability.new_run_id())
    metrics = _RunMetrics()
    exit_code = 0
    try:
        return _run(argv, metrics)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except BaseException:
        exit_code = 1
        raise
    finally:
        metrics.write(exit_code)


def _run(argv, metrics):
    from . import style

    parser = build_parser()
    # Shell tab-completion (bash/zsh/fish/tcsh): a core dependency (see
    # pyproject.toml), so this is always available once contextlake itself is
    # installed -- only registering it with a shell (init offers to do this;
    # docs/cli-reference.md#shell-completion has the manual one-liner) is a separate
    # step. autocomplete() only acts when a shell's completion machinery has
    # set COMP_LINE et al., so this is a silent no-op on every normal
    # invocation. The try/except is defensive, not the primary path: an
    # editable install or a stale environment predating this dependency
    # shouldn't crash the whole CLI over a missing completion library.
    try:
        import argcomplete

        argcomplete.autocomplete(parser)
    except Exception:  # noqa: BLE001,S110 - completion is a nicety, never a hard dependency
        # Deliberately broader than ImportError. argcomplete 3.7.1 shipped PEP 604
        # annotations evaluated at class-definition time while its metadata still
        # advertised Python 3.8+, so on an older interpreter `import argcomplete`
        # raised TypeError, sailed straight through an ImportError-only guard, and
        # took the entire CLI down. Every command, over a tab-completion helper.
        # The guard's own comment already said the intent: a missing or broken
        # completion library must not crash the tool. Any failure here means no
        # tab completion, which is the correct outcome for all of them.
        pass
    args = parser.parse_args(argv)

    # --plain is a friendlier spelling of NO_COLOR=1 -- same code path, so
    # every glyph/colour decision downstream stays in one place (style.py).
    # Set before _resolve_command so a namespace's own help honours it too.
    if getattr(args, "plain", False):
        os.environ["NO_COLOR"] = "1"

    # Bare `contextlake` is a first keystroke, not an error: show the front door
    # (description, command list, getting-started examples) and exit clean.
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    _resolve_command(args, parser)
    args.command = _ALIASES.get(args.command, args.command)

    # Same output `--version` prints (parser.prog is "contextlake"), just also
    # reachable as a subcommand.
    if args.command == "version":
        print(f"{parser.prog} {__version__}")
        sys.exit(0)

    setup_logging(verbose=args.verbose, quiet=args.quiet, log_file=args.log_file,
                  log_format=args.log_format or TEXT, redact=args.redact)

    # Installed before any command runs, so no code path can reach the network ahead of
    # the guard. It blocks at the socket rather than at each caller: a flag checked at
    # every request site only holds for the sites somebody remembered.
    if netguard.offline(args):
        netguard.install()

    # Everything below can emit log lines, so the run's identity has to be in
    # place first. `mirror sync` rather than `sync`: the JSON `command` field
    # should be the string a reader can paste back into a shell.
    observability.set_command(_qualified(args.command))
    observability.set_access_log(bool(args.access_log))
    if args.metrics_file:
        metrics.configure(expand_path(args.metrics_file), _qualified(args.command))
    # $HOME is knowable before any config is read and is the single most common
    # thing a pasted log leaks (the username, and often the employer's directory
    # layout). The group/forge-host rules are added once the mirror config is
    # loaded below; a kb command's store is added when its config resolves.
    #
    # --workspace/--source/--out are registered here rather than on the mirror
    # config path, because that path is the reason redaction was inert for the
    # whole knowledge layer: <workspace> was derived from the sync INI, which no
    # kb command reads, so `kb index --redact --workspace DIR` logged the
    # absolute directory in full under every one of --redact, --no-redact and
    # the default. These are the same directories under a different spelling,
    # and they are known the moment the arguments are parsed.
    _paths = [(os.path.expanduser("~"), "~")]
    for flag, placeholder in (("workspace", "<workspace>"), ("source", "<source>"),
                              ("out", "<out>")):
        value = getattr(args, flag, None)
        if value and value is not _S:
            _paths.append((expand_path(str(value)), placeholder))
    observability.add_redactions(paths=_paths)

    # Zero-step completion setup for anyone who skipped `init` entirely (a pip/
    # uv/pipx install has no post-install hook to hang this on -- see
    # maybe_auto_register_completion's own docstring). Skipped for init/
    # completion themselves: both already own this decision explicitly.
    #
    # Deliberately placed here, before any command-specific config load or
    # validation -- so on a command that goes on to fail (a mistyped
    # --config, a still-placeholder gitlab_group), the completion notice
    # prints first. Considered moving this after each dispatch path's own
    # config load instead, but that config load itself only *warns and
    # continues* on most real problems (a hard failure needs its own --config
    # to point at a literally nonexistent path -- a narrow edge case, and
    # only reachable at all before a first-ever `init`); the notice is its
    # own clearly-delimited line either way, never conflated with the error
    # that follows. Not worth threading this through every dispatch branch
    # for that narrow a case.
    if args.command not in ("init", "completion"):
        from .init_cmd import maybe_auto_register_completion
        maybe_auto_register_completion(quiet=args.quiet)

    # First-run setup writes the config the rest of the tool reads, so it must run
    # before load_config's "no config found" preamble. No [kb] extra needed.
    if args.command == "init":
        try:
            from .init_cmd import cmd_init
            sys.exit(cmd_init(args))
        except KeyboardInterrupt:
            log("Operation cancelled by user")
            sys.exit(130)

    # No [kb] extra needed -- shell completion has nothing to do with the
    # knowledge layer, so this must not route through _KB_COMMANDS.
    if args.command == "completion":
        from .init_cmd import cmd_completion
        sys.exit(cmd_completion(args))

    # Knowledge-layer verbs are handled by the optional kb subsystem and don't
    # need the sync config/preamble. Imported lazily so the core tool runs
    # without the [kb] extra. The import itself (tree-sitter, numpy, the mcp
    # SDK) is the slowest part of a cold start and a very reachable place for a
    # real Ctrl-C to land, so it needs the same KeyboardInterrupt catch as the
    # dispatch call below, not just the fast path.
    if args.command in _KB_COMMANDS:
        try:
            from .kb import commands as kb_commands
        except ImportError as e:
            log(f"The '{args.command}' command needs the knowledge-layer extra: "
                f"pip install 'contextlake[kb]'  ({e})")
            sys.exit(1)
        except KeyboardInterrupt:
            log("Operation cancelled by user")
            sys.exit(130)
        try:
            sys.exit(kb_commands.dispatch(args.command, args))
        except ConfigError as e:
            log(str(e))
            sys.exit(1)
        except KeyboardInterrupt:
            log("Operation cancelled by user")
            sys.exit(130)
        except Exception as e:  # noqa: BLE001 - top-level guard reports and exits
            # The same guard the mirror side has carried for a while, which the
            # kb side never got: anything but ConfigError escaped as a raw
            # traceback, at any verbosity. Measured on a full disk, where a
            # write failure during `kb index` reached the user as
            # `sqlite3.OperationalError: disk I/O error` and thirty lines of
            # stack, with no -v passed and nothing saying what to do about it.
            # Under --verbose the exception is re-raised rather than swallowed,
            # so a crash report can still carry the traceback. Python exits 1 on
            # an unhandled exception, so the exit status is unchanged either way.
            log(f"Error: {e}", error_type=type(e).__name__, error=str(e))
            if args.verbose:
                raise
            sys.exit(1)

    # Load configuration (honouring an explicit --config path if given), then
    # overlay any CLI overrides on top.
    try:
        config = load_config(args.config, cli_group=getattr(args, "group", None))
    except ConfigError as e:
        log(str(e))
        sys.exit(1)
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

    # Write the RESOLVED pair back before anything derives a path from the config:
    # apply_cli_overrides only propagates _TRISTATE_FLAGS/_SCALAR_FLAGS, so
    # --work-dir/--group were previously invisible to get_cache_paths, and two runs
    # against different groups from one config file shared a cache file. Both group
    # spellings, because `group` is the generic alias and takes precedence over
    # `gitlab_group` wherever the two are read together -- leaving one stale would
    # reopen the same collision on the alias path.
    config["work_dir"] = work_dir
    config["group"] = gitlab_group
    config["gitlab_group"] = gitlab_group

    if not _group_is_usable(gitlab_group) and _needs_group(args):
        # load_config already printed the searched-paths diagnostic; all that is
        # missing is the refusal. Exit 2 to match `init`, which rejects this exact
        # placeholder the same way -- the inconsistency was that every mirror
        # command instead carried on, read whatever cache happened to be there,
        # and printed a plausible sync report against a group that does not exist.
        log(style.fail(f"No group configured — refusing to run "
                       f"'{_qualified(args.command)}' against the placeholder "
                       f"{DEFAULT_CONFIG['gitlab_group']!r}."))
        log("  Set gitlab_group in a config file (run 'contextlake init'), or pass "
            "--group <your-group> / --config PATH.")
        sys.exit(2)

    # Now that the two values that identify *whose* fleet this is are known, they
    # can be scrubbed from the log file. The forge URL goes in too: it names the
    # company as reliably as the group does.
    observability.add_redactions(
        paths=[(work_dir, "<workspace>")],
        literals=[(gitlab_group, "<group>"),
                  (_forge_host(config), "<forge-host>")])

    # Widen child git/glab DNS budget for slow corporate resolvers (no-op if the
    # user already set RES_OPTIONS); harmless for non-network commands.
    configure_network_resilience(config)

    log(f"Working directory: {work_dir}")
    try:
        from .core import platform_label
        log(f"{platform_label(config)} group: {gitlab_group}")
    except Exception:  # noqa: BLE001 - an unknown platform is reported by fetch itself
        log(f"Group: {gitlab_group}")
    cache_file, _ = get_cache_paths(config)
    log(f"Cache file: {cache_file}")
    if config.get("dry_run", "false").lower() == "true":
        log("DRY RUN: no repositories will be cloned, updated, or switched")
    log("")

    # These reach a forge or a remote through `git` and `glab`, which are subprocesses
    # with their own sockets -- the in-process guard cannot see them. Refusing here is
    # what makes offline mode true for them rather than merely true for Python's own
    # requests. `status` and `verify` read the local workspace and stay allowed.
    if netguard.offline(args) and args.command in _NETWORK_MIRROR_COMMANDS:
        log(style.fail(netguard.refuse(f"`{_qualified(args.command)}`")))
        sys.exit(2)

    # Stages that mirror repositories report what they did; the rest (audit,
    # status, bootstrap -- which owns its own exit) leave this empty.
    result = StageResult()
    try:
        if args.command == "fetch":
            result = fetch_result(fetch_gitlab_projects(gitlab_group, config), config)
        elif args.command == "clone":
            result = clone_missing_repos(work_dir, config, gitlab_group)
        elif args.command == "update":
            result = update_repositories(work_dir, config)
        elif args.command == "branches":
            result = switch_repository_branches(work_dir, config, gitlab_group)
        elif args.command == "verify":
            result = verify_structure(work_dir, config, gitlab_group)
        elif args.command == "sync":
            log("Starting full synchronization...")
            log("")
            log(style.header(_mirror_stage_label(config)))
            result = fetch_result(fetch_gitlab_projects(gitlab_group, config), config)
            result += clone_missing_repos(work_dir, config, gitlab_group)
            result += update_repositories(work_dir, config)
            result += switch_repository_branches(work_dir, config, gitlab_group)
            result += verify_structure(work_dir, config, gitlab_group)
            # The finale glyph has to track the same total the exit code does: a ✓
            # over a run where every clone failed is the hollow success this whole
            # result type exists to stop.
            log(style.summary_line("ok" if not result.failed else "warn",
                                   "Full synchronization complete"))
            if not getattr(args, "no_audit", False):
                log("")
                log(style.header("Audit repositories (health & age)"))
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
            # The return value is honoured. It used to be discarded, so a `_bootstrap`
            # that refused still exited 0 -- reporting failure while claiming success,
            # which is the class of defect this release is about. `None` is the ordinary
            # "ran to the end" answer and maps to 0.
            rc = _bootstrap(args, config, work_dir, gitlab_group, metrics=metrics)
            if rc:
                sys.exit(rc)
    except KeyboardInterrupt:
        log("Operation cancelled by user")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - top-level guard reports and exits
        # The one-line summary is what a normal run should show. Under --verbose
        # the exception is re-raised instead of swallowed, so the traceback
        # reaches stderr: a crash report used to arrive with nothing but this
        # line in it, and the only way to get more was to ask the reporter to
        # reproduce it under a debugger. Python exits 1 on an unhandled
        # exception, so the exit status is unchanged either way.
        log(f"Error: {e}", error_type=type(e).__name__, error=str(e))
        if args.verbose:
            raise
        sys.exit(1)

    metrics.record(result)

    # Decided outside the try so the intent is unmistakable and no future
    # except-clause can swallow it. Before this, every mirror command exited 0 no
    # matter how much of the fleet failed, so nothing unattended -- the cron
    # wrapper in docs/mirroring-repositories.md, the oneshot systemd unit in examples/ -- could
    # tell a healthy mirror from a dead one.
    if result.failed:
        if getattr(args, "exit_zero_on_partial", False):
            log(style.dim(f"{result.failed} operation(s) failed; exiting 0 "
                          "(--exit-zero-on-partial)"))
        else:
            log(style.warn(f"{result.failed} operation(s) failed — exiting "
                           f"{result.exit_code}. Pass --exit-zero-on-partial to keep "
                           "the previous always-zero exit status."))
            sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
