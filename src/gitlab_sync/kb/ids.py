"""Stable node-ID normalization — the single source of truth for IDs.

Independent producers (AST extractor, manifest parser, connectors, and any LLM
pass) must agree on node IDs, or a single entity splits into disconnected ghost
nodes. Keeping the recipe in one place is what prevents that drift.

Recipe: NFKC-normalize (collapse composed/decomposed Unicode), replace runs of
non-word characters with a single underscore (``re.UNICODE`` so CJK/Cyrillic/
accented-Latin letters survive instead of collapsing to one node), collapse
repeated underscores, strip leading/trailing underscores, and casefold.
Idempotent: ``normalize_id(normalize_id(s)) == normalize_id(s)``.

Adapted from Graphify (https://github.com/safishamsi/graphify), MIT License,
Copyright (c) 2026 Safi Shamsi.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["normalize_id", "make_id"]


def normalize_id(s: str) -> str:
    """Normalize a single ID string to its canonical form (idempotent)."""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s)
    return s.strip("_").casefold()


def make_id(*parts: str) -> str:
    """Build a canonical node ID from one or more name parts.

    Parts are joined with ``_`` (after stripping stray ``_``/``.`` from each part)
    and run through :func:`normalize_id`, so the result matches what a builder
    would produce from the joined string.
    """
    return normalize_id("_".join(p.strip("_.") for p in parts if p))
