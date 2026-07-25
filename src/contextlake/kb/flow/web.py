"""Web-topology flow detection: frontend routes.

Finds, per file, the frontend **routes** a repo defines and emits them as
``route`` graph nodes (embeddable, like ``endpoint``) with a ``file -> route``
edge (``defines_route``). Unlike ``endpoint``/``topic``, a route has no
cross-repo caller/exposer pair, so route node ids are **repo-scoped**
(``make_id(repo_id, "route", norm)``): two apps that both define ``/orders`` are
distinct nodes, not one shared node. There is deliberately no two-hop join.

Framework-targeted, so every edge is ``INFERRED``, a likely undercount never
asserted as ground truth. Covered: **Next.js App Router** page files
(``app/.../page.*`` path convention), **React Router** both the v6 flat JSX
(``<Route path=...>``) and the data-router object form
(``createBrowserRouter([{ path, Component, children, index }])``), and **Angular**
``Routes`` tables. The object-literal forms use a tree-sitter AST walk anchored on
the route-table container (a ``Routes``-typed declarator, or the array argument to
``forRoot``/``provideRouter``/``create*Router``), so bare ``{path:...}`` config
objects are never mis-read as routes. Still deferred: Luigi navigation configs,
Angular lazy ``loadChildren`` sub-trees (the mount path is captured, the child
module is not), React ``loader``/``lazy``/``createRoutesFromElements``, realtime
channels, and templates/stylesheets. All are skipped, never mis-captured.
"""

from __future__ import annotations

import re
from datetime import date

from ..ids import make_id
from ..model import Confidence, Edge, Node, Provenance
from .data import strip_code_noise

_WEB_LANGS = {"javascript", "typescript", "tsx"}
_ROUTE_REL = "defines_route"

# a path segment that stands for a variable: :id, {id}, [id], [...slug], $id, {}
_DYN_SEG = re.compile(r"^(?::.+|\{.*\}|\[.*\]|\$.+)$")

_NEXT_PAGE = re.compile(r"(?:^|/)page\.[jt]sx?$")

# React Router v6 flat JSX: a <Route> tag, either self-closing or an opening
# tag paired with a later </Route> -- the pairing is what lets a nested
# <Route>'s relative `path` compose onto its enclosing <Route>'s, the same way
# _walk_route_objects composes the object-literal form's `children` (a nested
# route only ever resolves under its parent in React Router v6).
_ROUTE_OPEN = re.compile(r"<Route\b")
_ROUTE_CLOSE = re.compile(r"</Route\s*>")
_PATH_ATTR = re.compile(r"\bpath\s*=\s*([\"'])(?P<path>[^\"']+)\1")
# a single simple element={<Name ...}; None when the element is a ternary/wrapper.
_ELEMENT = re.compile(r"\belement\s*=\s*\{\s*<\s*([A-Z][A-Za-z0-9_]*)[\s/>]")


def _scan_tag_end(text: str, start: int) -> tuple[str, bool, int] | None:
    """From ``start`` (just after ``<Route``), find the tag's true end.

    A naive ``[^>]*?>`` lazy scan stops at the FIRST ``>`` it finds -- but a
    ``{...}`` attribute-expression value (``element={<Home/>}``) can contain
    its own nested JSX with its own ``/>``, which is not this tag's own close.
    Tracks ``{}`` depth (and skips over quoted strings, so a quoted ``{``/``}``
    can't desync it) so only a top-level ``>`` ends the tag. Returns
    ``(attrs, self_closing, index_after_tag)``, or ``None`` if the tag never
    closes (truncated/malformed source -- give up rather than guess).
    """
    depth = 0
    quote: str | None = None
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if quote:
            if c == quote and text[i - 1] != "\\":
                quote = None
        elif c in ("\"", "'"):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and c == ">":
            if i > start and text[i - 1] == "/":
                return text[start:i - 1], True, i + 1
            return text[start:i], False, i + 1
        i += 1
    return None


# cheap performance gates: only re-parse files that mention the routing API.
_NG_PREFILTER = re.compile(r"\bRoutes\b|\bRouterModule\b|\bprovideRouter\b")
_REACT_OBJ_PREFILTER = re.compile(r"\bcreate(?:Browser|Hash|Memory)Router\b")


def _vendored(rel_path: str) -> bool:
    return "node_modules" in rel_path or "module-federation" in rel_path


def _jsx_flat_routes(text: str) -> list[tuple[str, int, str | None]]:
    """``(route, line, context)`` for React Router v6 flat-JSX ``<Route>``
    elements, composing a nested ``<Route>``'s relative ``path`` onto its
    enclosing ``<Route>``'s (never emitting a nested route as an absolute
    root-level one it isn't). Comments and string literals are blanked out
    first via :func:`strip_code_noise` -- a ``{/* <Route .../> */}`` used to
    disable a route, or an unrelated ``// <Route .../>`` line comment, is not
    a registered route. Known residual gap, by design (stripping generic
    string literals would also strip legitimate ``path="..."`` attribute
    values, which are string literals too): JSX-shaped text sitting inside an
    unrelated plain string (e.g. example prose in a help-text constant) can
    still be matched.
    """
    text = strip_code_noise(text)
    out: list[tuple[str, int, str | None]] = []
    stack: list[str] = [""]  # composed prefixes; index 0 is the file root
    pos, n = 0, len(text)
    while pos < n:
        om = _ROUTE_OPEN.search(text, pos)
        cm = _ROUTE_CLOSE.search(text, pos)
        if om and (not cm or om.start() < cm.start()):
            scanned = _scan_tag_end(text, om.end())
            if scanned is None:
                break  # tag never closes -- truncated/malformed, stop rather than guess
            attrs, self_closing, end = scanned
            prefix = stack[-1]
            path_m = _PATH_ATTR.search(attrs)
            if path_m:
                raw = f"{prefix}/{path_m.group('path')}" if prefix else path_m.group("path")
                comp = _ELEMENT.search(attrs)
                line = text.count("\n", 0, om.start()) + 1
                out.append((normalize_route(raw), line, comp.group(1) if comp else None))
                child_prefix = raw
            else:
                child_prefix = prefix  # pathless layout <Route element={<Layout/>}>: no segment
            if not self_closing:
                stack.append(child_prefix)
            pos = end
        elif cm:
            if len(stack) > 1:
                stack.pop()
            pos = cm.end()
        else:
            break
    return out


def _is_route_param(seg: str) -> bool:
    return seg == "{}" or bool(_DYN_SEG.match(seg))


def normalize_route(raw: str) -> str:
    """Strip query/hash and collapse dynamic segments to ``{}``."""
    p = re.sub(r"[?#].*$", "", raw.strip().strip("'\"`"))
    segs = ["{}" if _is_route_param(s) else s for s in p.split("/") if s]
    return "/" + "/".join(segs)


def _text(source) -> str:
    return source.decode("utf-8", "replace") if isinstance(source, (bytes, bytearray)) else source


def _route_id(repo_id: str, route: str) -> str:
    """Repo-scoped id that preserves path structure.

    ``make_id`` collapses every non-word run to ``_``, so passing the whole path
    ("/orders/{}") would merge distinct routes ("/orders" and "/orders/{}") into
    one id. Feeding each segment as its own word part keeps depth and param
    positions distinct; the dynamic ``{}`` and catch-all ``*`` map to separate
    reserved tokens so "/" vs "/*" vs "/{}" stay distinct. (Two routes whose
    literal segments differ only by slash-vs-underscore, or that literally use a
    reserved word, can still share an id; that is rare and always repo-local.)
    """
    tokens = {"{}": "param", "*": "splat"}
    segs = [tokens.get(s, s) for s in route.split("/") if s]
    return make_id(repo_id, "route", *segs)


def _nextjs_app_root(parts: list[str]) -> int | None:
    """Index of the App Router root: the first ``app`` segment at the repo root
    or directly under ``src`` (``app/`` or ``src/app/``).

    Anchoring on the first such segment (not the last) keeps real routes like
    ``/app`` or ``/settings/app`` from collapsing to ``/``; the ``src`` rule keeps
    a monorepo package literally named ``app`` (``packages/app/src/app/...``) from
    being mistaken for the router root.
    """
    for i, seg in enumerate(parts):
        if seg == "app" and (i == 0 or parts[i - 1] == "src"):
            return i
    return None


def nextjs_url(rel_path: str, file_re: re.Pattern) -> str | None:
    """URL path for a Next.js App Router file matching ``file_re``, else None.

    Segments under the ``app/`` router root become the URL: route groups
    ``(name)`` contribute no segment, dynamic ``[x]``/``[...x]`` collapse to
    ``{}``. Shared by the page-route extractor here and the API-route-handler
    endpoint detection in :mod:`.http`.
    """
    if _vendored(rel_path):
        return None
    if not file_re.search(rel_path):
        return None
    parts = rel_path.split("/")
    root = _nextjs_app_root(parts)
    if root is None:
        return None
    out: list[str] = []
    for s in parts[root + 1:-1]:  # segments under app/, excluding the terminal file
        if s.startswith("(") and s.endswith(")"):
            continue  # route group: no URL segment
        out.append("{}" if _is_route_param(s) else s)
    return "/" + "/".join(out)


def _nextjs_route(rel_path: str) -> str | None:
    """Route path for a Next.js App Router ``page.*`` file, else None."""
    return nextjs_url(rel_path, _NEXT_PAGE)


# --- Angular route tables (tree-sitter AST) --------------------------------
# The correctness anchor is the route-table *container*, never the object shape:
# a bare ``{path: ...}`` object (build config, HTTP options) is not a route. A
# route array is only the value of a ``Routes``-typed declarator, or an inline
# array argument to forRoot/forChild/provideRouter.

def _node_text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _ta_is_routes(ta) -> bool:
    """True if a ``type_annotation`` is ``Routes`` or ``Route[]``."""
    for c in ta.named_children:
        if c.type == "type_identifier" and _node_text(c) == "Routes":
            return True
        if c.type == "array_type" and any(
                cc.type == "type_identifier" and _node_text(cc) == "Route"
                for cc in c.named_children):
            return True
    return False


def _ng_route_arrays(root) -> list:
    """Every AST ``array`` node that is an Angular route table (both anchors).

    Iterative (not recursive) so a deeply nested literal cannot ``RecursionError``
    and drop the whole file's indexing.
    """
    arrays = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "variable_declarator":
            ta = node.child_by_field_name("type")
            val = node.child_by_field_name("value")
            if ta is not None and val is not None and val.type == "array" and _ta_is_routes(ta):
                arrays.append(val)
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            # anchor on the router API specifically (RouterModule.forRoot/forChild,
            # provideRouter) so a StoreModule.forRoot([...]) or similar never matches
            if fn is not None and args is not None and _node_text(fn) in (
                    "provideRouter", "RouterModule.forRoot", "RouterModule.forChild") \
                    and args.named_child_count and args.named_children[0].type == "array":
                arrays.append(args.named_children[0])
        stack.extend(node.children)
    return arrays


def _obj_pairs(obj) -> dict:
    """``{key: value_node}`` for an object literal's ``pair`` children."""
    out = {}
    for p in obj.named_children:
        if p.type == "pair":
            k = p.child_by_field_name("key")
            if k is not None:
                out[_node_text(k)] = p.child_by_field_name("value")
    return out


def _component_ctx(pairs, *keys) -> str | None:
    """The route's component name, only when a plain ``identifier`` (not JSX)."""
    for k in keys:
        v = pairs.get(k)
        if v is not None and v.type == "identifier":
            return _node_text(v)
    return None


def _ng_segment(pairs):
    """``(emit, seg, ctx)`` for an Angular route object."""
    if "redirectTo" in pairs:
        return False, "", None  # a redirect renders no component: not a route
    path_node = pairs.get("path")
    if path_node is not None and path_node.type == "string":
        seg = _node_text(path_node).strip("\"'`")
        seg = "*" if seg == "**" else seg  # catch-all -> splat, never collides with "/"
        return True, seg, _component_ctx(pairs, "component")
    return False, "", None  # pathless layout / template path: skip but recurse


def _react_segment(pairs):
    """``(emit, seg, ctx)`` for a React data-router object."""
    path_node = pairs.get("path")
    if path_node is not None and path_node.type == "string":
        seg = _node_text(path_node).strip("\"'`")
        return True, seg, _component_ctx(pairs, "Component", "element")
    index_node = pairs.get("index")
    if index_node is not None and index_node.type == "true":
        # index route: resolves to the PARENT path (no segment). Emit, do not vanish.
        return True, "", _component_ctx(pairs, "Component", "element")
    return False, "", None  # pathless/indexless layout: skip but recurse


def _walk_route_objects(array_node, prefix: str, out: list, segment_of) -> None:
    """Recurse a route array, composing child paths onto ``prefix``.

    ``segment_of(pairs) -> (emit, seg, ctx)`` is the per-framework decision; the
    recursion, prefix composition, and normalize are shared.
    """
    for obj in array_node.named_children:
        if obj.type != "object":
            continue
        pairs = _obj_pairs(obj)
        emit, seg, ctx = segment_of(pairs)
        raw = f"{prefix}/{seg}" if seg else prefix
        if emit:
            out.append((normalize_route(raw), obj.start_point[0] + 1, ctx))
        children = pairs.get("children")
        if children is not None and children.type == "array":
            _walk_route_objects(children, raw, out, segment_of)


def _react_object_arrays(root) -> list:
    """Every ``array`` argument to a ``create{Browser,Hash,Memory}Router`` call."""
    arrays = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn is not None and args is not None and _node_text(fn) in (
                    "createBrowserRouter", "createHashRouter", "createMemoryRouter") \
                    and args.named_child_count and args.named_children[0].type == "array":
                arrays.append(args.named_children[0])
        stack.extend(node.children)
    return arrays


def _ast_routes(source, lang, arrays_of, segment_of) -> list:
    """``(route, line, context)`` via a tree-sitter walk of ``source``.

    Best-effort: route extraction must never abort the file's indexing, so any
    parse/walk failure yields no routes rather than propagating.
    """
    from .. import parse  # lazy: parse.py imports this module, avoid a cycle
    src_bytes = source if isinstance(source, (bytes, bytearray)) else source.encode("utf-8")
    try:
        tree = parse._parser(lang).parse(src_bytes)
        out: list = []
        for arr in arrays_of(tree.root_node):
            _walk_route_objects(arr, "", out, segment_of)
        return out
    except Exception:  # noqa: BLE001 - never let a bad parse drop the file's nodes
        return []


def _angular_routes(source) -> list:
    """``(route, line, context)`` for every Angular route in a TS file."""
    return _ast_routes(source, "typescript", _ng_route_arrays, _ng_segment)


def _react_object_routes(source, lang) -> list:
    """``(route, line, context)`` for every React data-router route in a file."""
    return _ast_routes(source, lang, _react_object_arrays, _react_segment)


def extract_web_flow(repo_id: str, rel_path: str, source, lang: str,
                     verified_at: date | None = None) -> tuple[list[Node], list[Edge]]:
    """``route`` nodes + ``defines_route`` edges for one file."""
    if lang not in _WEB_LANGS:
        return [], []
    verified_at = verified_at or date.today()
    file_id = make_id(repo_id, rel_path)
    nodes: list[Node] = []
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()

    def emit(route: str, line: int, context: str | None = None) -> None:
        rid = _route_id(repo_id, route)
        if (_ROUTE_REL, rid) in seen:
            return
        seen.add((_ROUTE_REL, rid))
        nodes.append(Node(id=rid, repo=repo_id, kind="route",
                          name=route, qualified_name=route, file=rel_path))
        edges.append(Edge(
            src=file_id, dst=rid, relation=_ROUTE_REL,
            confidence=Confidence.INFERRED, context=context,
            provenance=Provenance(source_file=rel_path, source_line=line,
                                  verified_at=verified_at)))

    # Next.js App Router: derived from the file path, no source needed. Routes
    # are repo-scoped (no cross-repo join), so every explicitly-declared route
    # is kept — the endpoint-style generalization does not apply here.
    rp = _nextjs_route(rel_path)
    if rp is not None:
        emit(rp, 1)

    if not _vendored(rel_path):
        text = _text(source)
        # React Router v6 flat JSX: <Route path=...> in the source, nested
        # <Route>s composed onto their enclosing one.
        for route, line, ctx in _jsx_flat_routes(text):
            emit(route, line, ctx)
        # Angular route tables: re-parse (only prefiltered TS files) and walk the AST.
        if lang == "typescript" and _NG_PREFILTER.search(text):
            for route, line, ctx in _angular_routes(source):
                emit(route, line, ctx)
        # React data-router object form: createBrowserRouter([...]) in any web lang.
        if _REACT_OBJ_PREFILTER.search(text):
            for route, line, ctx in _react_object_routes(source, lang):
                emit(route, line, ctx)

    return nodes, edges
