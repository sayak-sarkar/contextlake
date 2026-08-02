"""A shared, no-network text-mention matcher: which known code symbols does a
piece of free text (a Slack message, an ingested document, generated wiki
prose) mention by name? Reused by the Slack matcher and by the ingest/enrich/
wiki linking task -- one implementation, not four."""

from __future__ import annotations

import re

from ..embeddings.index import EMBEDDABLE_KINDS
from ..model import Confidence, Node

__all__ = ["match_symbol_mentions"]


def match_symbol_mentions(
    text: str, symbols: list[Node], *, min_name_len: int = 3
) -> list[tuple[str, Confidence]]:
    """(symbol_node_id, Confidence.AMBIGUOUS) for every embeddable symbol whose
    name appears in `text` as a whole-word match. Longest names are checked
    first so a short name can't spuriously match inside a longer mention."""
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
