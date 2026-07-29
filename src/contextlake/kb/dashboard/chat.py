"""Natural-language Q&A for the dashboard, built on the existing MCP `ask` tool.

Two layers, always both available in the response:

* **Router (always on, free).** Reuses `contextlake serve`'s own `ask` tool
  unchanged, via an in-process MCP client against a throwaway server instance
  (the same pattern `data.mcp_console` already uses for tool introspection) --
  no logic is duplicated or re-implemented here. Classifies the question,
  dispatches to the matching graph tool, returns a structured, cited result.
  Zero LLM cost, zero new failure surface.
* **LLM synthesis (opt-in at dashboard startup, never per-request).** When the
  caller passes an `LlmClient` (built only if `--llm-chat` was set when the
  dashboard was started), the router's structured result is additionally
  turned into a short prose answer -- grounded in that data, not free-form.
  A failure here degrades to the router-only result; it never breaks the free
  path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..security import sanitize_label

_PROSE_MAX_LEN = 4000


def chat_answer(
    store, question: str, *, llm=None, embedder=None, vector_store=None,
) -> dict[str, Any]:
    """Answer ``question`` against ``store``. See module docstring for the
    two-layer shape. Always returns a dict with ``structured`` (the router's
    result) and ``answer``/``llm_used`` (the optional prose layer)."""
    structured = asyncio.run(_ask_via_router(store, question, embedder, vector_store))
    result: dict[str, Any] = {
        "question": question, "structured": structured,
        "answer": None, "llm_used": False,
    }
    if llm is not None:
        try:
            prose = llm.generate(_prompt(question, structured))
        except Exception as e:  # noqa: BLE001 - an LLM failure must not break the free path
            result["llm_error"] = sanitize_label(str(e))
        else:
            result["answer"] = sanitize_label(prose, max_len=_PROSE_MAX_LEN)
            result["llm_used"] = True
    return result


async def _ask_via_router(store, question: str, embedder, vector_store) -> Any:
    from mcp import Client

    from ..server import build_server

    mcp = build_server(store, embedder=embedder, vector_store=vector_store)
    async with Client(mcp) as client:
        res = await client.call_tool("ask", {"question": question})
        return res.structured_content


def _prompt(question: str, structured: Any) -> str:
    return (
        "Answer the question using ONLY the structured data below -- it comes from "
        "a real code knowledge graph query that has ALREADY run and ALREADY resolved "
        "the relationship in question; you are writing up its result, not "
        "independently re-verifying whether the relationship holds. Trust `route` and "
        "`note`: they state what relationship the returned items already have to the "
        "question (e.g. route=\"callers\" means the listed nodes ARE the callers -- "
        "don't ask for edge/call-site proof the query doesn't return, and don't refuse "
        "to answer just because a field you'd like isn't present). Only say the data "
        "doesn't answer the question when the query genuinely found nothing (an empty "
        "result, or note explicitly says no match) -- and do flag real, stated caveats "
        "from the data itself: `truncated: true` (more results exist), a `stale` wiki, "
        "or a low-confidence relation, since those are genuine, not invented, gaps. Do "
        "not add facts beyond what's in the data.\n\n"
        f"Question: {question}\n\n"
        f"Structured data (JSON):\n{json.dumps(structured, indent=2, default=str)}"
    )
