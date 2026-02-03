"""Orchestrate knowledge-source connectors over indexed repos.

Ties repo-side reference signals (``kb.references``) to a connector's live fetch,
producing a reconciled set of external nodes/edges. Output is stored in a
per-repo partition (``@connect:<repo>``) that is isolated from the code shards, so
re-indexing a repo's code never clobbers its connector links and vice versa.
"""

from __future__ import annotations

from ..model import Confidence
from .atlassian import (
    DEFAULT_MCP_URL,
    AtlassianConnector,
    associate,
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


def enrich_repo_figma(connector, repo_id, *, links=()):
    """Associate figma.com links to design nodes, with best-effort name enrichment."""
    from .figma import associate_designs

    nodes, edges = associate_designs(repo_id, links=links, site_hosts=connector.hosts)
    for n in nodes:
        if n.kind == "design":
            meta = connector.fetch_metadata(n.name)
            title = meta.get("name") if isinstance(meta, dict) else None
            if title:
                n.attrs["title"] = title
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


def enrich_repo(connector, sites, repo_id, *, issue_keys=(), links=()):
    """Associate reference signals, live-verify issue keys, and reconcile.

    ``sites`` is ``{site_url: cloudId}`` (from ``connector.discover_sites()``).
    """
    site_hosts = [h for h in (host_of(u) for u in sites) if h]
    nodes, edges = associate(repo_id, issue_keys=issue_keys, links=links, site_hosts=site_hosts)
    confirmed: dict[str, dict] = {}
    keys = list(issue_keys)
    if keys:
        for cloud_id in sites.values():
            confirmed.update(connector.verify_issues(cloud_id, keys))
    return reconcile(nodes, edges, confirmed)
