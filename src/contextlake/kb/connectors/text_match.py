"""A shared, no-network text-mention matcher: which known code symbols does a
piece of free text (a Slack message, an ingested document, generated wiki
prose) mention by name? Reused by the Slack matcher and by the ingest/enrich/
wiki linking task -- one implementation, not four.

Also home to the two pieces every one of those callers needs alongside the
matcher itself: looking a repo's symbol nodes up out of the store
(:func:`symbol_nodes_for_repo`) and turning the matches into edges
(:func:`link_documents_to_symbols`)."""

from __future__ import annotations

import re

from ..embeddings.index import EMBEDDABLE_KINDS
from ..model import Confidence, Edge, Node

__all__ = ["link_documents_to_symbols", "match_symbol_mentions", "symbol_nodes_for_repo"]


def match_symbol_mentions(
    text: str, symbols: list[Node], *, min_name_len: int = 3
) -> list[tuple[str, Confidence]]:
    """(symbol_node_id, Confidence.AMBIGUOUS) for every embeddable symbol whose
    name appears in `text` as a whole-word match. Whole-word `\\b` matching
    prevents substring false-positives (e.g. `charge` won't match inside
    `charge_order`); the longest-name-first sort only fixes the order of the
    returned list -- it does not suppress two genuinely distinct,
    overlapping-name symbols (e.g. `charge` and `chargeOrder`) from both
    matching if both are present in the text."""
    candidates = [
        s for s in symbols
        if s.kind in EMBEDDABLE_KINDS and s.name and len(s.name) >= min_name_len
    ]
    candidates.sort(key=lambda s: len(s.name), reverse=True)
    seen: set[str] = set()
    matches: list[tuple[str, Confidence]] = []
    for sym in candidates:
        if sym.id in seen:
            continue
        pattern = r"\b" + re.escape(sym.name) + r"\b"
        if re.search(pattern, text):
            matches.append((sym.id, Confidence.AMBIGUOUS))
            seen.add(sym.id)
    return matches


def symbol_nodes_for_repo(store, repo_id: str) -> list[Node]:
    """``repo_id``'s semantically-meaningful symbol nodes (``EMBEDDABLE_KINDS``),
    for text-mention matching against free text (Slack messages, ingested
    documents, enrichment results, generated wiki prose).

    The ``Store`` ABC has no "all nodes for a repo" scan (see
    ``figma.match_frame_names_to_symbols`` and ``visualize.payload.repo_subgraph``
    for the same gap), so this drops to the same raw-SQL escape hatch those
    already use. Unlike Figma's name-only lookup, :func:`match_symbol_mentions`
    needs real ``Node`` objects -- but only ever reads ``.id``/``.kind``/``.name``, so
    those three columns are selected directly (no ``store.get_node`` N+1 -- a repo can
    carry thousands of symbols, and this runs on every text-linked pipeline, not
    just once at index time).

    Empty for a repo id the store has never indexed -- which is exactly what a
    synthetic partition key (``@ingest:cli``, a cluster namespace, a module page's
    ``repo::prefix``) is, so those callers degrade to "no links" without needing
    their own guard.
    """
    kind_placeholders = ",".join("?" * len(EMBEDDABLE_KINDS))
    rows = store.conn.execute(
        f"""
        SELECT node_id, kind, name FROM nodes
        WHERE repo_id = ? AND kind IN ({kind_placeholders})
        ORDER BY node_id
        """,
        (repo_id, *EMBEDDABLE_KINDS),
    ).fetchall()
    return [Node(id=row[0], repo=repo_id, kind=row[1], name=row[2]) for row in rows]


def link_documents_to_symbols(store, repo_id: str | None, nodes: list[Node],
                              texts: list[str], relation: str,
                              source_file: str, *,
                              repo_fallback: bool = True) -> list[Edge]:
    """Edges from ``repo_id``'s code symbols to every document node whose own text
    mentions them by name -- the shared body of the ingest / enrich / wiki
    "link this prose to the code it talks about" step.

    ``nodes``/``texts`` are the parallel document-node and document-body lists
    each of those pipelines already builds. Direction follows
    :func:`common.link_to_code`: an edge runs *from* the matched code symbol *to*
    the document (``documented_by``).

    ``repo_fallback`` controls that function's repo-level fallback edge
    (``repo_<id> -> document``), which attaches a matching document to the repo
    as a whole. It is on for ingest and enrich, whose documents are genuinely
    third-party knowledge *about* the repo, and off for the wiki -- the repo-level
    edge is what the "external knowledge" surfaces read (``get_repo_links``,
    the dashboard's ``_links_for``), and contextlake's own generated wiki prose
    is not external knowledge, so listing it there mislabels our own output as a
    third-party cross-link. The wiki's symbol-side edges are unaffected: "where
    is this function explained?" is still a graph hop.

    Returns no edges at all -- not even the repo-level fallback -- when
    ``repo_id`` is absent or names nothing indexed (see
    :func:`symbol_nodes_for_repo`); a pipeline with no real target repo must stay
    exactly as edge-free as it was before. The symbol lookup is a full per-repo
    scan, so it happens once per call, never once per document.
    """
    from .common import edge_from, link_to_code

    symbols = symbol_nodes_for_repo(store, repo_id) if repo_id else []
    if not symbols:
        return []
    edges: list[Edge] = []
    for node, text in zip(nodes, texts):
        matches = match_symbol_mentions(text or "", symbols)
        if not matches:
            continue
        if repo_fallback:
            edges.extend(link_to_code(repo_id, node, matches, relation, source_file))
        else:
            edges.extend(
                edge_from(code_id, node, relation, source_file, confidence=confidence)
                for code_id, confidence in matches
            )
    return edges
