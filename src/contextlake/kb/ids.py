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

import hashlib
import re
import unicodedata

__all__ = ["normalize_id", "make_id", "canonical_path", "scoped_id", "file_node_id"]


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


def canonical_path(rel_path: str) -> str:
    """One spelling of a repo-relative path, the same on every OS.

    ``\\`` to ``/``, a leading ``./`` or ``/`` removed, repeated ``/`` merged, NFC. Case is
    KEPT: ``A_B.py`` and ``a_b.py`` are two files. ``make_id`` used to hide any difference in
    how producers spelled a path; an exact id must not split one file into two nodes.
    """
    path = unicodedata.normalize("NFC", rel_path.replace("\\", "/"))
    path = re.sub(r"/{2,}", "/", path)
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def scoped_id(repo_id: str, rel_path: str, *name_parts: str) -> str:
    """The id of a file, or of a name inside a file, in one repository (D05).

    The readable ``make_id`` prefix plus 8 hex of a SHA-256 over the EXACT repo id, path and
    names. ``make_id`` alone casefolds and folds punctuation, so ``a-b.py``, ``a_b.py`` and
    ``A_B.py`` were one node, and repos ``grp/a-b`` and ``grp/a/b`` shared file ids. The
    suffix only has to separate ids whose readable prefix already matches, so 8 hex is
    enough. ``v1`` versions the hash input: changing it changes every id.
    """
    path = canonical_path(rel_path)
    exact = "\x1f".join(["v1", repo_id, path, *name_parts])
    digest = hashlib.sha256(exact.encode("utf-8")).hexdigest()[:8]
    return f"{make_id(repo_id, path, *name_parts)}_{digest}"


def file_node_id(repo_id: str, rel_path: str) -> str:
    """The node id of a file. Every producer of file nodes calls this, so they agree."""
    return scoped_id(repo_id, rel_path)
