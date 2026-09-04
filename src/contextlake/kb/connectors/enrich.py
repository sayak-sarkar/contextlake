"""Query-driven enrichment: turn a repo's own codebase into search terms, fan
those terms out to connected sources, and store whatever comes back.

Unlike ``connect`` (which reconciles explicit reference signals - issue keys,
doc links - found in a repo), ``enrich`` never inspects the repo's text for
references. It asks the graph "what is this repo actually about" (its name
plus its highest-degree, most-meaningful symbols) and lets each connected
source answer with whatever it has: Jira/Confluence pages, a wiki search tool
over MCP, anything reachable via ``search_source``. Output lands in its own
``@enrich:<repo>`` partition so a re-run cleanly replaces stale results and
never collides with the code shard or the ``@connect:<repo>`` partition.
"""

from __future__ import annotations

from typing import NamedTuple

from ..embeddings.index import EMBEDDABLE_KINDS
from ..model import Node
from ..resilience import note_unavailable
from ..sources.base import Document
from ..store.shards import GraphShard, write_shard
from ..wiki.generate import repo_brief
from .mcp_query import _cfg_get, _normalize, mcp_tool_query
from .text_match import link_documents_to_symbols


def enrich_partition(repo_id: str) -> str:
    """Store partition holding a repo's query-driven enrichment documents."""
    return f"@enrich:{repo_id}"


def build_terms(store_dir, repo_id: str, *, max_terms: int = 10) -> list[str]:
    """Query terms for ``repo_id``: its name plus its top meaningful symbols.

    The repo name (last ``/``-segment of ``repo_id``) always leads, followed by
    up to ``max_terms - 1`` of :func:`repo_brief`'s ``top_symbols`` (ranked by
    graph degree) whose kind is in :data:`EMBEDDABLE_KINDS` - definitions worth
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


def _atlassian_search(cfg, terms: list[str], *, timeout: float | None = None) -> list[Document]:
    from .atlassian import DEFAULT_MCP_URL, AtlassianConnector

    connector = AtlassianConnector(
        _cfg_get(cfg, "name", "enrich"),
        mcp_url=_cfg_get(cfg, "mcp") or DEFAULT_MCP_URL,
        auth_dir=_cfg_get(cfg, "auth_dir"),
        timeout=timeout if timeout is not None else _cfg_get(cfg, "timeout", 120),
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
    an empty list so one broken source never aborts an ``enrich`` run -- but the
    reason is logged rather than swallowed (see
    :func:`resilience.note_unavailable`), because "found nothing" and "never
    answered" are otherwise the same empty list. Repeated failures trip the
    per-server breaker inside :func:`mcp_client.call_tool`.
    """
    try:
        if _cfg_get(cfg, "tool"):
            return mcp_tool_query(cfg, terms, timeout=timeout)
        if _cfg_get(cfg, "type") == "atlassian":
            return _atlassian_search(cfg, terms, timeout=timeout)
        return []
    except Exception as e:  # an unreachable/misbehaving source yields nothing
        note_unavailable(f"enrich source {_cfg_get(cfg, 'name', '?')!r}", e)
        return []


class EnrichCounts(NamedTuple):
    """What one repo's enrichment did: terms tried, documents stored, edges to code.

    Three numbers, not one. ``documents`` on its own reported a document with no
    edge to any symbol as a success, and a document with no edge to code cannot
    answer a question about code. ``edges`` is the number that says the
    enrichment reached the codebase; both are reported, never one instead of the
    other.

    ``terms`` separates two states the document count alone cannot: a repo that
    was never searched (no graph shard, so :func:`build_terms` had nothing to
    build from) reads the same as a repo that was searched and found nothing.

    Zero ``edges`` beside a non-zero ``documents`` is a state, not a failure. A
    document that discusses the repo in prose without naming a symbol of 3 or
    more characters correctly matches nothing: :func:`match_symbol_mentions` is
    whole-word with ``min_name_len=3``, and
    :func:`link_documents_to_symbols` skips the repo-level fallback for a
    document that matched no symbol.
    """

    terms: int
    documents: int
    edges: int


def _document_node(part: str, doc: Document, source_type: str | None) -> Node:
    return Node(id=f"{part}:{doc.id}", repo=part, kind="document", name=doc.title,
                file=(doc.uri or None),
                attrs={**doc.attrs, "source": source_type, "snippet": (doc.text or "")[:300]})


def run_enrich_repo(
    store, store_dir, cfg, repo_id: str, *, embedder=None, vector_store=None
) -> EnrichCounts:
    """Build query terms from ``repo_id``'s codebase, search every enabled source
    in ``cfg.sources``, and store the results in its ``@enrich:<repo_id>``
    partition (clear-then-write, so re-running never accumulates duplicates).

    Each stored result is additionally matched (whole-word) against ``repo_id``'s
    own symbol names, so a document that names a symbol gets a ``documented_by``
    edge straight to it rather than only existing as an isolated node -- the
    partition used to be written with ``edges=[]`` unconditionally.

    Returns :class:`EnrichCounts`: terms tried, documents stored, and edges
    attached to this repo's code. All three are zero when the repo has no shard
    to build terms from.
    """
    terms = build_terms(store_dir, repo_id)
    if not terms:
        return EnrichCounts(0, 0, 0)

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

    # An enrichment result is, by construction, a document about THIS repo -- so
    # link each one to the symbols of it the result actually names. The edges are
    # stored under the enrichment partition, so the clear_repo below drops a
    # previous run's along with its nodes.
    edges = link_documents_to_symbols(store, repo_id, nodes, texts, "documented_by", "enrich")

    store.clear_repo(part)
    store.upsert_nodes(part, nodes)
    store.upsert_edges(part, edges)
    write_shard(store_dir, GraphShard(repo=part, head_commit="enrich", nodes=nodes, edges=edges))

    # Clear this partition's stale vectors unconditionally (mirroring the graph
    # store.clear_repo above) -- a source that stops returning a doc a prior run
    # embedded, or returns none at all, would otherwise leave orphaned vectors.
    if vector_store is not None:
        vector_store.clear_repo(part)
    if embedder and vector_store and nodes:
        from ..commands import _embed_documents
        batch = getattr(cfg.embeddings, "batch_size", 64)
        _embed_documents(vector_store, embedder, part, nodes, texts, batch)

    # The edge count is returned beside the document count, not discarded. It was
    # computed above, stored on the line after it, and then thrown away, so every
    # caller could only report documents stored -- which reads as success for a
    # document that reached no code at all.
    return EnrichCounts(len(terms), len(nodes), len(edges))
