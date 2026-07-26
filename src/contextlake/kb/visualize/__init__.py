"""Bounded subgraph extraction + rendering for `contextlake graph`.

Split into styling/payload/diagrams/html_render/serve (see
plan-kb-module-split.md); re-exported here so `from . import visualize as viz`
keeps working exactly as before for every caller (kb/c4.py, kb/commands.py,
kb/dashboard/, and every test)."""

from __future__ import annotations

from .diagrams import (  # noqa: F401
    _CLASSIFIER_KINDS,
    _RESOURCE_CATEGORIES,
    _SEQUENCE_MAX_MESSAGES,
    _cytoscape_elements,
    _dot_escape,
    _mermaid_escape,
    _resource_category,
    to_class_diagram,
    to_deployment_diagram,
    to_dot,
    to_er_diagram,
    to_json,
    to_mermaid,
    to_sequence_diagram,
    to_state_diagram,
)
from .html_render import (  # noqa: F401
    _CDN_URL,
    _HTML_TEMPLATE,
    _INDEX_TEMPLATE,
    _WIKI_TEMPLATE,
    LAYOUTS,
    _app_css,
    _app_js,
    _cytoscape_js,
    _match_repo,
    _md_to_html,
    _read_static_raw,
    _site_index,
    _wiki_page,
    build_site,
    repo_slug,
    to_html,
)
from .payload import (  # noqa: F401
    _edge_dict,
    _is_sentinel_repo,
    _node_dict,
    extract_subgraph,
    overview_subgraph,
    repo_node_sizes,
    repo_subgraph,
    seed_ids_from_args,
    to_payload,
)
from .serve import (  # noqa: F401
    build_graph_server,
    build_site_server,
    serve_graph,
    serve_site,
)
from .styling import (  # noqa: F401
    _BRAND,
    _CONF_DOT,
    _GLYPH_SVG,
    _KIND_ICON_PATHS,
    _LANG_LABELS,
    CONF_META,
    DEFAULT_COLOR,
    DEFAULT_EDGE_COLOR,
    KIND_COLORS,
    RELATION_COLORS,
    _icon_uri,
    _kind_icons,
    _lang_icon,
    _lang_icons,
    _luma,
)
