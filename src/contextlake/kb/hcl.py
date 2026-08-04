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


def _reference_segments(
    var_expr: ts.Node, siblings: list[ts.Node], index: int
) -> tuple[str, list[str]]:
    """From a ``variable_expr`` node, return (root_identifier, [segment, ...]).

    Segments are the consecutive following ``get_attr`` siblings' identifiers,
    stopping at the first non-``get_attr`` sibling (index/splat/operator), so
    ``aws_x.y[0].id`` and ``aws_x.y[*].id`` both yield root ``aws_x``, segs ``[y]``.

    ``siblings``/``index`` are the node's own position in its parent's child
    list, supplied by :func:`_walk_ctx`, because ``Node.next_sibling`` is
    O(depth) per access in py-tree-sitter (it re-descends from the tree root),
    which made this walk O(depth^2) on a deeply nested file. Sliced by index
    rather than by ``siblings[index + 1:]`` so a node with very many siblings
    does not pay a list copy per reference either.
    """
    root = _text(var_expr)
    segs: list[str] = []
    i = index + 1
    while i < len(siblings) and siblings[i].type == "get_attr":
        ident = next((c for c in siblings[i].children if c.type == "identifier"), None)
        if ident is None:
            break
        segs.append(_text(ident))
        i += 1
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


def _is_locals_block(block: ts.Node | None) -> bool:
    if block is None or block.type != "block":
        return False
    kids = block.children
    return bool(kids) and kids[0].type == "identifier" and _text(kids[0]) == "locals"


def _walk_ctx(root: ts.Node):
    """Depth-first walk yielding ``(node, siblings, index, top_block, local_attr)``.

    Visit order is exactly that of a plain ``stack.pop()`` /
    ``stack.extend(children)`` walk, so the order references are emitted in is
    unchanged. What changes is that everything the reference pass needs to know
    about a node's surroundings - its position among its siblings, its
    outermost enclosing ``block``, and the ``locals`` attribute it sits under -
    is carried *down* the traversal instead of being recovered afterwards by
    climbing back up.

    That matters because ``Node.parent`` and ``Node.next_sibling`` are O(depth)
    per access in py-tree-sitter (each re-descends from the tree root), while
    ``Node.children`` is not. Recovering a node's context by climbing therefore
    cost O(depth) per node over O(depth) nodes, making ``parse_hcl`` O(depth^2)
    on a deeply nested ``.tf`` file: 0.29s / 1.18s / 4.87s at nesting depth
    2500 / 5000 / 10000, against 0.034s for the raw tree-sitter parse of the
    deepest of those. The grammar was never the problem.

    ``local_attr`` is the ``attribute`` node directly under a ``locals`` block's
    body that encloses this node, if any - a reference inside one belongs to
    that specific ``local.<attr>``, not to the block. Nested ones simply
    overwrite as the walk descends, so the innermost wins.
    """
    stack: list[tuple[ts.Node, ts.Node | None, list[ts.Node], int,
                      ts.Node | None, ts.Node | None]] = [(root, None, [root], 0, None, None)]
    while stack:
        node, parent, siblings, index, top_block, local_attr = stack.pop()
        if top_block is None and node.type == "block":
            top_block = node
        yield node, siblings, index, top_block, local_attr
        kids = node.children
        if not kids:
            continue
        marks_locals = node.type == "body" and _is_locals_block(parent)
        stack.extend(
            (child, node, kids, i, top_block,
             child if (marks_locals and child.type == "attribute") else local_attr)
            for i, child in enumerate(kids))


def parse_hcl(
    repo_id: str, rel_path: str, source: bytes, verified_at: date | None = None
) -> tuple[list[Node], list[tuple[str, str, str, int]]]:
    """Parse one ``.tf`` file into (definition nodes, unresolved depends_on refs).

    Refs are ``(src_node_id, target_address, rel_path, line)`` - resolved
    repo-wide by :func:`.parse.index_repo_dir`.

    ``verified_at`` is accepted for signature parity with :func:`.parse.parse_source`;
    HCL nodes carry structural provenance (file/line) and resolved edges are
    stamped at resolution time, so the value is unused here.
    """
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

    def _src_id_for(top_block: ts.Node | None, local_attr: ts.Node | None) -> str | None:
        # Both arguments come from _walk_ctx; deriving them here by walking
        # node.parent to the root is what used to make this quadratic.
        if top_block is None:
            return None
        # A ref inside a locals block belongs to its specific local.<attr> node.
        if _is_locals_block(top_block):
            if local_attr is None:
                return None
            name_node = local_attr.child_by_field_name("name") or (
                local_attr.named_child(0) if local_attr.named_child_count else None)
            if name_node is None:
                return None
            return make_id(repo_id, rel_path, f"local.{_text(name_node)}")
        return block_to_id.get(top_block.id)

    seen: set[tuple[str, str]] = set()
    for var_expr, siblings, index, top_block, local_attr in _walk_ctx(tree.root_node):
        if var_expr.type != "variable_expr":
            continue
        root, segs = _reference_segments(var_expr, siblings, index)
        address = _reference_address(root, segs)
        if address is None:
            continue
        src_id = _src_id_for(top_block, local_attr)
        if src_id is None:
            continue
        key = (src_id, address)
        if key in seen:
            continue  # dedup implicit + explicit (depends_on) references
        seen.add(key)
        refs.append((src_id, address, rel_path, var_expr.start_point[0] + 1))

    return nodes, refs
