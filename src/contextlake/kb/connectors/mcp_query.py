"""Generic MCP tool-calling connector: template codebase terms into a search tool.

Given a source config naming an MCP search tool and an argument template, this
templates the caller's terms into that template, calls the tool once, and
normalizes whatever shape the server returns (a list of hits, a wrapped dict, or
plain text) into provenance-stamped :class:`Document`s. This is the mechanism the
``enrich`` stage drives: pull related external context for codebase terms without
each connector reimplementing its own result-shape guesswork.
"""

from __future__ import annotations

import json
from typing import Any

from ..mcp_client import call_tool
from ..sources.base import Document

_RESULT_LIST_KEYS = ("results", "items", "data", "hits")
_TITLE_KEYS = ("title", "name")
_URI_KEYS = ("url", "uri", "id")
_TEXT_KEYS = ("text", "snippet", "content", "body")


def _render_args(arg_template: dict, terms: list[str]) -> dict:
    """Deep-walk ``arg_template``, replacing ``{terms}``/``{query}`` in string values."""
    joined = " ".join(terms)

    def render(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("{terms}", joined).replace("{query}", joined)
        if isinstance(value, dict):
            return {k: render(v) for k, v in value.items()}
        if isinstance(value, list):
            return [render(v) for v in value]
        return value

    return render(arg_template or {})


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` off ``cfg``, which may be a plain dict or a SourceCfg model."""
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _first_str(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _doc_from_dict(d: dict, tool: str, index: int) -> Document | None:
    title = _first_str(d, _TITLE_KEYS)
    uri = _first_str(d, _URI_KEYS) or f"mcp://{tool}/{index}"
    text = _first_str(d, _TEXT_KEYS) or json.dumps(d)
    if not text:
        return None
    attrs = {"source": "mcp", "tool": tool}
    mime = d.get("mimeType")
    if mime:
        attrs["mimeType"] = mime
    return Document(id=uri, title=title or uri, text=text, uri=uri, attrs=attrs)


def _documents_from_list(items: list, tool: str) -> list[Document]:
    docs = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        doc = _doc_from_dict(item, tool, i)
        if doc:
            docs.append(doc)
    return docs


def _documents_from_string(text: str, tool: str) -> list[Document]:
    if not text:
        return []
    uri = f"mcp://{tool}"
    return [Document(id=uri, title=tool, text=text, uri=uri,
                      attrs={"source": "mcp", "tool": tool})]


def _normalize(result: Any, tool: str) -> list[Document]:
    if isinstance(result, list):
        return _documents_from_list(result, tool)
    if isinstance(result, dict):
        for key in _RESULT_LIST_KEYS:
            nested = result.get(key)
            if isinstance(nested, list):
                return _documents_from_list(nested, tool)
        # Check if the dict itself has recognized content keys; if so, treat it as a single hit
        has_content = any(result.get(k) for k in _TITLE_KEYS) or \
                     any(result.get(k) for k in _TEXT_KEYS) or \
                     any(result.get(k) for k in _URI_KEYS)
        if has_content:
            return _documents_from_list([result], tool)
        return []
    if isinstance(result, str):
        return _documents_from_string(result, tool)
    return []


def mcp_tool_query(cfg: Any, terms: list[str], *, timeout: float | None = None) -> list[Document]:
    """Call ``cfg``'s MCP search tool with ``terms`` templated in, as ``Document``s.

    ``cfg`` carries the transport (``command``/``args``/``env`` for stdio, or
    ``url`` for streamable-HTTP), the ``tool`` name, and an ``arg_template`` dict.
    Never raises: any failure (missing tool, unreachable server, malformed
    result) yields an empty list.
    """
    try:
        tool = _cfg_get(cfg, "tool")
        if not tool:
            return []
        args = _render_args(_cfg_get(cfg, "arg_template") or {}, terms)
        result = call_tool(
            command=_cfg_get(cfg, "command"),
            args=_cfg_get(cfg, "args") or (),
            url=_cfg_get(cfg, "url"),
            tool=tool,
            arguments=args,
            timeout=timeout or 60,
            env=_cfg_get(cfg, "env"),
        )
        return _normalize(result, tool)
    except Exception:  # noqa: BLE001 - an unreachable/misbehaving server yields nothing
        return []
