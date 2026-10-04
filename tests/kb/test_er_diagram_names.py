"""The ER diagram writes every table name as a valid Mermaid entity name.

The SQL extractor reads quoted names, so `[Order Details]` reaches the diagram as
`order details`. Written as it is, Mermaid reads two tokens and the whole diagram fails
to render, where before the extractor change it showed a wrongly named table.
"""

import re

from contextlake.kb.visualize.diagrams import to_er_diagram

_ENTITY = re.compile(r"^[\w-]+$")


def _payload():
    return {
        "nodes": [
            {"id": "t1", "kind": "table", "name": "order details"},
            {"id": "t2", "kind": "table", "name": "shipments"},
            {"id": "t3", "kind": "table", "name": "line-items"},
            {"id": "t4", "kind": "view", "name": "v quoted"},
        ],
        "edges": [{"src": "t1", "dst": "t2", "relation": "references"}],
    }


def test_every_entity_name_is_a_single_mermaid_token():
    out = to_er_diagram(_payload())
    lines = out.splitlines()[1:]
    assert "  shipments ||--o{ order_details : references" in lines
    for line in lines:
        rel = re.fullmatch(r"\s+(\S+) \|\|--o\{ (\S+) : references", line)
        names = rel.groups() if rel else (line.strip(),)
        for name in names:
            assert _ENTITY.match(name), f"not a single Mermaid entity token: {line!r}"
    assert "  line-items" in lines and "  v_quoted" in lines
