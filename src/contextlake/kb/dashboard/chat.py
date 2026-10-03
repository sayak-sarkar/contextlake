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

``anonymize`` makes the answer safe to screen-share. The router's result is scrubbed
BEFORE the prose layer sees it, so a wiki page's text and real author names reach
neither the browser nor the LLM provider. See :func:`_withhold`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..security import UNTRUSTED_DATA_RULE, sanitize_label, untrusted_block
from . import data as kbdata

_PROSE_MAX_LEN = 4000

_WIKI_WITHHELD_NOTE = (
    "A wiki page exists for this repo, but its text is withheld because this dashboard "
    "was started with --anonymize. Only whether the page exists and whether it is stale "
    "is shown.")
_OWNERS_WITHHELD_GAP = (
    "owner names are withheld because this dashboard was started with --anonymize and "
    "no pseudonym could be derived for this repo")


def chat_answer(
    store, question: str, *, llm=None, embedder=None, vector_store=None,
    anonymize: bool = False,
) -> dict[str, Any]:
    """Answer ``question`` against ``store``. See module docstring for the
    two-layer shape. Always returns a dict with ``structured`` (the router's
    result) and ``answer``/``llm_used`` (the optional prose layer)."""
    structured = asyncio.run(_ask_via_router(store, question, embedder, vector_store))
    if anonymize:
        structured = _withhold(store, structured)
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


def _withhold(store, structured: Any) -> Any:
    """The router's result with the fields ``--anonymize`` withholds taken out.

    Two fields of ``ask``'s answer carry what the other dashboard routes withhold, so
    the scrub is keyed on the FIELD, not on the route that happened to fill it:

    * ``wiki``: the page text. Same rule as ``/api/repo/<id>/wiki``: the ``found`` and
      ``stale`` flags stay, the prose goes.
    * ``owners``: real names. Rebuilt through the same function the owners panel uses,
      so one person has one pseudonym on both. The names are not re-hashed here: the
      router's owner rows carry no e-mail, and the pseudonym is keyed on the e-mail.

    The MCP network path refuses ``ask`` for a key that wants pseudonymous owners,
    because it has no anonymiser. This surface has one, so it answers. Where no
    pseudonym can be derived, the names are dropped and not passed through.
    """
    if not isinstance(structured, dict):
        # Not the shape the scrub knows. Pass nothing on; do not pass it through.
        return {"route": "withheld", "answered": False,
                "note": "the answer could not be anonymised, so it is withheld"}
    out = dict(structured)
    wiki = out.get("wiki")
    if wiki:
        out["wiki"] = {**wiki, "markdown": ""}
        if wiki.get("found"):
            out["note"] = _WIKI_WITHHELD_NOTE
    owners = out.get("owners")
    if owners and owners.get("owners"):
        out["owners"] = _pseudonymous(store, owners)
    return out


def _pseudonymous(store, owners: dict) -> dict:
    """``owners`` with each name replaced by the owners panel's pseudonym."""
    # `ask` calls who_knows with a repo id and no path, so `scope` is the repo id.
    rid = owners.get("scope")
    if not rid or store.get_repo(rid) is None:
        return {**owners, "owners": [], "ranking_gap": _OWNERS_WITHHELD_GAP}
    return {**owners, "owners": kbdata._owners_for(
        store, rid, anonymize=True, limit=len(owners["owners"]))}


async def _ask_via_router(store, question: str, embedder, vector_store) -> Any:
    from mcp import Client

    from ..server import build_server

    mcp = build_server(store, embedder=embedder, vector_store=vector_store)
    async with Client(mcp) as client:
        res = await client.call_tool("ask", {"question": question})
        return res.structured_content


def _prompt(question: str, structured: Any) -> str:
    """The synthesis prompt: the router's result, framed as untrusted data.

    The JSON below is repository content -- symbol names, file paths, docstrings,
    wiki excerpts -- so it is wrapped in an ``untrusted_block`` and the trust rule
    is stated inline. This provider is called with no ``system`` argument (see
    ``chat_answer``), so there is no other place for the rule to live.

    ``question`` is left unwrapped: it is typed by the dashboard operator, not
    read out of an indexed repo, and it is the one instruction the model is
    supposed to act on.
    """
    return (
        f"{UNTRUSTED_DATA_RULE}\n\n"
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
        "Structured data (JSON):\n"
        + untrusted_block(json.dumps(structured, indent=2, default=str),
                          source="knowledge-graph query result")
    )
