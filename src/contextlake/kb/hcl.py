"""Terraform / HCL extraction -> infrastructure dependency graph.

HCL is not object-oriented, so it does not use the OO capture model in
:mod:`.parse` (``_DEF_TYPES``/``_QUERIES``). Instead this module walks top-level
``config_file > body > block`` nodes into definition Nodes whose ``name`` is the
full Terraform address (``aws_s3_bucket.logs``, ``var.region``), and reconstructs
interpolation references (``var.x``, ``module.y``, ``type.name``) into
``(src_id, address, file, line)`` tuples that :func:`.parse.index_repo_dir`
resolves repo-wide into ``depends_on`` edges by reusing ``_resolve_name_refs``.

The grammar (``tree-sitter-hcl``) is an optional ``[kb]`` dependency.
"""

from __future__ import annotations

from datetime import date

import tree_sitter as ts

from .ids import make_id
from .model import Node

# Block keywords that introduce a top-level definition. ``locals`` is special:
# its *body attributes* are the units (``local.<attr>``), not the block itself.
_DEF_BLOCKS = {"resource", "data", "variable", "output", "module"}

# Root segments of a reference that are Terraform meta (not references to a
# named block) and are skipped during reference reconstruction.
_META_ROOTS = {"each", "count", "path", "self", "terraform"}

_LANG: ts.Language | None = None
_PARSER: ts.Parser | None = None


def _parser() -> ts.Parser:
    global _LANG, _PARSER
    if _PARSER is None:
        import tree_sitter_hcl as g
        _LANG = ts.Language(g.language())
        _PARSER = ts.Parser(_LANG)
    return _PARSER


def _text(node: ts.Node) -> str:
    return node.text.decode("utf-8", "replace")


def _labels(block: ts.Node) -> list[str]:
    """The ``string_lit`` labels of a block, quotes stripped, in order."""
    return [_text(c).strip().strip('"') for c in block.children if c.type == "string_lit"]


def _block_body(block: ts.Node) -> ts.Node | None:
    for c in block.children:
        if c.type == "body":
            return c
    return None


def _address_for_block(keyword: str, labels: list[str]) -> str | None:
    """The Terraform address used as a def node's ``name`` (None if malformed)."""
    if keyword == "resource" and len(labels) >= 2:
        return f"{labels[0]}.{labels[1]}"
    if keyword == "data" and len(labels) >= 2:
        return f"data.{labels[0]}.{labels[1]}"
    if keyword == "variable" and labels:
        return f"var.{labels[0]}"
    if keyword == "output" and labels:
        return f"output.{labels[0]}"
    if keyword == "module" and labels:
        return f"module.{labels[0]}"
    return None


def _top_level_blocks(root: ts.Node) -> list[ts.Node]:
    """Direct ``config_file > body > block`` children (top level only)."""
    out: list[ts.Node] = []
    for body in root.children:
        if body.type == "body":
            out.extend(c for c in body.children if c.type == "block")
    return out


def _reference_segments(var_expr: ts.Node) -> tuple[str, list[str]]:
    """From a ``variable_expr`` node, return (root_identifier, [segment, ...]).

    Segments are the consecutive following ``get_attr`` siblings' identifiers,
    stopping at the first non-``get_attr`` sibling (index/splat/operator), so
    ``aws_x.y[0].id`` and ``aws_x.y[*].id`` both yield root ``aws_x``, segs ``[y]``.
    """
    root = _text(var_expr)
    segs: list[str] = []
    sib = var_expr.next_sibling
    while sib is not None and sib.type == "get_attr":
        ident = next((c for c in sib.children if c.type == "identifier"), None)
        if ident is None:
            break
        segs.append(_text(ident))
        sib = sib.next_sibling
    return root, segs


def _reference_address(root: str, segs: list[str]) -> str | None:
    """Map (root, segments) to a target Terraform address (None if not a ref)."""
    if root in _META_ROOTS:
        return None
    if root == "var":
        return f"var.{segs[0]}" if segs else None
    if root == "local":
        return f"local.{segs[0]}" if segs else None
    if root == "module":
        return f"module.{segs[0]}" if segs else None
    if root == "data":
        return f"data.{segs[0]}.{segs[1]}" if len(segs) >= 2 else None
    # a resource-type reference: <type>.<name>
    return f"{root}.{segs[0]}" if segs else None


def _walk(node: ts.Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _enclosing_local_attr(node: ts.Node) -> ts.Node | None:
    """The ``attribute`` node directly under a ``locals`` block body, if any."""
    n = node
    while n is not None:
        if n.type == "attribute" and n.parent is not None and n.parent.type == "body" \
                and n.parent.parent is not None and n.parent.parent.type == "block":
            block = n.parent.parent
            kids = block.children
            if kids and kids[0].type == "identifier" and _text(kids[0]) == "locals":
                return n
        n = n.parent
    return None


def parse_hcl(
    repo_id: str, rel_path: str, source: bytes, verified_at: date | None = None
) -> tuple[list[Node], list[tuple[str, str, str, int]]]:
    """Parse one ``.tf`` file into (definition nodes, unresolved depends_on refs).

    Refs are ``(src_node_id, target_address, rel_path, line)`` - resolved
    repo-wide by :func:`.parse.index_repo_dir`.
    """
    verified_at = verified_at or date.today()
    tree = _parser().parse(source)
    nodes: list[Node] = []
    # block ts-node id -> the def node id it maps to (for ref attribution, Task 2)
    block_to_id: dict[int, str] = {}

    for block in _top_level_blocks(tree.root_node):
        kids = block.children
        if not kids or kids[0].type != "identifier":
            continue
        keyword = _text(kids[0])
        line = kids[0].start_point[0] + 1
        line_end = block.end_point[0] + 1
        if keyword in _DEF_BLOCKS:
            address = _address_for_block(keyword, _labels(block))
            if address is None:
                continue
            nid = make_id(repo_id, rel_path, address)
            nodes.append(Node(
                id=nid, repo=repo_id, kind=keyword, name=address,
                qualified_name=f"{rel_path}::{address}", file=rel_path,
                line_start=line, line_end=line_end, lang="hcl",
            ))
            block_to_id[block.id] = nid
        elif keyword == "locals":
            body = _block_body(block)
            if body is None:
                continue
            for attr in body.children:
                if attr.type != "attribute":
                    continue
                name_node = attr.child_by_field_name("name") or (
                    attr.named_child(0) if attr.named_child_count else None)
                if name_node is None:
                    continue
                address = f"local.{_text(name_node)}"
                nid = make_id(repo_id, rel_path, address)
                nodes.append(Node(
                    id=nid, repo=repo_id, kind="local", name=address,
                    qualified_name=f"{rel_path}::{address}", file=rel_path,
                    line_start=attr.start_point[0] + 1,
                    line_end=attr.end_point[0] + 1, lang="hcl",
                ))
        # provider / terraform / backend / moved / import / ... : not a def

    refs: list[tuple[str, str, str, int]] = []

    def _src_id_for(node: ts.Node) -> str | None:
        # A ref inside a locals block belongs to its specific local.<attr> node.
        top_block: ts.Node | None = None
        n = node
        while n is not None:
            if n.type == "block":
                top_block = n
            n = n.parent
        if top_block is None:
            return None
        kids = top_block.children
        if kids and kids[0].type == "identifier" and _text(kids[0]) == "locals":
            attr = _enclosing_local_attr(node)
            if attr is None:
                return None
            name_node = attr.child_by_field_name("name") or (
                attr.named_child(0) if attr.named_child_count else None)
            if name_node is None:
                return None
            return make_id(repo_id, rel_path, f"local.{_text(name_node)}")
        return block_to_id.get(top_block.id)

    seen: set[tuple[str, str]] = set()
    for var_expr in (n for n in _walk(tree.root_node) if n.type == "variable_expr"):
        root, segs = _reference_segments(var_expr)
        address = _reference_address(root, segs)
        if address is None:
            continue
        src_id = _src_id_for(var_expr)
        if src_id is None:
            continue
        key = (src_id, address)
        if key in seen:
            continue  # dedup implicit + explicit (depends_on) references
        seen.add(key)
        refs.append((src_id, address, rel_path, var_expr.start_point[0] + 1))

    return nodes, refs
