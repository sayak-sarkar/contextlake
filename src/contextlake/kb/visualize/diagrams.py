"""Text-format diagram renderers: json/dot/mermaid/classdiagram/sequencediagram/statediagram/erdiagram."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .styling import _CONF_DOT, DEFAULT_COLOR, KIND_COLORS

if TYPE_CHECKING:  # avoid importing the model at call time; we only need types here
    pass

def to_json(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _dot_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def to_dot(payload: dict) -> str:
    lines = ["digraph contextlake {", "  rankdir=LR;",
             '  node [style=filled, shape=box, fontname="sans-serif"];']
    idmap: dict[str, str] = {}
    for i, n in enumerate(payload["nodes"]):
        sid = f"n{i}"
        idmap[n["id"]] = sid
        color = KIND_COLORS.get(n.get("kind"), DEFAULT_COLOR)
        label = _dot_escape(n.get("name") or n["id"])
        lines.append(f'  {sid} [label="{label}", fillcolor="{color}", '
                     f'tooltip="{_dot_escape(n.get("kind", ""))}"];')
    for e in payload["edges"]:
        s, d = idmap.get(e["src"]), idmap.get(e["dst"])
        if not s or not d:
            continue
        style = _CONF_DOT.get(e.get("confidence", "EXTRACTED"), "solid")
        lines.append(f'  {s} -> {d} [label="{_dot_escape(e["relation"])}", style={style}];')
    lines.append("}")
    return "\n".join(lines)


def _mermaid_escape(s: str) -> str:
    return (
        (s or "")
        .replace('"', "&quot;")
        .replace("[", "(")
        .replace("]", ")")
        .replace("{", "(")
        .replace("}", ")")
        .replace("|", "/")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def to_mermaid(payload: dict) -> str:
    lines = ["graph LR"]
    idmap: dict[str, str] = {}
    for i, n in enumerate(payload["nodes"]):
        mid = f"n{i}"
        idmap[n["id"]] = mid
        lines.append(f'  {mid}["{_mermaid_escape(n.get("name") or n["id"])}"]')
    for e in payload["edges"]:
        s, d = idmap.get(e["src"]), idmap.get(e["dst"])
        if not s or not d:
            continue
        lines.append(f'  {s} -->|{_mermaid_escape(e["relation"])}| {d}')
    return "\n".join(lines)


# Node kinds that are "classifiers" in a class diagram.


_CLASSIFIER_KINDS = {"class", "interface", "struct", "enum"}


def to_class_diagram(payload: dict) -> str:
    """Render a Mermaid ``classDiagram``: classifier nodes (class/interface/struct/enum)
    with their methods as members, and ``inherits`` edges as inheritance arrows —
    solid ``<|--`` for extends, dotted ``<|..`` when the base is an interface (implements).

    Non-classifier nodes (files) and non-structural edges (calls/imports) are dropped,
    so the output is a focused UML class view rather than the flat relation graph.
    """
    by_id = {n["id"]: n for n in payload["nodes"]}
    classifiers = {nid: n for nid, n in by_id.items()
                   if n.get("kind") in _CLASSIFIER_KINDS}
    if not classifiers:
        return "classDiagram\n  %% no classes/interfaces in this view"

    # methods owned by each classifier, via contains edges (classifier -> method)
    members: dict[str, list[dict]] = {nid: [] for nid in classifiers}
    for e in payload["edges"]:
        if e.get("relation") != "contains":
            continue
        src, dst = e["src"], e["dst"]
        if src in classifiers and (m := by_id.get(dst)) and m.get("kind") in ("method", "function"):
            members[src].append(m)

    alias = {nid: f"c{i}" for i, nid in enumerate(classifiers)}
    lines = ["classDiagram"]
    for nid, node in classifiers.items():
        a = alias[nid]
        stereo = "  <<interface>>\n" if node.get("kind") == "interface" else ""
        label = _mermaid_escape(node.get("name") or nid)
        mem = members[nid]
        if stereo or mem:
            body = stereo + "".join(f"    +{_mermaid_escape(m.get('name') or '?')}"
                                    f"{_mermaid_escape(m.get('signature') or '()')}\n"
                                    for m in mem)
            lines.append(f'  class {a}["{label}"] {{\n{body}  }}')
        else:
            lines.append(f'  class {a}["{label}"]')

    for e in payload["edges"]:
        if e.get("relation") != "inherits":
            continue
        sub, base = e["src"], e["dst"]
        if sub in alias and base in alias:
            # base <|-- sub (extends); base <|.. sub (implements an interface)
            arrow = "<|.." if by_id[base].get("kind") == "interface" else "<|--"
            lines.append(f"  {alias[base]} {arrow} {alias[sub]}")
    return "\n".join(lines)


_SEQUENCE_MAX_MESSAGES = 200


def to_sequence_diagram(payload: dict, *, max_messages: int = _SEQUENCE_MAX_MESSAGES) -> str:
    """Render a Mermaid ``sequenceDiagram`` walking outgoing ``calls`` edges from the
    view's single seed node, depth-first, each caller's callees ordered by call-site
    line (``prov_line``) -- the order they actually appear in the source.

    Requires a view with exactly one seed (``graph --node/--name/--search``, not
    ``--repo``/``--overview``): a sequence diagram needs one unambiguous starting
    actor -- there's no single obvious ordering across multiple unrelated seeds, so
    this doesn't guess one. Only ``calls`` edges already present in the view are
    walked, so depth is governed by the view's own ``--hops``; there's no separate
    depth control here. A node already on the current call path is not re-entered
    (breaks cycles/recursion without dropping the rest of the diagram); the walk is
    capped at ``max_messages`` with a truncation note appended -- never silent.
    """
    seeds = (payload.get("meta") or {}).get("seed_ids") or []
    if len(seeds) != 1:
        return ("sequenceDiagram\n  %% needs exactly one seed node "
                "(graph --node/--name/--search, not --repo/--overview)")
    seed = seeds[0]
    by_id = {n["id"]: n for n in payload["nodes"]}
    if seed not in by_id:
        return "sequenceDiagram\n  %% seed node not present in this view"

    calls_from: dict[str, list[dict]] = {}
    for e in payload["edges"]:
        if e.get("relation") == "calls" and e["src"] in by_id and e["dst"] in by_id:
            calls_from.setdefault(e["src"], []).append(e)
    for lst in calls_from.values():
        lst.sort(key=lambda e: (e.get("prov_line") is None, e.get("prov_line")))

    alias: dict[str, str] = {}

    def _alias(nid: str) -> str:
        if nid not in alias:
            alias[nid] = f"p{len(alias)}"
        return alias[nid]

    _alias(seed)
    messages: list[tuple[str, str, str]] = []  # (src_alias, dst_alias, label)
    truncated = False

    def walk(node_id: str, path: frozenset[str]) -> None:
        nonlocal truncated
        for e in calls_from.get(node_id, []):
            if len(messages) >= max_messages:
                truncated = True
                return
            dst = e["dst"]
            label = _mermaid_escape(by_id[dst].get("name") or dst)
            messages.append((_alias(node_id), _alias(dst), label))
            if dst not in path:
                walk(dst, path | {dst})

    walk(seed, frozenset({seed}))

    if not messages:
        return "sequenceDiagram\n  %% no outgoing calls from this seed in view"

    lines = ["sequenceDiagram"]
    for nid, a in alias.items():
        lines.append(f'  participant {a} as {_mermaid_escape(by_id[nid].get("name") or nid)}')
    for s, d, label in messages:
        lines.append(f"  {s}->>{d}: {label}()")
    if truncated:
        lines.append(f"  %% truncated at {max_messages} messages")
    return "\n".join(lines)


def to_state_diagram(payload: dict) -> str:
    """Render a Mermaid ``stateDiagram-v2`` from ``state`` nodes and
    ``transitions_to`` edges (see :mod:`kb.flow.state` for the regex/AST-light,
    guard-inferred extraction — every edge here is a high-confidence transition
    the source code actually establishes, never a synthetic ``*`` start state).

    Nodes are grouped by entity (the qualified name's ``Entity.Value`` prefix)
    into one composite ``state Entity { ... }`` block each; a single-entity view
    renders flat. A state value the code never transitions to/from (only ever
    read, never assigned) still appears, unconnected — it's a known value, just
    not one this extractor saw the code move to.

    Best fed a ``--repo`` view (like ``classdiagram``): a ``--name``/``--node``
    seed's BFS reaches state nodes via the file that declares them (a
    ``contains`` edge, 2 hops from a class-named seed) but stops before a 3rd
    hop would traverse *their own* ``transitions_to`` edges, so a seeded view
    can show the states without their transitions. ``--repo``'s induced subgraph
    has no such hop limit.
    """
    by_id = {n["id"]: n for n in payload["nodes"] if n.get("kind") == "state"}
    if not by_id:
        return "stateDiagram-v2\n  %% no state transitions in this view"

    def entity_of(n: dict) -> str:
        qn = n.get("qualified_name") or ""
        return qn.rsplit(".", 1)[0] if "." in qn else "?"

    by_entity: dict[str, list[str]] = {}
    for nid, n in by_id.items():
        by_entity.setdefault(entity_of(n), []).append(nid)

    trans_by_entity: dict[str, list[tuple[str, str, str]]] = {e: [] for e in by_entity}
    for e in payload["edges"]:
        if e.get("relation") != "transitions_to":
            continue
        src, dst = e.get("src"), e.get("dst")
        if src not in by_id or dst not in by_id:
            continue
        entity = entity_of(by_id[src])
        trans_by_entity.setdefault(entity, []).append(
            (by_id[src]["name"], by_id[dst]["name"], e.get("context") or "")
        )

    single = len(by_entity) == 1
    lines = ["stateDiagram-v2"]
    for entity, state_ids in by_entity.items():
        body = [f"    {_mermaid_escape(frm)} --> {_mermaid_escape(to)} : {_mermaid_escape(label)}"
                for frm, to, label in trans_by_entity.get(entity, [])]
        mentioned = {s for frm, to, _ in trans_by_entity.get(entity, []) for s in (frm, to)}
        body.extend(f"    {_mermaid_escape(by_id[nid]['name'])}"
                    for nid in state_ids if by_id[nid]["name"] not in mentioned)
        if single:
            lines.extend(body)
        else:
            lines.append(f"  state {_mermaid_escape(entity)} {{")
            lines.extend(f"  {line}" for line in body)
            lines.append("  }")
    return "\n".join(lines)


def to_er_diagram(payload: dict) -> str:
    """Render a Mermaid ``erDiagram`` from ``table``/``view`` nodes and
    ``references`` (FK) edges (see :mod:`kb.sql` for the regex DDL extraction --
    every edge here is ``INFERRED``, a likely undercount, never asserted as
    ground truth).

    No attribute blocks: the extractor captures ``CREATE TABLE``/``VIEW`` names
    and FK targets, not column lists, so this shows entities and relationships
    only. Table/view names come straight from ``kb.sql``'s ``_NAME`` regex
    (``[A-Za-z_]\\w*``), always Mermaid-identifier-safe, so unlike the other
    diagram formats this needs no ``_mermaid_escape`` on the identifiers.

    ORM-defined schemas (SQLAlchemy / Entity Framework / TypeORM model classes,
    no raw ``.sql`` DDL) produce nothing here -- this format only sees literal
    ``CREATE TABLE``/``VIEW`` text, never ORM model classes, so a typical
    ORM-only repo view is an honest empty diagram, not a bug.
    """
    by_id = {n["id"]: n for n in payload["nodes"] if n.get("kind") in ("table", "view")}
    if not by_id:
        return ("erDiagram\n"
                "  %% no table/view definitions in this view -- contextlake's SQL extractor\n"
                "  %% reads raw CREATE TABLE/VIEW DDL, not ORM model classes (SQLAlchemy/\n"
                "  %% Entity Framework/TypeORM); point --repo at a repo with .sql files")

    lines = ["erDiagram"]
    seen: set[tuple[str, str]] = set()
    for e in payload["edges"]:
        if e.get("relation") != "references":
            continue
        src, dst = e.get("src"), e.get("dst")
        if src not in by_id or dst not in by_id or (src, dst) in seen:
            continue
        seen.add((src, dst))
        # A REFERENCES clause always points from the child (many rows) to the
        # parent it names (one row) -- FK semantics, not a guess -- so the
        # one/many cardinality here is asserted, unlike the edge's own
        # INFERRED confidence (which is about whether the reference exists).
        child, parent = by_id[src]["name"], by_id[dst]["name"]
        lines.append(f"  {parent} ||--o{{ {child} : references")

    mentioned = {n for pair in seen for n in pair}
    for nid, n in by_id.items():
        if nid not in mentioned:
            lines.append(f"  {n['name']}")
    return "\n".join(lines)


def _cytoscape_elements(payload: dict) -> list[dict]:
    # Undirected degree per node, excluding self-loops -- mirrors app.js's own
    # n.degree(false) exactly, computed once here so every node's `deg` is present
    # from the first style pass (the "width"/"height" mapData(deg, ...) rule applies
    # to every node). Without it, app.js only sets `deg` in a .forEach() AFTER the
    # graph is first styled, so cytoscape logs a console warning for every node on
    # initial render (harmless, but noisy) before silently correcting itself.
    degree: dict[str, int] = {}
    for e in payload["edges"]:
        src, dst = e["src"], e["dst"]
        if src == dst:
            continue
        degree[src] = degree.get(src, 0) + 1
        degree[dst] = degree.get(dst, 0) + 1

    els = []
    for n in payload["nodes"]:
        attrs = n.get("attrs") or {}
        data = {
            "id": n["id"], "label": n.get("name") or n["id"], "kind": n.get("kind", ""),
            "repo": n.get("repo", ""), "qn": n.get("qualified_name") or "",
            "file": n.get("file") or "", "line": n.get("line"),
            "count": attrs.get("node_count"), "href": n.get("href") or "",
            "lang": n.get("lang") or "", "deg": degree.get(n["id"], 0),
        }
        parent = n.get("parent") or ""
        if parent:
            data["parent"] = parent
        els.append({"data": data})
    for e in payload["edges"]:
        els.append({"data": {"source": e["src"], "target": e["dst"],
                             "relation": e.get("relation", ""),
                             "confidence": e.get("confidence", "EXTRACTED"),
                             "context": e.get("context") or "",
                             "weight": e.get("weight", 1.0),  # always present -> mapData safe
                             "prov_file": e.get("prov_file") or "",
                             "prov_line": e.get("prov_line"),
                             "verified_at": e.get("verified_at") or ""}})
    return els


# Coarse resource-type category, for visual grouping only -- a heuristic keyword
# match over common cloud-provider naming conventions (AWS/Azure/GCP today).
# Never asserted as ground truth the way the underlying resource/depends_on
# data is; an unrecognized type lands in "other", never silently dropped.
#
# Order matters: checked top to bottom, first match wins. database/storage/
# security are listed before compute/network because their keywords are more
# specific -- "instance" (compute) is a substring of "db_instance" (database),
# so a generic "instance" match would wrongly claim database resources first
# if compute were checked earlier. Verified live: aws_db_instance.db must land
# in "database", not "compute".
_RESOURCE_CATEGORIES = [
    ("database", ("rds", "db_", "dynamodb", "redis", "elasticache", "sql",
                  "cosmos", "database", "cache")),
    ("storage", ("s3", "bucket", "ebs", "efs", "disk", "storage", "blob", "volume")),
    ("security", ("security_group", "iam", "policy", "role", "kms", "acl",
                  "secret", "certificate")),
    ("network", ("vpc", "subnet", "route", "gateway", "nat", "eip", "network",
                "vnet", "firewall", "dns", "zone")),
    ("compute", ("instance", "ec2", "lambda", "ecs", "eks", "fargate", "function",
                "vm", "compute", "container", "task", "cluster", "app_service")),
]


def _resource_category(type_name: str) -> str:
    low = type_name.lower()
    for category, keywords in _RESOURCE_CATEGORIES:
        if any(kw in low for kw in keywords):
            return category
    return "other"


def to_deployment_diagram(payload: dict) -> str:
    """Render a Mermaid flowchart of Terraform/HCL resources grouped by a coarse
    inferred category (network/compute/storage/database/security/other/module),
    from ``kb/hcl.py``'s already-extracted ``resource``/``data``/``module`` nodes
    and ``depends_on`` edges -- no new extraction pass, same spirit as
    ``to_er_diagram`` over the SQL DDL extractor's data.

    Category is a heuristic keyword match over the resource type prefix (e.g.
    ``aws_security_group.web`` -> security), for visual grouping only.

    Terraform-only today (HCL is the only IaC language ``kb/hcl.py`` parses);
    a repo with no ``.tf`` files renders an honest empty diagram with guidance,
    not a bug.
    """
    by_id = {n["id"]: n for n in payload["nodes"]
            if n.get("kind") in ("resource", "data", "module")}
    if not by_id:
        return ("graph TD\n"
                "  %% no Terraform resource/data/module definitions in this view --\n"
                "  %% contextlake's HCL extractor reads .tf files; point --repo at a\n"
                "  %% repo with Terraform configuration")

    def category_of(n: dict) -> str:
        if n["kind"] == "module":
            return "module"
        name = n.get("name") or ""
        # A `data` block's address is `data.<type>.<name>` (kb/hcl.py's
        # _address_for_block), so the type prefix is the *second* dot-segment,
        # not the first ("data" itself never matches any keyword).
        if n["kind"] == "data" and name.startswith("data."):
            name = name[len("data."):]
        return _resource_category(name.split(".", 1)[0])

    by_category: dict[str, list[str]] = {}
    for nid, n in by_id.items():
        by_category.setdefault(category_of(n), []).append(nid)

    alias = {nid: f"n{i}" for i, nid in enumerate(sorted(by_id))}
    single = len(by_category) == 1
    lines = ["graph TD"]
    for category, nids in sorted(by_category.items()):
        node_lines = [f'    {alias[nid]}["{_mermaid_escape(by_id[nid]["name"])}"]'
                     for nid in sorted(nids)]
        if single:
            lines.extend(node_lines)
        else:
            lines.append(f"  subgraph {_mermaid_escape(category)}")
            lines.extend(node_lines)
            lines.append("  end")

    seen: set[tuple[str, str]] = set()
    for e in payload["edges"]:
        if e.get("relation") != "depends_on":
            continue
        src, dst = e.get("src"), e.get("dst")
        if src not in by_id or dst not in by_id or (src, dst) in seen:
            continue
        seen.add((src, dst))
        lines.append(f"  {alias[src]} --> {alias[dst]}")
    return "\n".join(lines)

