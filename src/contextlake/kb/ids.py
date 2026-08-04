r"""Stable node-ID normalization — the single source of truth for IDs.

Independent producers (AST extractor, manifest parser, connectors, and any LLM
pass) must agree on node IDs, or a single entity splits into disconnected ghost
nodes. Keeping the recipe in one place is what prevents that drift.

Recipe: NFKC-normalize (collapse composed/decomposed Unicode), casefold,
replace runs of non-word characters with a single underscore (``re.UNICODE`` so
CJK/Cyrillic/accented-Latin letters survive instead of collapsing to one node),
collapse repeated underscores, and strip leading/trailing underscores.
Idempotent: ``normalize_id(normalize_id(s)) == normalize_id(s)``.

Casefolding *before* the punctuation strip is what makes that idempotence claim
true. Full case folding can expand one character into a base letter plus a
combining mark (U+0130 LATIN CAPITAL LETTER I WITH DOT ABOVE folds to ``i`` +
U+0307 COMBINING DOT ABOVE), and a combining mark is not ``\w``. With the strip
running first, that mark survived the call that created it and was only removed
by a *second* call - so ``normalize_id`` disagreed with itself. Verified by
brute force over every Unicode code point: with this order there is no code
point for which one pass and two passes differ.

Adapted from Graphify (https://github.com/safishamsi/graphify), MIT License,
Copyright (c) 2026 Safi Shamsi.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["normalize_id", "make_id"]


def normalize_id(s: str) -> str:
    """Normalize a single ID string to its canonical form (idempotent)."""
    # casefold() BEFORE the strip: a fold that expands into a base letter plus a
    # combining mark must still face the non-word strip, or the mark leaks into
    # the id and only a second call removes it (see module docstring).
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def make_id(*parts: str) -> str:
    """Build a canonical node ID from one or more name parts.

    Parts are joined with ``_`` (after stripping stray ``_``/``.`` from each part)
    and run through :func:`normalize_id`, so the result matches what a builder
    would produce from the joined string.
    """
    return normalize_id("_".join(p.strip("_.") for p in parts if p))
