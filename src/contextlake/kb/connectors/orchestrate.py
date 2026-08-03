"""Orchestrate knowledge-source connectors over indexed repos.

Ties repo-side reference signals (``kb.references``) to a connector's live fetch,
producing a reconciled set of external nodes/edges. Output is stored in a
per-repo partition (``@connect:<repo>``) that is isolated from the code shards, so
re-indexing a repo's code never clobbers its connector links and vice versa.
"""

from __future__ import annotations

from ..cmds.ingest import _embed_documents
from ..ids import make_id
from ..model import Confidence, Node
from .atlassian import (
    DEFAULT_MCP_URL,
    AtlassianConnector,
    associate,
    associate_symbols,
    external_node,
    host_of,
)


def connect_partition(repo_id: str) -> str:
    """Store partition holding a repo's connector output."""
    return f"@connect:{repo_id}"


def build_atlassian(src) -> AtlassianConnector:
    """Construct a connector from a SourceCfg (connector-specific keys via extras)."""
    extra = getattr(src, "model_extra", None) or {}
    return AtlassianConnector(
        src.name,
        mcp_url=src.mcp or DEFAULT_MCP_URL,
        auth_dir=extra.get("auth_dir"),
        timeout=extra.get("timeout", 120),
    )


def build_figma(src):
    """Construct a Figma connector from a SourceCfg."""
    from .figma import DEFAULT_HOSTS, FigmaConnector

    extra = getattr(src, "model_extra", None) or {}
    return FigmaConnector(
        src.name,
        mcp_url=src.mcp,
        mcp_command=extra.get("mcp_command"),
        hosts=extra.get("hosts", DEFAULT_HOSTS),
        auth_dir=extra.get("auth_dir"),
        timeout=extra.get("timeout", 120),
    )


def build_gitlab(src):
    """Construct a GitLab connector from a SourceCfg."""
    from .gitlab import GitLabConnector

    extra = getattr(src, "model_extra", None) or {}
    return GitLabConnector(
        src.name,
        group=extra.get("group"),
        timeout=extra.get("timeout", 30),
        per_page=extra.get("per_page", 50),
    )


def build_slack(src):
    """Construct a Slack connector from a SourceCfg."""
    from .slack import (
        DEFAULT_HISTORY_TOOL,
        DEFAULT_HOSTS,
        DEFAULT_VERIFY_TOOL,
        SlackConnector,
    )

    extra = getattr(src, "model_extra", None) or {}
    return SlackConnector(
        src.name,
        mcp_url=src.mcp,
        mcp_command=extra.get("mcp_command"),
        hosts=extra.get("hosts", DEFAULT_HOSTS),
        verify_tool=extra.get("verify_tool", DEFAULT_VERIFY_TOOL),
        history_tool=extra.get("history_tool", DEFAULT_HISTORY_TOOL),
        auth_dir=extra.get("auth_dir"),
        timeout=extra.get("timeout", 120),
    )


def _embed_connector_nodes(repo_id, nodes, embedder, vector_store) -> None:
    """Embed connector nodes into the semantic store, same pattern
    ``enrich.py``'s own documents already use (see ``_embed_documents``) --
    a no-op whenever an embedder/vector store isn't configured or there's
    nothing to embed."""
    if embedder is None or vector_store is None or not nodes:
        return
    texts = [n.attrs.get("title") or n.attrs.get("name") or n.name for n in nodes]
    _embed_documents(vector_store, embedder, connect_partition(repo_id), nodes, texts,
                      batch_size=32)


def enrich_repo_gitlab(connector, repo_id, store, *, embedder=None, vector_store=None):
    """Link a repo to its open merge requests and issues (live fetch), and each
    MR to the code files its diff actually touches.

    A GitLab diff is a hard fact, not an inference (see
    ``gitlab.match_files_to_nodes``), so every touched-file edge is
    ``Confidence.EXTRACTED``. ``store`` is required to look those file nodes
    up -- it was not previously threaded into this function. When an
    ``embedder``/``vector_store`` pair is configured, the MR/issue nodes built
    here are also embedded so they're semantically searchable, same as
    ``enrich.py``'s own documents.
    """
    from .common import link_to_code
    from .gitlab import associate_gitlab, match_files_to_nodes

    mrs, issues = connector.fetch(repo_id)
    nodes, edges = associate_gitlab(repo_id, mrs, issues)
    by_id = {n.id: n for n in nodes}
    for mr in mrs:
        iid = str(mr.get("iid") or "")
        if not iid:
            continue
        # Mirrors gitlab._item_node's id recipe exactly -- attrs carries no
        # `iid` key to match on, so the node id itself is the reliable link.
        mr_node = by_id.get(make_id("gitlab", "mr", repo_id, iid))
        if mr_node is None:
            continue
        files = connector.fetch_changes(repo_id, iid)
        if not files:
            continue
        matches = match_files_to_nodes(store, repo_id, files)
        if matches:
            edges.extend(link_to_code(repo_id, mr_node, matches, "touches", "gitlab"))
    _embed_connector_nodes(repo_id, nodes, embedder, vector_store)
    return nodes, edges


def enrich_repo_figma(connector, repo_id, store, *, links=(), embedder=None, vector_store=None):
    """Associate figma.com links to design nodes (names come from the URL slug).

    If a Figma MCP is configured, each design's real metadata is fetched
    best-effort and merged in (a real name and/or top structural frame/page
    names, see :func:`figma.parse_metadata`) alongside a ``verified`` flag,
    and each frame/component name that matches an existing symbol in this
    repo is additionally linked straight to that symbol (see
    :func:`figma.match_frame_names_to_symbols`) -- ``AMBIGUOUS``, since a name
    match is inferred, not a hard fact. Never blocks the association graph: an
    unreachable/misconfigured MCP just leaves the design as URL-slug-only,
    same as before. ``store`` is required to look those symbol nodes up --
    mirrors ``enrich_repo_gitlab``, not previously threaded into this function.
    When an ``embedder``/``vector_store`` pair is configured, the design nodes
    built here are also embedded so they're semantically searchable.
    """
    from .common import link_to_code
    from .figma import associate_designs, match_frame_names_to_symbols, parse_metadata

    nodes, edges = associate_designs(repo_id, links=links, site_hosts=connector.hosts)
    seen = {(e.src, e.dst, e.relation) for e in edges}
    for n in nodes:
        if n.kind != "design":
            continue
        meta = connector.fetch_metadata(n.name)
        if meta is None:
            continue
        n.attrs["verified"] = True
        n.attrs.update(parse_metadata(meta))
        matches = match_frame_names_to_symbols(store, repo_id, n.attrs)
        if not matches:
            continue
        # link_to_code always re-appends the repo-level "designed_in" fallback
        # edge that associate_designs already emitted above -- dedupe on the
        # (src, dst, relation) key so it isn't persisted twice.
        for e in link_to_code(repo_id, n, matches, "designed_in", "docs"):
            key = (e.src, e.dst, e.relation)
            if key in seen:
                continue
            seen.add(key)
            edges.append(e)
    _embed_connector_nodes(repo_id, nodes, embedder, vector_store)
    return nodes, edges


def enrich_repo_slack(connector, repo_id, store, *, links=(), embedder=None, vector_store=None):
    """Associate slack.com links to channel/message nodes (from the URL itself).

    If a Slack MCP is configured, each channel is additionally checked for
    reachability (flagged ``verified``) and its recent message history is
    fetched best-effort and matched (whole-word) against this repo's existing
    symbol names via :func:`text_match.match_symbol_mentions`; any symbol
    mentioned gets a direct ``discussed_in`` edge, ``Confidence.AMBIGUOUS``
    (same as Figma's frame-name matching -- a text mention is inferred, not a
    hard fact). Both checks are best-effort and never block the association
    graph. ``store`` is required to look those symbol nodes up -- mirrors
    ``enrich_repo_gitlab``/``enrich_repo_figma``, not previously threaded into
    this function.

    A channel keeps its existing repo-level ``referenced_in`` edge (from a doc
    link, see :func:`slack.associate_slack`) *and* gains a second repo-level
    ``discussed_in`` edge here (``link_to_code``'s always-present fallback) when
    its history actually mentions a symbol -- these are deliberately NOT deduped
    against each other, unlike Figma's identical-relation case: "referenced in
    docs" and "discussed in messages" are different facts with different
    provenance, the same reasoning that already lets GitLab's per-file
    ``touches`` edges coexist with its own repo-level ``tracked_by`` edge.
    When an ``embedder``/``vector_store`` pair is configured, the channel
    nodes built here are also embedded so they're semantically searchable.
    """
    from .common import link_to_code
    from .slack import associate_slack
    from .text_match import match_symbol_mentions, symbol_nodes_for_repo

    nodes, edges = associate_slack(repo_id, links=links, site_hosts=connector.hosts)
    symbols: list[Node] | None = None
    for n in nodes:
        if n.kind != "channel":
            continue
        if connector.verify(n.name):
            n.attrs["verified"] = True
        messages = connector.fetch_messages(n.name)
        if not messages:
            continue
        if symbols is None:  # fetched at most once per call, only if some channel has messages
            symbols = symbol_nodes_for_repo(store, repo_id)
        if not symbols:
            continue
        # One matcher pass over all of a channel's messages joined -- \b word-boundary
        # matching is unaffected by the newline join, and match_symbol_mentions already
        # dedupes by symbol id internally, so there's no need to loop per message.
        matches = match_symbol_mentions("\n".join(messages), symbols)
        if matches:
            edges.extend(link_to_code(repo_id, n, matches, "discussed_in", "slack"))
    _embed_connector_nodes(repo_id, nodes, embedder, vector_store)
    return nodes, edges


def reconcile(nodes, edges, confirmed):
    """Prune and enrich the candidate graph against a live verification result.

    ``confirmed`` is ``{issue_key: {summary,status,url}}`` from a JQL pass. Rule:
    AMBIGUOUS git-ref issue edges survive only if their key was confirmed (then
    promoted to INFERRED and the node enriched); explicit doc-link edges (INFERRED)
    and page edges are kept as-is.
    """
    by_id = {n.id: n for n in nodes}
    confirmed_by_id = {external_node("issue", k).id: k for k in confirmed}
    keep_ids: set[str] = set()
    out_edges = []
    for e in edges:
        dst = by_id.get(e.dst)
        if dst is not None and dst.kind == "issue" and e.confidence == Confidence.AMBIGUOUS:
            if e.dst in confirmed_by_id:
                out_edges.append(e.model_copy(update={"confidence": Confidence.INFERRED}))
                keep_ids.add(e.dst)
            # else: drop the unverified candidate
        else:
            out_edges.append(e)
            keep_ids.add(e.dst)

    out_nodes = []
    for n in nodes:
        if n.kind == "repo":
            out_nodes.append(n)
            continue
        if n.id not in keep_ids:
            continue
        meta = confirmed.get(confirmed_by_id.get(n.id, ""))
        if meta:
            attrs = dict(n.attrs)
            for k in ("summary", "status", "url"):
                if meta.get(k):
                    attrs[k] = meta[k]
            out_nodes.append(n.model_copy(update={"attrs": attrs}))
        else:
            out_nodes.append(n)
    return out_nodes, out_edges


def enrich_repo(connector, sites, repo_id, *, issue_keys=(), links=(), symbol_keys=None):
    """Associate reference signals, live-verify issue keys, and reconcile.

    ``sites`` is ``{site_url: cloudId}`` (from ``connector.discover_sites()``).
    ``symbol_keys`` is ``{symbol_node_id: issue_key}`` (see :mod:`symbol_refs`) --
    per-symbol candidates, merged in alongside the repo-level ones and verified
    in the same batched JQL call, so a symbol's issue key is confirmed/dropped
    exactly like a branch-derived one.
    """
    site_hosts = [h for h in (host_of(u) for u in sites) if h]
    nodes, edges = associate(repo_id, issue_keys=issue_keys, links=links, site_hosts=site_hosts)
    if symbol_keys:
        sym_nodes, sym_edges = associate_symbols(symbol_keys)
        by_id = {n.id: n for n in nodes}
        by_id.update({n.id: n for n in sym_nodes})
        nodes = list(by_id.values())
        edges = edges + sym_edges
    confirmed: dict[str, dict] = {}
    keys = list(dict.fromkeys([*issue_keys, *(symbol_keys or {}).values()]))
    if keys:
        for cloud_id in sites.values():
            confirmed.update(connector.verify_issues(cloud_id, keys))
    return reconcile(nodes, edges, confirmed)
