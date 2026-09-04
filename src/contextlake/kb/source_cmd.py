"""``contextlake kb source`` -- manage ``[[sources]]`` blocks in ``kb.toml``.

A thin CLI verb (add/list/remove/test/enable/disable) over ``config_edit``'s
comment-preserving tomlkit mutation. Mutations (add/remove/enable/disable)
always write a single target file -- an explicit ``--config`` path, else the
global ``kb.toml`` (see ``config_edit.resolve_write_target``). Reads
(list/test) instead resolve through ``load_kb_config``, the same merged
precedence chain (legacy-global -> global -> legacy-local -> local ->
--config) that ``connect``/``ingest``/``wiki`` consume -- so `list` reports
exactly what the running system would actually see, even when a source is
defined in a config file other than the mutation target. When a
remove/enable/disable can't find the name in its single write-target file,
the message names that file, so a global-vs-local mismatch is visible rather
than silently confusing.

Secret *values* are never echoed or stored here -- a token is referenced by
env-var *name* only (``--set token_env=MY_TOKEN``), matching ``init_cmd``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from .. import style
from ..logging_setup import log
from . import config_edit
from .config import load_kb_config

# Connector sources feed `connect` (external reference enrichment); every other
# type -- built-in ingest sources and third-party plugin types alike -- feeds
# `ingest` (RAG documents).
_CONNECT_TYPES = {"atlassian", "figma", "gitlab", "slack", "zendesk"}


def _pipeline_for(source_type: str) -> str:
    return "connect" if source_type in _CONNECT_TYPES else "ingest"


def known_source_types() -> tuple[str, ...]:
    """Every source type this build actually ships: the connector types plus the
    built-in ingest sources and any installed plugin.

    ``--type`` is deliberately an OPEN set -- a plugin registers its own name
    through the ``contextlake.sources`` entry point (see
    :mod:`kb.sources.base`) -- so this is not what ``--type`` accepts. It is
    what a user can rely on being present, and so what a listing of types owes
    them. The CLI help and the interactive prompt each used to carry their own
    hand-written list; they disagreed with each other and both with the build,
    leaving ``slack`` and ``graphql`` shipped but undiscoverable from ``--help``.
    """
    from .sources import discover_sources

    return tuple(sorted(_CONNECT_TYPES | set(discover_sources())))


def _type_prompt() -> str:
    return f"Source type ({'/'.join(known_source_types())})"


# Keys whose value would be a literal credential. Refused rather than written:
# the module's contract (see the docstring above) is that a secret is referenced
# by env-var *name*, and nothing reads a bare `token` anyway -- the sources that
# authenticate (kb/sources/api.py, kb/sources/graphql.py) read `token_env`. A
# secret written where no connector can use it is a leak that buys nothing.
_SECRET_KEYS = {"token", "api_key", "apikey", "access_token", "password", "secret",
                "private_key"}


def _reject_literal_secrets(src: dict) -> str | None:
    """The error text for the first literal-secret key in ``src``, else None."""
    for key in src:
        if key.lower() in _SECRET_KEYS:
            return (f"refusing to write a literal secret into the config: {key!r}. "
                    f"Reference it by environment-variable NAME instead, e.g. "
                    f"--set token_env=MY_TOKEN -- the value stays in your "
                    f"environment and only the name is stored.")
    return None


def _keys_a_discovered_config_may_not_set(src: dict) -> list[str]:
    """The keys in ``src`` that only a config file the user NAMED may set.

    Read out of :mod:`kb.trust` rather than re-listed here. That set was called
    ``EXECUTABLE_SOURCE_KEYS`` while it held one capability class and three more
    stayed open, and a copy in this module would reproduce the same drift with
    the copy failing open. ``scopes`` is in the answer only when it widens the
    grant, matching what ``kb/config.py`` drops.
    """
    from .trust import PRIVILEGED_SOURCE_KEYS, SCOPE_KEY, scopes_widen

    bad = set(PRIVILEGED_SOURCE_KEYS) & set(src)
    if SCOPE_KEY in src and scopes_widen(src[SCOPE_KEY]):
        bad.add(SCOPE_KEY)
    return sorted(bad)


def refusal_for_unloadable_keys(target, explicit_config: str | None,
                                src: dict, *,
                                global_config: str | None = None) -> str | None:
    """The refusal text for a write ``target`` the next load would discard, else ``None``.

    The hole this closes: the wizard asked for an MCP URL, wrote it to a
    project-local ``.contextlake.kb.toml``, printed a checkmark, and the survey
    on the next loop of the same run showed the key gone. ``kb/trust.py`` refuses
    ``mcp``, ``token_env``, ``auth_dir``, ``command``, ``args``, ``mcp_command``
    and a widening ``scopes`` from any config file found by walking up from the
    working directory, so the write landed and the load dropped it. Printing
    success for a write the loader discards is the failure this module treats as
    worse than the bug.

    Public, and taking a path rather than an ``args``, because ``contextlake
    init`` writes source blocks too and had the same defect: it accepted an MCP
    URL, printed a green tick, and the next load dropped the key, after which
    ``connectors/orchestrate.py`` fell back to ``DEFAULT_MCP_URL`` and dialled
    the vendor's hosted endpoint instead of the URL the operator typed. One
    definition here, two callers, rather than a second copy that drifts.

    ``target`` and ``explicit_config`` stay SEPARATE parameters on purpose.
    Collapsing them (passing the target as its own explicit config) would make
    every write privileged, which is this whole gate reopened: ``source add``
    with no ``--config`` and a discovered ancestor file resolves its target to
    that file, and the point is that the target being found is not the user
    naming it.

    ``global_config`` says which path the CALLER treats as the global tier, and
    defaults to ``kb.config.GLOBAL_CONFIG``. ``contextlake init`` needs it
    because it carries its own copy of that path: ``init_cmd._KB_CONFIG``, which
    exists so ``init`` runs without the ``[kb]`` extra installed and is expanded
    at import time rather than at call time. Two constants for one file, and
    this gate read only one of them, so ``init``'s default (global-tier) write
    was judged a discovered project file and every ``mcp`` URL it collected was
    refused. The refusal TEXT still names ``kb.config.GLOBAL_CONFIG``: it tells
    a person where to put the source, and the real global config is the right
    file to name there.
    """
    from .config import GLOBAL_CONFIG
    from .trust import is_privileged_source

    target = Path(target)
    # `is None`, not `or`: `is_privileged_source` spells the same parameter that
    # way, and an empty string here would fall back to the real global config
    # rather than matching nothing. A trust gate must not fail open on a
    # formatting difference (see `_same_file`).
    if global_config is None:
        global_config = GLOBAL_CONFIG
    if is_privileged_source(str(target), explicit_config, global_config=global_config):
        return None
    keys = _keys_a_discovered_config_may_not_set(src)
    if not keys:
        return None
    # Absolute, because `resolve_write_target` hands back a bare
    # ".contextlake.kb.toml" when no ancestor has one yet, and the message tells
    # the user to pass this path to --config from wherever they are standing.
    target = target.resolve()
    return (f"Refusing to write {', '.join(repr(k) for k in keys)} to {target}: "
            f"the next load would drop it and this command would have told you "
            f"it was saved.\n"
            f"  contextlake finds that file by walking up from the working "
            f"directory rather than being told about it, so it may not set a key "
            f"that names an endpoint, a credential variable, an OAuth token "
            f"directory, or a program to run (see SECURITY.md, "
            f"'Workspace trust').\n"
            f"  Two routes keep the key:\n"
            f"  1. Put the source in the global config, which is trusted on every "
            f"run with no extra flag:\n"
            f"       contextlake kb source add ... --config {GLOBAL_CONFIG}\n"
            f"  2. Keep it in this project file and name the file on every command "
            f"that reads it:\n"
            f"       contextlake kb source add ... --config {target}\n"
            f"     then --config {target} on kb connect / kb ingest / kb source "
            f"list as well. The key is honoured on the runs that name the file "
            f"and dropped on the runs that do not.")


def _refuse_keys_the_loader_would_strip(args, src: dict) -> str | None:
    """:func:`refusal_for_unloadable_keys` for one ``kb source`` invocation.

    Keyed on the RESOLVED write target, not on ``--local``.
    ``resolve_write_target`` also lands in a discovered ancestor file with no
    flag at all, once a workspace has one, so gating on the flag would fix one
    spelling of the bug and leave the other.
    """
    config = getattr(args, "config", None)
    return refusal_for_unloadable_keys(
        config_edit.resolve_write_target(config, local=getattr(args, "local", False)),
        config, src)


def _parse_set_flags(pairs: list[str] | None) -> dict:
    """Repeatable ``--set KEY=VALUE`` flags into a dict."""
    out: dict = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"--set expects KEY=VALUE, got {pair!r}")
        out[key] = value
    return out


def _assemble_source(args) -> dict:
    """The source dict from flags + ``--set``, dropping unset (None) fields."""
    src = {
        "type": getattr(args, "type", None),
        "name": getattr(args, "name", None),
        "mcp": getattr(args, "mcp", None),
    }
    src.update(_parse_set_flags(getattr(args, "set", None)))
    return {k: v for k, v in src.items() if v is not None}


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _prompt_missing(src: dict) -> dict:
    """Fill in a missing ``type``/``name``/``mcp`` interactively (TTY only)."""
    from ..init_cmd import _MCP_DEFAULTS, _ask  # lazy: keeps this module import-cheap

    if not src.get("type"):
        src["type"] = _ask(_type_prompt(), "files")
    if not src.get("name"):
        log("  Source name is a local nickname you pick to reference this "
            "connection later (contextlake kb source test <name>) -- it is not "
            "your Atlassian site, Figma team, or any other provider-side ID.")
        src["name"] = _ask("Source name", src["type"])
    if not src.get("mcp") and src["type"] in _MCP_DEFAULTS:
        default_mcp = _MCP_DEFAULTS[src["type"]]
        log(f"  MCP server URL: {src['type']}'s official hosted endpoint is "
            "suggested below; press enter to accept it, or supply your own "
            "self-hosted/enterprise MCP URL instead.")
        mcp_url = _ask("MCP server URL (blank to configure later)", default_mcp)
        if mcp_url:
            src["mcp"] = mcp_url
    return src


# --- add -----------------------------------------------------------------

def _read_stdin_value(key: str) -> str | None:
    """The value for ``--from-stdin KEY``, or ``None`` (with an error already
    logged) if stdin isn't actually piped -- reading from a TTY here would just
    hang, which is a worse failure than a clear message."""
    try:
        if sys.stdin.isatty():
            log(style.fail(f"--from-stdin {key} needs a piped value, e.g.: "
                            f"printf '%s' \"$TOKEN\" | contextlake kb source add ... "
                            f"--from-stdin {key}"))
            return None
    except (AttributeError, ValueError):
        pass
    return sys.stdin.readline().rstrip("\n")


def cmd_source_add(args) -> int:
    try:
        src = _assemble_source(args)
    except ValueError as e:
        log(style.fail(str(e)))
        return 2
    stdin_key = getattr(args, "from_stdin", None)
    if stdin_key:
        # Checked before stdin is read, so a piped secret is never even held in
        # this process's memory on the path that would refuse it anyway.
        refusal = _reject_literal_secrets({stdin_key: None})
        if refusal:
            log(style.fail(refusal))
            return 2
        value = _read_stdin_value(stdin_key)
        if value is None:
            return 2
        src[stdin_key] = value
    refusal = _reject_literal_secrets(src)
    if refusal:
        log(style.fail(refusal))
        return 2
    if not src.get("type") or not src.get("name"):
        if _interactive():
            src = _prompt_missing(src)
        else:
            # The name is POSITIONAL on this command (`kb source add jira
            # --type atlassian`); there is no `--name` flag on `kb source` at
            # all. Naming one sent the reader to a spelling argparse refuses.
            log(style.fail("source add requires --type and a source name, e.g. "
                            "`contextlake kb source add jira --type atlassian` "
                            "(or run interactively)"))
            return 2

    # `--type` is an OPEN set (a plugin registers its own name), but "open" means
    # "whatever is installed", and at this moment that is exactly enumerable. An
    # unrecognised type used to be written out and confirmed with a checkmark, then
    # told to "run `contextlake kb ingest` to pull it in" -- an instruction that can
    # never do anything, because `_pipeline_for` routes every unknown type to ingest
    # and ingest has no class to construct. A typo therefore produced a config entry
    # that looked configured and was inert. Refused here rather than at ingest time,
    # where the source is silently absent from the run.
    known = known_source_types()
    if src["type"] not in known:
        log(style.fail(f"Unknown source type {src['type']!r}. This build can run: "
                       f"{', '.join(known)}."))
        log("  Nothing was written. A source type comes from a built-in or from an "
            "installed plugin, so if this is a plugin type, install it first and re-run "
            "-- the type is then discovered automatically.")
        return 2

    # Checked before the write, so a key the loader would strip costs a refusal
    # rather than a checkmark on a value that will not survive the next load.
    refusal = _refuse_keys_the_loader_would_strip(args, src)
    if refusal:
        log(style.fail(refusal))
        log("  Nothing was written.")
        return 2

    config_edit.add_source(getattr(args, "config", None), src, local=getattr(args, "local", False))
    pipeline = _pipeline_for(src["type"])
    log(style.ok(f"Added source {style.cyan(src['name'])} (type={src['type']})"))
    if pipeline == "connect":
        log("  Run `contextlake kb connect` to enrich the graph from it.")
    else:
        log("  Run `contextlake kb ingest` to pull it in (then `contextlake kb embed` for search).")
    return 0


# --- list ------------------------------------------------------------------

def cmd_source_list(args) -> int:
    """List the EFFECTIVE (merged) config -- what ``connect``/``ingest``/``wiki``
    and ``source test`` actually consume -- not just the single write-target
    file. Keeps `list` and `test` agreeing (see module docstring)."""
    cfg = load_kb_config(getattr(args, "config", None))
    if not cfg.sources:
        log("No sources configured (add one with `contextlake kb source add`)")
        return 0

    log(style.bold(f"{'NAME':<20}{'TYPE':<14}{'PIPELINE':<10}ENABLED"))
    for src in cfg.sources:
        pipeline = _pipeline_for(src.type)
        status = style.green("yes") if src.enabled else style.dim("no")
        log(f"{src.name:<20}{src.type:<14}{pipeline:<10}{status}")
    return 0


# --- remove / enable / disable ----------------------------------------------

def _require_name(args) -> str | None:
    name = getattr(args, "name", None)
    if not name:
        # Positional, not a flag: `kb source enable jira`. See cmd_source_add.
        log(style.fail("this action requires a source name, e.g. "
                        "`contextlake kb source enable jira`"))
        return None
    return name


def _not_found_message(args, name: str) -> str:
    """Name the single file a mutation looked in, so a source that only exists
    in another config file in the precedence chain (e.g. a local
    .contextlake.kb.toml while this looked at the global kb.toml) is a visible
    mismatch, not a silent "not found"."""
    target = config_edit.resolve_write_target(
        getattr(args, "config", None), local=getattr(args, "local", False))
    return (f"No source named {style.cyan(name)} in {target} "
            "(run `contextlake kb source list` to see the effective config; "
            "it may live in another config file)")


def cmd_source_remove(args) -> int:
    name = _require_name(args)
    if name is None:
        return 2
    if config_edit.remove_source(getattr(args, "config", None), name,
                                 local=getattr(args, "local", False)):
        log(style.ok(f"Removed source {style.cyan(name)}"))
    else:
        log(f"{_not_found_message(args, name)} -- nothing to remove")
    return 0


def _cmd_source_set_enabled(args, enabled: bool) -> int:
    name = _require_name(args)
    if name is None:
        return 2
    found = config_edit.set_source_enabled(getattr(args, "config", None), name, enabled,
                                           local=getattr(args, "local", False))
    if not found:
        log(style.fail(_not_found_message(args, name)))
        return 1
    verb = "Enabled" if enabled else "Disabled"
    log(style.ok(f"{verb} source {style.cyan(name)}"))
    return 0


def cmd_source_enable(args) -> int:
    return _cmd_source_set_enabled(args, True)


def cmd_source_disable(args) -> int:
    return _cmd_source_set_enabled(args, False)


# --- test (reachability) ----------------------------------------------------

def _verify_atlassian(src, timeout: float | None = None) -> tuple[bool, str]:
    from .connectors.orchestrate import build_atlassian
    from .mcp_client import McpToolError
    from .resilience import describe, find_in_chain

    conn = build_atlassian(src)
    if timeout is not None:
        conn.timeout = timeout
    # Three outcomes, deliberately three messages. Collapsing them into one
    # "no sites accessible" line sent readers hunting for a permissions problem
    # when the real cause was a request that asked for no product scopes.
    #
    # The rejection branch matches with `find_in_chain`, not `except
    # McpToolError`. The MCP client runs its session under anyio task groups, so
    # a rejection raised inside one reaches this frame wrapped in a doubly
    # nested ExceptionGroup whose str() is "unhandled errors in a TaskGroup
    # (1 sub-exception)". Measured against a spawned server on this tree:
    # `isinstance(e, McpToolError)` is False, so that branch was dead and
    # verify_source's catch-all printed the wrapper text -- the operator lost
    # both the failing tool's name and the server's own words, which are the
    # only two facts the line carried.
    try:
        sites = conn.discover_sites()
    except ValueError as e:
        # `parse_sites` raises this after `call_tool` has returned, outside the
        # task group, so it arrives unwrapped and a direct clause still matches.
        return False, (f"MCP reachable, but its answer was not a site list -- {e}. "
                       "This usually means the tool changed shape; please report it.")
    except Exception as e:  # noqa: BLE001 - classified here, never re-raised
        rejected = find_in_chain(e, McpToolError)
        if rejected is not None:
            return False, (f"MCP reachable, but the server rejected the call to "
                           f"{rejected.tool!r} -- "
                           f"{rejected.detail or 'no detail given'}")
        # Transport-level. `describe` sees through the same wrappers, so a
        # timeout or a refused connection reads as itself.
        return False, f"MCP call failed -- {describe(e)}"
    if not sites:
        return False, ("MCP reachable and authorized, but this token can see no "
                       "Atlassian sites. The token is scoped to "
                       f"{conn.scopes!r} -- if that lacks the product scopes "
                       "(read:jira-work, read:page:confluence), re-authorize after "
                       "clearing the cached grant, or set `scopes` on the source.")
    return True, f"{len(sites)} site(s) reachable"


def _verify_figma(src) -> tuple[bool, str]:
    from .connectors.orchestrate import build_figma

    conn = build_figma(src)
    if not (conn.mcp_url or conn.mcp_command):
        return False, "no Figma MCP configured (set `mcp` or `mcp_command`)"
    extra = getattr(src, "model_extra", None) or {}
    file_key = extra.get("file_key")
    if not file_key:
        return False, ("Figma MCP configured, but no `file_key` to test reachability "
                        "against (add one via `--set file_key=KEY`)")
    ok = conn.verify(file_key, node_id=extra.get("node_id"))
    if ok:
        return True, f"design file {file_key!r} reachable"
    return False, f"MCP configured, but design file {file_key!r} was not reachable"


def _verify_slack(src) -> tuple[bool, str]:
    from .connectors.orchestrate import build_slack

    conn = build_slack(src)
    if not (conn.mcp_url or conn.mcp_command):
        return False, "no Slack MCP configured (set `mcp` or `mcp_command`)"
    extra = getattr(src, "model_extra", None) or {}
    channel = extra.get("channel")
    if not channel:
        return False, ("Slack MCP configured, but no `channel` to test reachability "
                        "against (add one via `--set channel=CHANNEL_ID`)")
    ok = conn.verify(channel)
    if ok:
        return True, f"channel {channel!r} reachable"
    return False, f"MCP configured, but channel {channel!r} was not reachable"


def _verify_mcp(src, timeout: float | None = None) -> tuple[bool, str]:
    import asyncio

    from .sources.mcp import McpSource

    extra = getattr(src, "model_extra", None) or {}
    effective_timeout = timeout if timeout is not None else extra.get("timeout", 60)
    source = McpSource(
        command=extra.get("command"), args=extra.get("args"), url=extra.get("url"),
        env=extra.get("env"), timeout=effective_timeout,
    )
    if not source.command and not source.url:
        return False, "no `command` or `url` configured for this mcp source"
    # Bypass iter_documents()'s intentional exception-swallowing (it treats an
    # unreachable server as "no documents", which a reachability check must not).
    docs = asyncio.run(asyncio.wait_for(source._collect(), source.timeout))
    return True, f"{len(docs)} resource(s) listed"


def _verify_fetching_source(src, timeout: float | None = None) -> tuple[bool, str]:
    """Probe a `web` / `api` / `graphql` source by actually asking it for documents.

    These three were the WORST case in the whole diagnostic: they swallow every network
    error internally, so they were also three of the five types with no probe here. A
    user whose token had expired ran `kb source test`, was told "(source is configured)",
    and exited 0 -- the diagnostic tool confirming that a broken source is fine.

    No connectivity logic is reimplemented: the source is built and asked to iterate, and
    the misses it now records (`sources/base.FetchFailures`) are what gets reported. That
    keeps one definition of "can this be read" and makes the probe agree, by construction,
    with what an actual ingest would do.
    """
    from .sources.base import build_source

    opts = {k: v for k, v in src.model_dump().items()
            if k not in {"type", "name", "enabled", "mcp"}}
    if timeout is not None:
        opts.setdefault("timeout", timeout)
    # A PROBE, not an ingest. `max_pages=1` stops a paginated API from walking its whole
    # history, and the loop below breaks at the first document -- `iter_documents` is a
    # generator, so nothing past that is fetched. Draining it here would make a command
    # users expect to answer in a second take as long as the real run.
    opts.setdefault("max_pages", 1)
    source = build_source(src.type, **opts)
    if source is None:
        return False, f"no builder for type {src.type!r}"
    got = 0
    for _doc in source.iter_documents():
        got = 1
        break
    misses = list(getattr(source, "failures", ()))
    if misses:
        first = "; ".join(f"{tgt} ({why})" for tgt, why in misses[:2])
        more = f" (+{len(misses) - 2} more)" if len(misses) > 2 else ""
        return False, f"{len(misses)} target(s) unreadable: {first}{more}"
    if got:
        return True, "at least one document available"
    # Not a pass. The `misses` branch above already returned, so this line is
    # reached only when `got == 0` AND nothing was recorded as unreachable --
    # the probe read no document and hit no failing target. It used to return
    # True, so a source that dialled nothing drew the same green tick as one
    # that answered -- the absent-check-reads-as-a-pass defect this probe exists
    # to close, one branch over. `files` already answers the same "exists but
    # matched nothing" case with False.
    #
    # Two DIFFERENT facts share that branch, and one message for both told an
    # operator to check a url that was correct and had been dialled, then named
    # a `record path` key that exists on no source type at all. Split here, and
    # each half gets advice that fits the type in hand.
    return False, _nothing_was_read_reason(src.type, source)


def _nothing_was_read_reason(source_type: str, source) -> str:
    """Why a `web`/`api`/`graphql` probe finished with no document and no miss.

    Three distinct answers, because they need three different things done:
    nothing was configured to dial, everything configured was refused before a
    request went out, or a target answered and carried nothing this source could
    read.
    """
    from .sources.base import url_is_fetchable

    targets = _configured_targets(source)
    if not targets:
        key = "url/urls" if source_type == "web" else "url"
        return (f"nothing was dialled: this {source_type} source has no `{key}` "
                f"configured, so the probe had nothing to ask. Set one with "
                f"`--set url=https://...`.")
    # `url_is_fetchable` rather than a copy of the scheme rule. It logs a WARNING
    # when it refuses, so a refused target is named twice on this path -- once by
    # `iter_documents` above and once here. That is the price of one definition
    # of "an ingest fetcher will open this", and a second copy of an allowlist
    # drifting from `sources/base.py` costs more.
    refused = [t for t in targets if not url_is_fetchable(t, source=f"{source_type} source")]
    if len(refused) == len(targets):
        shown = ", ".join(refused[:2])
        more = f" (+{len(refused) - 2} more)" if len(refused) > 2 else ""
        return (f"nothing was dialled: only http and https URLs are fetched, and "
                f"every configured target uses another scheme -- {shown}{more}.")
    dialled = len(targets) - len(refused)
    skipped = (f" ({len(refused)} more target(s) were not dialled: only http and "
               f"https URLs are fetched.)" if refused else "")
    if source_type == "web":
        return (f"{dialled} target(s) answered, and no readable text came back. "
                f"contextlake reads visible page text, so a page whose body is "
                f"built by JavaScript reads as empty here.{skipped}")
    return (f"{dialled} target(s) answered, and no record came back. Check "
            f"`items`, the dotted path to the record list in the response, and "
            f"`text_field` -- a record with no text is skipped.{skipped}")


def _configured_targets(source) -> list[str]:
    """The URLs a built fetching source would dial: ``urls`` (web) or ``url``.

    Read off the built source, not off the config dict, so it reports what the
    source actually holds after its own constructor has normalised a single
    ``url`` into a list.
    """
    urls = getattr(source, "urls", None)
    if urls:
        return [str(u) for u in urls]
    url = getattr(source, "url", None)
    return [str(url)] if url else []


def _verify_files(src, timeout: float | None = None) -> tuple[bool, str]:
    """Probe a `files` source: does its path exist, and does anything match?

    A path typo is the `files` equivalent of an expired token, and it produced the same
    silent zero.
    """
    from .sources.base import build_source

    path = Path(str(getattr(src, "path", ".") or ".")).expanduser()
    if not path.exists():
        return False, f"path does not exist: {path}"
    opts = {k: v for k, v in src.model_dump().items()
            if k not in {"type", "name", "enabled", "mcp"}}
    source = build_source("files", **opts)
    if source is None:
        return False, "no builder for type 'files'"
    # Same reason: stop at the first match rather than reading the whole tree.
    got = 0
    for _doc in source.iter_documents():
        got = 1
        break
    if not got:
        return False, f"{path} exists but no file matched the configured globs"
    return True, "at least one document available"


def verify_source(src, timeout: float | None = None) -> tuple[bool, str]:
    """Best-effort reachability check for a configured source. Never raises.

    Dispatches to each connector's own verify/discovery path -- no connectivity
    logic is reimplemented here. Reused by ``contextlake doctor``.

    ``timeout``, when given, bounds the connector's own reachability call (the
    atlassian and mcp connectors default to a 120s/60s timeout, which would
    otherwise let ``doctor``'s per-source loop hang on an unreachable source).
    Standalone ``source test`` leaves it unset, keeping each connector's
    default.
    """
    try:
        if src.type == "atlassian":
            return _verify_atlassian(src, timeout=timeout)
        if src.type == "figma":
            return _verify_figma(src)
        if src.type == "slack":
            return _verify_slack(src)
        if src.type == "mcp":
            return _verify_mcp(src, timeout=timeout)
        if src.type in {"web", "api", "graphql"}:
            return _verify_fetching_source(src, timeout=timeout)
        if src.type == "files":
            return _verify_files(src, timeout=timeout)
        return False, f"no reachability check for type {src.type!r}"
    except Exception as e:  # noqa: BLE001 - test must report, never raise
        # `describe`, not `str(e)`. Every connector here reaches its server
        # through the MCP client's anyio task groups, and `str()` on the group
        # they raise is "unhandled errors in a TaskGroup (1 sub-exception)" --
        # the reason replaced by no reason. `describe` returns the wrapped
        # exception's own text and is identical to `str(e)` when there is no
        # wrapper.
        from .resilience import describe

        return False, describe(e)


# Types with an actual reachability probe in verify_source() above -- used to
# tell "probed and unreachable" (a real test failure) apart from "no probe
# available for this type" (the source may be perfectly valid; there's simply
# nothing here to dial).
_PROBED_TYPES = {"atlassian", "figma", "slack", "mcp",
                 "web", "api", "graphql", "files"}


def is_probed_type(source_type: str) -> bool:
    """Does ``verify_source`` have a real reachability probe for this type?

    The single public reader of ``_PROBED_TYPES``, so the set has one consumer
    rather than several that can drift. An unlisted type answers False, which
    renders as "nothing was tested" -- a new type is therefore reported as
    untested until someone adds a probe for it, rather than as a pass.
    """
    return source_type in _PROBED_TYPES


# The four answers a survey of one configured source can give. String constants,
# not booleans, because the two non-reachable answers are different facts: doctor
# and the wizard both map these to their own marks, from one derivation here.
SURVEY_OK = "ok"                # probed, and it answered
SURVEY_FAILED = "failed"        # probed, and it did not answer
SURVEY_UNTESTED = "untested"    # no probe exists for this type; nothing was dialled
SURVEY_DISABLED = "disabled"    # switched off in the config; not dialled


def survey_source(src, timeout: float | None = 8) -> tuple[str, str]:
    """One configured source's state and detail: ``(SURVEY_*, text)``.

    ``verify_source`` returns ``(bool, str)`` and keeps that shape -- ``source
    test`` shares it, and changing a return type for one caller is how a
    read/write half-fix starts. The third and fourth states are derived here
    instead, from ``_PROBED_TYPES`` and from ``enabled``.

    A disabled source is not dialled at all. ``kb connect`` (cmds/connect.py)
    and ``kb ingest`` (cmds/ingest.py) both skip disabled sources already, so
    spending a network round trip reporting a broken path on one nothing will
    read was work with no reader.
    """
    if not src.enabled:
        return SURVEY_DISABLED, "disabled"
    ok, detail = verify_source(src, timeout=timeout)
    if ok:
        return SURVEY_OK, detail
    return (SURVEY_FAILED if is_probed_type(src.type) else SURVEY_UNTESTED), detail


def cmd_source_test(args) -> int:
    name = _require_name(args)
    if name is None:
        return 2

    cfg = load_kb_config(getattr(args, "config", None))
    src = next((s for s in cfg.sources if s.name == name), None)
    if src is None:
        log(style.fail(f"No source named {style.cyan(name)}"))
        return 1

    # `survey_source`, not `verify_source`: the disabled state is the fourth
    # answer and this command used to have no branch for it. `source list`
    # reported ENABLED=no, `kb connect` and `kb ingest` skipped it, and `source
    # test` dialled it anyway and drew a green tick -- three surfaces on one
    # fact, two of them right. `timeout=None` keeps the standalone command's
    # existing behaviour of leaving each connector's own default in place;
    # `survey_source`'s 8s default is doctor's per-source bound, not this one's.
    state, detail = survey_source(src, timeout=None)
    label = f"{name} ({src.type})"
    if state == SURVEY_DISABLED:
        # Exit 0, matching SURVEY_UNTESTED below and doctor's advisory mark. The
        # operator switched this source off on purpose, so exit 1 would state
        # "this source failed" about something that was never asked. The defect
        # was the green tick, and 0 stops reading as a pass once the tick is
        # gone and the line says what happened.
        log(f"{style.skip(label)}: DISABLED — not dialled.")
        log(f"  `contextlake kb source list` reports it ENABLED=no, and "
            f"`contextlake kb connect` and `contextlake kb ingest` skip it. "
            f"Nothing here says it works. Run `contextlake kb source enable "
            f"{name}` first to test it.")
        return 0
    if state == SURVEY_UNTESTED:
        # A neutral result, not a failure: this source type has nothing to dial (e.g.
        # `gitlab`, whose reachability belongs to the mirror tier). The source is
        # otherwise perfectly valid.
        #
        # But it must not READ like a pass. This previously printed
        # "(source is configured)" and exited 0, which is what a healthy probe looks
        # like -- so the five unprobed types, three of which swallowed every network
        # error, all reported success no matter what was wrong with them. Those three
        # are now genuinely probed; what is left says plainly that nothing was tested.
        log(f"{style.warn(label)}: NOT TESTED — {detail}")
        log("  The source is configured and this command cannot verify it. Nothing "
            "here says it works.")
        return 0
    ok = state == SURVEY_OK
    log(f"{style.ok(label) if ok else style.fail(label)}: {detail}")
    return 0 if ok else 1


# --- wizard ------------------------------------------------------------------

# The mark each survey state draws. Same four states doctor renders, same
# derivation (survey_source); only the renderer differs, because doctor reports
# through report_line and this reports through log().
_SURVEY_MARK = {
    SURVEY_OK: style.ok,
    SURVEY_FAILED: style.warn,
    SURVEY_UNTESTED: style.skip,
    SURVEY_DISABLED: style.skip,
}

# Named rather than described, because the point of the refusal is to leave the
# user with a command they can run. Keys are per connector, so the generic
# `--set KEY=VALUE` is named instead of one key that would be wrong for most types.
_WIZARD_FLAG_FORM = ("contextlake kb source add NAME --type TYPE "
                     "[--mcp URL] [--set KEY=VALUE]")


def _wizard_survey(cfg) -> None:
    """Print one line per configured source, with the mark its state draws."""
    if not cfg.sources:
        log("No sources are configured yet.")
        return
    log(style.bold("Configured sources"))
    untested = 0
    for src in cfg.sources:
        state, detail = survey_source(src, timeout=8)
        label = f"{src.name} ({src.type})"
        # `.get`, not `[state]`: a state added later degrades to ⊘ ("nothing was
        # tested"), the same safe default is_probed_type documents, rather than
        # raising KeyError in the middle of a survey.
        log(f"  {_SURVEY_MARK.get(state, style.skip)(label)} — {detail}")
        untested += state in (SURVEY_UNTESTED, SURVEY_DISABLED)
    if untested:
        # Said out loud because the two reasons for the same mark are different
        # facts, and neither is a claim that the source works.
        log(f"  {style.dim('⊘ means nothing was tested here, not that anything is wrong.')}")


def cmd_source_wizard(args) -> int:
    """Survey what is configured, then offer to add more, one at a time.

    The survey runs through ``survey_source``, the same path ``doctor`` uses, so
    "is this source reachable" has one answer and not two. One dead source does
    not stop the run: ``verify_source`` catches every exception and reports it.
    """
    from ..init_cmd import _ask_yn  # lazy: keeps this module import-cheap

    if not _interactive():
        # A prompt written to a pipe hangs, and a hang in CI reads as a network
        # problem. Refuse before anything is asked, and name the form that works
        # without a terminal.
        log(style.fail("source wizard needs a terminal: it asks questions, and a "
                       "prompt written to a pipe hangs instead of failing."))
        log(f"  Use the flag form instead: {_WIZARD_FLAG_FORM}")
        return 2

    added = 0
    while True:
        cfg = load_kb_config(getattr(args, "config", None))
        _wizard_survey(cfg)
        # Blank input keeps this default (see init_cmd._ask_yn), so pressing
        # enter ends the run rather than starting an add nobody asked for.
        if not _ask_yn("Add another source?", False):
            log(f"Done: {added} source(s) added. Run the wizard again to add more.")
            return 0
        # The add step is `source add` run interactively -- same prompts, same
        # secret refusal, same write path. A second add path would be a second
        # place to forget _reject_literal_secrets.
        rc = cmd_source_add(SimpleNamespace(
            action="add",
            config=getattr(args, "config", None),
            local=getattr(args, "local", False),
            type=None, name=None, mcp=None, set=None, from_stdin=None))
        # A refused add (a literal secret, an unknown type) wrote nothing, so it
        # must not be counted as one. The loop continues either way: the next
        # survey re-reads the config and shows what is actually there.
        added += rc == 0


# --- dispatch ----------------------------------------------------------------

_ACTIONS = {
    "add": cmd_source_add,
    "list": cmd_source_list,
    "remove": cmd_source_remove,
    "enable": cmd_source_enable,
    "disable": cmd_source_disable,
    "test": cmd_source_test,
    "wizard": cmd_source_wizard,
}


def cmd_source(args) -> int:
    action = getattr(args, "action", None)
    handler = _ACTIONS.get(action)
    if handler is None:
        log(style.fail(f"unknown source action {action!r} "
                        "(use add|list|remove|test|enable|disable|wizard)"))
        return 2
    return handler(args)
