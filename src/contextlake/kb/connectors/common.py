"""Generic, connector-agnostic helpers shared by knowledge-source connectors.

URL host matching and the repo/link graph primitives live here so every
connector (Atlassian, Figma, …) builds links the same way. Connector-specific
URL classification and external-node minting stay in each connector module.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

from ..ids import make_id
from ..model import Confidence, Edge, Node, Provenance


def host_of(url: str) -> str | None:
    try:
        return urlparse(url).netloc.lower() or None
    except ValueError:
        return None


def claims(url: str, site_hosts) -> bool:
    """Whether ``url``'s host matches one of ``site_hosts`` (exact or subdomain)."""
    h = host_of(url)
    return bool(h and any(h == s.lower() or h.endswith("." + s.lower()) for s in site_hosts))


def repo_node(repo_id: str) -> Node:
    return Node(id=make_id("repo", repo_id), repo=repo_id, kind="repo", name=repo_id)


def link_edge(repo_id: str, ext: Node, relation: str, source_file: str, *,
              confidence: Confidence = Confidence.INFERRED,
              verified_at: date | None = None) -> Edge:
    """A repo -> external-knowledge edge (e.g. tracked_by / documented_by / designed_in)."""
    return Edge(
        src=make_id("repo", repo_id), dst=ext.id, relation=relation,
        confidence=confidence,
        provenance=Provenance(source_file=source_file, verified_at=verified_at or date.today()),
    )
