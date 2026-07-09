"""Query-driven enrichment: turn a repo's own codebase into search terms, fan
those terms out to connected sources, and store whatever comes back.

Unlike ``connect`` (which reconciles explicit reference signals — issue keys,
doc links — found in a repo), ``enrich`` never inspects the repo's text for
references. It asks the graph "what is this repo actually about" (its name
plus its highest-degree, most-meaningful symbols) and lets each connected
source answer with whatever it has: Jira/Confluence pages, a wiki search tool
over MCP, anything reachable via ``search_source``. Output lands in its own
``@enrich:<repo>`` partition so a re-run cleanly replaces stale results and
never collides with the code shard or the ``@connect:<repo>`` partition.
"""

from __future__ import annotations

from ..embeddings.index import EMBEDDABLE_KINDS
from ..model import Node
from ..sources.base import Document
from ..store.shards import GraphShard, write_shard
from ..wiki.generate import repo_brief
from .mcp_query import _cfg_get, _normalize, mcp_tool_query


def enrich_partition(repo_id: str) -> str:
    """Store partition holding a repo's query-driven enrichment documents."""
    return f"@enrich:{repo_id}"


def build_terms(store_dir, repo_id: str, *, max_terms: int = 10) -> list[str]:
    """Query terms for ``repo_id``: its name plus its top meaningful symbols.

    The repo name (last ``/``-segment of ``repo_id``) always leads, followed by
    up to ``max_terms - 1`` of :func:`repo_brief`'s ``top_symbols`` (ranked by
    graph degree) whose kind is in :data:`EMBEDDABLE_KINDS` — definitions worth
    searching for, not files/packages/modules. Empty if the repo has no shard.
    """
    brief = repo_brief(store_dir, repo_id)
    if brief is None:
        return []
    terms = [repo_id.rsplit("/", 1)[-1]]
    for sym in brief["top_symbols"]:
        if len(terms) >= max_terms:
            break
        name = sym.get("name")
        if sym.get("kind") in EMBEDDABLE_KINDS and name and name not in terms:
            terms.append(name)
    return terms


def _atlassian_search(cfg, terms: list[str]) -> list[Document]:
    from .atlassian import DEFAULT_MCP_URL, AtlassianConnector

    connector = AtlassianConnector(
        _cfg_get(cfg, "name", "enrich"),
        mcp_url=_cfg_get(cfg, "mcp") or DEFAULT_MCP_URL,
        auth_dir=_cfg_get(cfg, "auth_dir"),
        timeout=_cfg_get(cfg, "timeout", 120),
    )
    result = connector.search(" ".join(terms))
    docs = _normalize(result, "atlassian")
    out = []
    for d in docs:
        attrs = {k: v for k, v in d.attrs.items() if k != "tool"}
        attrs["source"] = "atlassian"
        out.append(Document(id=d.id, title=d.title, text=d.text, uri=d.uri, attrs=attrs))
    return out


def search_source(cfg, terms: list[str], *, timeout: float | None = None) -> list[Document]:
    """Query one connected source with ``terms``, as :class:`Document`s.

    Dispatches on the source shape: a generic MCP search ``tool`` (see
    ``mcp_query.py``), or a Rovo ``atlassian`` cross-search. Never raises: any
    failure (unreachable server, unrecognized source, malformed result) yields
    an empty list so one broken source never aborts an ``enrich`` run.
    """
    try:
        if _cfg_get(cfg, "tool"):
            return mcp_tool_query(cfg, terms, timeout=timeout)
        if _cfg_get(cfg, "type") == "atlassian":
            return _atlassian_search(cfg, terms)
        return []
    except Exception:  # an unreachable/misbehaving source yields nothing
        return []


def _document_node(part: str, doc: Document, source_type: str | None) -> Node:
    return Node(id=f"{part}:{doc.id}", repo=part, kind="document", name=doc.title,
                file=(doc.uri or None), attrs={**doc.attrs, "source": source_type})


def enrich_repo(store, store_dir, cfg, repo_id: str, *, embedder=None, vector_store=None) -> int:
    """Build query terms from ``repo_id``'s codebase, search every enabled source
    in ``cfg.sources``, and store the results in its ``@enrich:<repo_id>``
    partition (clear-then-write, so re-running never accumulates duplicates).

    Returns the number of documents stored (0 with no terms or no results).
    """
    terms = build_terms(store_dir, repo_id)
    if not terms:
        return 0

    part = enrich_partition(repo_id)
    seen: set[str] = set()
    nodes: list[Node] = []
    texts: list[str] = []
    for src in cfg.sources:
        if _cfg_get(src, "enabled", True) is False:
            continue
        for doc in search_source(src, terms):
            if doc.id in seen:
                continue
            seen.add(doc.id)
            nodes.append(_document_node(part, doc, _cfg_get(src, "type")))
            texts.append(doc.text)

    store.clear_repo(part)
    store.upsert_nodes(part, nodes)
    write_shard(store_dir, GraphShard(repo=part, head_commit="enrich", nodes=nodes, edges=[]))

    if embedder and vector_store and nodes:
        from ..commands import _embed_documents
        batch = getattr(cfg.embeddings, "batch_size", 64)
        _embed_documents(vector_store, embedder, part, nodes, texts, batch)

    return len(nodes)
