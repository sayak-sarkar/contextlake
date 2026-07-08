"""``contextlake source`` -- manage ``[[sources]]`` blocks in ``kb.toml``.

A thin CLI verb (add/list/remove/test/enable/disable) over ``config_edit``'s
comment-preserving tomlkit mutation. This module never writes toml directly --
every mutation goes through ``config_edit``, and every read of "what's
configured, as a name-keyed lookup for a single file" goes through
``config_edit.read_sources``. ``test`` is the exception: it resolves the source
through ``load_kb_config`` (the same precedence chain ``connect``/``ingest``
use), so it reports against what the running system would actually see.

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
    sources = config_edit.read_sources(getattr(args, "config", None))
    if not sources:
        log("No sources configured (add one with `contextlake source add`)")
        return 0

    log(style.bold(f"{'NAME':<20}{'TYPE':<14}{'PIPELINE':<10}ENABLED"))
    for raw in sources:
        name = raw.get("name", "?")
        stype = raw.get("type", "?")
        enabled = raw.get("enabled", True)
        pipeline = _pipeline_for(stype)
        status = style.green("yes") if enabled else style.dim("no")
        log(f"{name:<20}{stype:<14}{pipeline:<10}{status}")
    return 0


# --- remove / enable / disable ----------------------------------------------

def _require_name(args) -> str | None:
    name = getattr(args, "name", None)
    if not name:
        log(style.fail("this action requires --name"))
        return None
    return name


def cmd_source_remove(args) -> int:
    name = _require_name(args)
    if name is None:
        return 2
    if config_edit.remove_source(getattr(args, "config", None), name):
        log(style.ok(f"Removed source {style.cyan(name)}"))
    else:
        log(f"No source named {style.cyan(name)} (nothing to remove)")
    return 0


def _cmd_source_set_enabled(args, enabled: bool) -> int:
    name = _require_name(args)
    if name is None:
        return 2
    found = config_edit.set_source_enabled(getattr(args, "config", None), name, enabled)
    if not found:
        log(style.fail(f"No source named {style.cyan(name)}"))
        return 1
    verb = "Enabled" if enabled else "Disabled"
    log(style.ok(f"{verb} source {style.cyan(name)}"))
    return 0


def cmd_source_enable(args) -> int:
    return _cmd_source_set_enabled(args, True)


def cmd_source_disable(args) -> int:
    return _cmd_source_set_enabled(args, False)


# --- test (reachability) ----------------------------------------------------

def _verify_atlassian(src) -> tuple[bool, str]:
    from .connectors.orchestrate import build_atlassian

    conn = build_atlassian(src)
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


def _verify_mcp(src) -> tuple[bool, str]:
    import asyncio

    from .sources.mcp import McpSource

    extra = getattr(src, "model_extra", None) or {}
    source = McpSource(
        command=extra.get("command"), args=extra.get("args"), url=extra.get("url"),
        env=extra.get("env"), timeout=extra.get("timeout", 60),
    )
    if not source.command and not source.url:
        return False, "no `command` or `url` configured for this mcp source"
    # Bypass iter_documents()'s intentional exception-swallowing (it treats an
    # unreachable server as "no documents", which a reachability check must not).
    docs = asyncio.run(asyncio.wait_for(source._collect(), source.timeout))
    return True, f"{len(docs)} resource(s) listed"


def verify_source(src) -> tuple[bool, str]:
    """Best-effort reachability check for a configured source. Never raises.

    Dispatches to each connector's own verify/discovery path -- no connectivity
    logic is reimplemented here. Reused by ``contextlake doctor``.
    """
    try:
        if src.type == "atlassian":
            return _verify_atlassian(src)
        if src.type == "figma":
            return _verify_figma(src)
        if src.type == "mcp":
            return _verify_mcp(src)
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
