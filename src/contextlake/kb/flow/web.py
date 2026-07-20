"""Web-topology flow detection: frontend routes.

Finds, per file, the frontend **routes** a repo defines and emits them as
``route`` graph nodes (embeddable, like ``endpoint``) with a ``file -> route``
edge (``defines_route``). Unlike ``endpoint``/``topic``, a route has no
cross-repo caller/exposer pair, so route node ids are **repo-scoped**
(``make_id(repo_id, "route", norm)``): two apps that both define ``/orders`` are
distinct nodes, not one shared node. There is deliberately no two-hop join.

Framework-targeted, so every edge is ``INFERRED``, a likely undercount never
asserted as ground truth. Covered: **Next.js App Router** page files
(``app/.../page.*`` path convention), **React Router v6 flat JSX**
(``<Route path=...>``), and **Angular** ``Routes`` tables (a tree-sitter AST
walk anchored on the route-table container, so bare ``{path:...}`` config objects
are never mis-read as routes). Still deferred: the React ``createBrowserRouter``
object form, Luigi navigation configs, Angular lazy ``loadChildren`` sub-trees
(the mount path is captured, the child module is not), realtime channels, and
templates/stylesheets. All are skipped, never mis-captured.
"""

from __future__ import annotations

import re
from datetime import date

from ..ids import make_id
from ..model import Confidence, Edge, Node, Provenance

_WEB_LANGS = {"javascript", "typescript", "tsx"}
_ROUTE_REL = "defines_route"

# a path segment that stands for a variable: :id, {id}, [id], [...slug], $id, {}
_DYN_SEG = re.compile(r"^(?::.+|\{.*\}|\[.*\]|\$.+)$")

_NEXT_PAGE = re.compile(r"(?:^|/)page\.[jt]sx?$")

# React Router v6 flat JSX: <Route ... path="..." ...> (self-closing or open tag).
_ROUTE_JSX = re.compile(r"<Route\b[^>]*?\bpath\s*=\s*([\"'])(?P<path>[^\"']+)\1[^>]*?>", re.DOTALL)
# a single simple element={<Name ...}; None when the element is a ternary/wrapper.
_ELEMENT = re.compile(r"\belement\s*=\s*\{\s*<\s*([A-Z][A-Za-z0-9_]*)[\s/>]")

# cheap performance gate: only re-parse TS files that mention Angular routing.
_NG_PREFILTER = re.compile(r"\bRoutes\b|\bRouterModule\b|\bprovideRouter\b")


def _vendored(rel_path: str) -> bool:
    return "node_modules" in rel_path or "module-federation" in rel_path


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


def _ng_pairs(obj) -> dict:
    """``{key: value_node}`` for an object literal's ``pair`` children."""
    out = {}
    for p in obj.named_children:
        if p.type == "pair":
            k = p.child_by_field_name("key")
            if k is not None:
                out[_node_text(k)] = p.child_by_field_name("value")
    return out


def _walk_ng_routes(array_node, prefix: str, out: list) -> None:
    """Recurse an Angular route array, composing child paths onto ``prefix``."""
    for obj in array_node.named_children:
        if obj.type != "object":
            continue
        pairs = _ng_pairs(obj)
        if "redirectTo" in pairs:
            continue  # a redirect renders no component: not a navigable route
        path_node = pairs.get("path")
        # only a static string path is a real route segment; a pathless layout
        # route or a template-literal path adds no segment and must not emit a
        # phantom "/" (but its children still compose onto the parent prefix).
        has_path = path_node is not None and path_node.type == "string"
        seg = ""
        if has_path:
            seg = _node_text(path_node).strip("\"'`")
            if seg == "**":
                seg = "*"  # catch-all -> the splat token, never collides with "/"
        raw = f"{prefix}/{seg}" if seg else prefix
        if has_path:
            comp = pairs.get("component")
            ctx = _node_text(comp) if comp is not None and comp.type == "identifier" else None
            out.append((normalize_route(raw), obj.start_point[0] + 1, ctx))
        children = pairs.get("children")
        if children is not None and children.type == "array":
            _walk_ng_routes(children, raw, out)  # lazy loadChildren is never an array


def _angular_routes(source) -> list:
    """``(route, line, context)`` for every Angular route in a TS file.

    Best-effort: route extraction must never abort the file's indexing, so any
    parse/walk failure yields no routes rather than propagating.
    """
    from .. import parse  # lazy: parse.py imports this module, avoid a cycle
    src_bytes = source if isinstance(source, (bytes, bytearray)) else source.encode("utf-8")
    try:
        tree = parse._parser("typescript").parse(src_bytes)
        out: list = []
        for arr in _ng_route_arrays(tree.root_node):
            _walk_ng_routes(arr, "", out)
        return out
    except Exception:  # noqa: BLE001 - never let a bad parse drop the file's nodes
        return []


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
    # is kept — the endpoint-style genericity guard does not apply here.
    rp = _nextjs_route(rel_path)
    if rp is not None:
        emit(rp, 1)

    if not _vendored(rel_path):
        text = _text(source)
        # React Router v6 flat JSX: <Route path=...> in the source.
        for m in _ROUTE_JSX.finditer(text):
            route = normalize_route(m.group("path"))
            comp = _ELEMENT.search(m.group(0))
            line = text.count("\n", 0, m.start()) + 1
            emit(route, line, comp.group(1) if comp else None)
        # Angular route tables: re-parse (only prefiltered TS files) and walk the AST.
        if lang == "typescript" and _NG_PREFILTER.search(text):
            for route, line, ctx in _angular_routes(source):
                emit(route, line, ctx)

    return nodes, edges
