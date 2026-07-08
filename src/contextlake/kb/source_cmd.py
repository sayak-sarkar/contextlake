"""``contextlake source`` -- manage ``[[sources]]`` blocks in ``kb.toml``.

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

from .. import style
from ..logging_setup import log
from . import config_edit
from .config import load_kb_config

# Connector sources feed `connect` (external reference enrichment); every other
# type -- built-in ingest sources and third-party plugin types alike -- feeds
# `ingest` (RAG documents).
_CONNECT_TYPES = {"atlassian", "figma", "gitlab"}


def _pipeline_for(source_type: str) -> str:
    return "connect" if source_type in _CONNECT_TYPES else "ingest"


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
    """Fill in a missing ``type``/``name`` interactively (TTY only)."""
    from ..init_cmd import _ask  # lazy: keeps this module import-cheap

    if not src.get("type"):
        src["type"] = _ask("Source type (atlassian/figma/gitlab/files/web/api/mcp)", "files")
    if not src.get("name"):
        src["name"] = _ask("Source name", src["type"])
    return src


# --- add -----------------------------------------------------------------

def cmd_source_add(args) -> int:
    src = _assemble_source(args)
    if not src.get("type") or not src.get("name"):
        if _interactive():
            src = _prompt_missing(src)
        else:
            log(style.fail("source add requires --type and --name "
                            "(or run interactively)"))
            return 2

    config_edit.add_source(getattr(args, "config", None), src)
    pipeline = _pipeline_for(src["type"])
    log(style.ok(f"Added source {style.cyan(src['name'])} (type={src['type']})"))
    if pipeline == "connect":
        log("  Run `contextlake connect` to enrich the graph from it.")
    else:
        log("  Run `contextlake ingest` to pull it in (then `contextlake embed` for search).")
    return 0


# --- list ------------------------------------------------------------------

def cmd_source_list(args) -> int:
    """List the EFFECTIVE (merged) config -- what ``connect``/``ingest``/``wiki``
    and ``source test`` actually consume -- not just the single write-target
    file. Keeps `list` and `test` agreeing (see module docstring)."""
    cfg = load_kb_config(getattr(args, "config", None))
    if not cfg.sources:
        log("No sources configured (add one with `contextlake source add`)")
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
        log(style.fail("this action requires --name"))
        return None
    return name


def _not_found_message(args, name: str) -> str:
    """Name the single file a mutation looked in, so a source that only exists
    in another config file in the precedence chain (e.g. a local
    .contextlake.kb.toml while this looked at the global kb.toml) is a visible
    mismatch, not a silent "not found"."""
    target = config_edit.resolve_write_target(getattr(args, "config", None))
    return (f"No source named {style.cyan(name)} in {target} "
            "(run `contextlake source list` to see the effective config; "
            "it may live in another config file)")


def cmd_source_remove(args) -> int:
    name = _require_name(args)
    if name is None:
        return 2
    if config_edit.remove_source(getattr(args, "config", None), name):
        log(style.ok(f"Removed source {style.cyan(name)}"))
    else:
        log(f"{_not_found_message(args, name)} -- nothing to remove")
    return 0


def _cmd_source_set_enabled(args, enabled: bool) -> int:
    name = _require_name(args)
    if name is None:
        return 2
    found = config_edit.set_source_enabled(getattr(args, "config", None), name, enabled)
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

    conn = build_atlassian(src)
    if timeout is not None:
        conn.timeout = timeout
    sites = conn.discover_sites()
    if not sites:
        return False, "MCP reachable, but no Atlassian sites accessible to this token"
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
        if src.type == "mcp":
            return _verify_mcp(src, timeout=timeout)
        return False, f"no reachability check for type {src.type!r}"
    except Exception as e:  # noqa: BLE001 - test must report, never raise
        return False, str(e)


def cmd_source_test(args) -> int:
    name = _require_name(args)
    if name is None:
        return 2

    cfg = load_kb_config(getattr(args, "config", None))
    src = next((s for s in cfg.sources if s.name == name), None)
    if src is None:
        log(style.fail(f"No source named {style.cyan(name)}"))
        return 1

    ok, detail = verify_source(src)
    label = f"{name} ({src.type})"
    log(f"{style.ok(label) if ok else style.fail(label)}: {detail}")
    return 0 if ok else 1


# --- dispatch ----------------------------------------------------------------

_ACTIONS = {
    "add": cmd_source_add,
    "list": cmd_source_list,
    "remove": cmd_source_remove,
    "enable": cmd_source_enable,
    "disable": cmd_source_disable,
    "test": cmd_source_test,
}


def cmd_source(args) -> int:
    action = getattr(args, "action", None)
    handler = _ACTIONS.get(action)
    if handler is None:
        log(style.fail(f"unknown source action {action!r} "
                        "(use add|list|remove|test|enable|disable)"))
        return 2
    return handler(args)
