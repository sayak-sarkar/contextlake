"""Styling vocabulary: kind/relation colours, confidence dots, inline icon SVGs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:  # avoid importing the model at call time; we only need types here
    pass

KIND_COLORS = {
    "file": "#8ecae6", "module": "#ffb703", "class": "#fb8500", "interface": "#fd9e02",
    "struct": "#f4a261", "function": "#90be6d", "method": "#43aa8b", "enum": "#577590",
    "package": "#e76f51", "repo": "#264653", "issue": "#bc6c25", "page": "#606c38",
    "design": "#9d4edd",
    # flow nodes (from the HTTP / event extractors) — a service surface, not a symbol
    "endpoint": "#f08c3a", "topic": "#b07fd0",
    # C4 namespace boundary compound parent node (contextlake/kb/c4.py c4_payload)
    "namespace": "#3d5a80",
    # C1 external-system box (kb/c4.py C4System) — deliberately muted/neutral:
    # unclassified, could be a real third party or just an unindexed internal
    # service, so it should never read as confidently "external" via color alone.
    "system": "#6c757d",
}


DEFAULT_COLOR = "#c9c9c9"
# Relation -> edge hue (within the brand family; greys for structural relations).
# Open vocabulary: unknown relations fall back to DEFAULT_EDGE_COLOR.


RELATION_COLORS = {
    "calls": "#137A8B", "imports": "#2BB3A3", "contains": "#9fb4b8",
    "depends_on": "#E7B53C", "publishes": "#D7C5A0", "tracked_by": "#577590",
    "documented_by": "#9d4edd", "flow": "#e5571f", "exposes": "#f08c3a",
    "calls_http": "#c1440e",
}


DEFAULT_EDGE_COLOR = "#aecace"


_CONF_DOT = {"EXTRACTED": "solid", "INFERRED": "dashed", "AMBIGUOUS": "dotted"}
# Confidence -> human label + trust dot, surfaced in the edge inspector.


CONF_META = {
    "EXTRACTED": ("Extracted", "#2BB3A3", "Direct from source (AST / manifest)"),
    "INFERRED": ("Inferred", "#E7B53C", "Deduced (second-pass / heuristic)"),
    "AMBIGUOUS": ("Ambiguous", "#e76f51", "Uncertain (flagged for review)"),
}


_BRAND = {"deepwater": "#0E2A33", "lake": "#137A8B", "current": "#2BB3A3",
          "mist": "#EAF4F4", "shore": "#D7C5A0", "sun": "#E7B53C"}
# The brand glyph, inlined so the page stays self-contained/offline.


_GLYPH_SVG = (
    '<img class="glyph" src="data:image/png;base64,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"'
    ' alt="contextlake" width="30" height="30"/>'
)

# Per-kind glyphs (inner SVG paths, Lucide-style line icons) painted onto nodes so a
# diagram reads by *type* at a glance — a file vs a service vs an HTTP endpoint. Kept
# as bare 24x24 path content; the stroke colour is chosen per node at build time
# (_kind_icons) for contrast, and the data-URI is inlined so the page stays offline.


_KIND_ICON_PATHS = {
    "file": '<path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12'
            'a2 2 0 0 0 2-2V8z"/>',
    "page": '<path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12'
            'a2 2 0 0 0 2-2V8z"/><line x1="8" y1="13" x2="15" y2="13"/>'
            '<line x1="8" y1="17" x2="15" y2="17"/>',
    "module": '<path d="M12 2 2 7l10 5 10-5z"/><path d="M2 17l10 5 10-5"/>'
              '<path d="M2 12l10 5 10-5"/>',
    "class": '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8'
             'v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>',
    "struct": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" '
              'height="7"/><rect x="14" y="14" width="7" height="7"/>'
              '<rect x="3" y="14" width="7" height="7"/>',
    "interface": '<path d="M8 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h1"/>'
                 '<path d="M16 3h1a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-1"/>',
    "enum": '<circle cx="4" cy="6" r="1.3"/><circle cx="4" cy="12" r="1.3"/>'
            '<circle cx="4" cy="18" r="1.3"/><line x1="9" y1="6" x2="21" y2="6"/>'
            '<line x1="9" y1="12" x2="21" y2="12"/><line x1="9" y1="18" x2="21" y2="18"/>',
    "function": '<polyline points="8 7 3 12 8 17"/><polyline points="16 7 21 12 16 17"/>',
    "method": '<polyline points="8 7 3 12 8 17"/><polyline points="16 7 21 12 16 17"/>',
    "package": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8'
               'a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>'
               '<path d="M3.3 7 12 12l8.7-5"/><line x1="12" y1="22" x2="12" y2="12"/>',
    "repo": '<rect x="3" y="4" width="18" height="7" rx="1.5"/>'
            '<rect x="3" y="13" width="18" height="7" rx="1.5"/>'
            '<line x1="7" y1="7.5" x2="7.01" y2="7.5"/>'
            '<line x1="7" y1="16.5" x2="7.01" y2="16.5"/>',
    "issue": '<circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/>'
             '<line x1="12" y1="16" x2="12.01" y2="16"/>',
    "design": '<path d="M12 3l1.9 5.1L19 11l-5.1 1.9L12 18l-1.9-5L5 11l5.1-1.9z"/>',
    "endpoint": '<circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/>'
                '<path d="M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z"/>',
    "topic": '<path d="M21 14a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
}


def _luma(hex_color: str) -> float:
    """Perceived brightness (0-255) of a #rrggbb colour — picks glyph contrast."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b


def _icon_uri(inner: str, stroke: str) -> str:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        'viewBox="0 0 24 24" fill="none" stroke="' + stroke + '" stroke-width="2.2" '
        'stroke-linecap="round" stroke-linejoin="round">' + inner + '</svg>'
    )
    return "data:image/svg+xml;utf8," + quote(svg, safe="")


def _kind_icons() -> dict:
    """kind -> data-URI glyph, stroke coloured for contrast against the node fill."""
    out = {}
    for kind, inner in _KIND_ICON_PATHS.items():
        color = KIND_COLORS.get(kind, DEFAULT_COLOR)
        stroke = "#0E2A33" if _luma(color) > 150 else "#ffffff"
        out[kind] = _icon_uri(inner, stroke)
    return out


# Primary-language lettermark for repo nodes, so the fleet diagram shows its tech
# stack at a glance. Keys are the parser's lang ids; unknown languages keep the
# generic repo glyph. White text reads on the dark navy repo fill.


_LANG_LABELS = {
    "python": "PY", "javascript": "JS", "typescript": "TS", "tsx": "TS",
    "csharp": "C#", "c_sharp": "C#", "java": "JV", "go": "GO", "ruby": "RB",
    "rust": "RS", "php": "PHP", "kotlin": "KT", "cpp": "C++", "c": "C",
}


def _lang_icon(label: str) -> str:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        'viewBox="0 0 24 24"><text x="12" y="16" text-anchor="middle" '
        'font-family="ui-sans-serif,system-ui,sans-serif" font-size="10" '
        'font-weight="700" fill="#ffffff">' + label + '</text></svg>'
    )
    return "data:image/svg+xml;utf8," + quote(svg, safe="")


def _lang_icons() -> dict:
    """lang id -> data-URI lettermark glyph (overlaid on repo nodes)."""
    return {lang: _lang_icon(label) for lang, label in _LANG_LABELS.items()}


# ---------------------------------------------------------------------------
# Canonical serialization (one shape reused by json / html / the /neighbors API)
# ---------------------------------------------------------------------------

