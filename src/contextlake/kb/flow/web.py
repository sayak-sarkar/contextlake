"""Web-topology flow detection: frontend routes.

Finds, per file, the frontend **routes** a repo defines and emits them as
``route`` graph nodes (embeddable, like ``endpoint``) with a ``file -> route``
edge (``defines_route``). Unlike ``endpoint``/``topic``, a route has no
cross-repo caller/exposer pair, so route node ids are **repo-scoped**
(``make_id(repo_id, "route", norm)``): two apps that both define ``/orders`` are
distinct nodes, not one shared node. There is deliberately no two-hop join.

Regex/convention-based and framework-targeted, so every edge is ``INFERRED`` — a
likely undercount, never asserted as ground truth. This phase covers the two
shapes a real-fleet survey found need no AST: **Next.js App Router** page files
(``app/.../page.*`` path convention) and **React Router v6 flat JSX**
(``<Route path=...>``). Angular ``Routes`` arrays, the ``createBrowserRouter``
object form, and Luigi navigation configs need brace-aware/AST parsing and are
handled in a later phase; here they are skipped, never mis-captured.
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


def _is_route_param(seg: str) -> bool:
    return seg == "{}" or bool(_DYN_SEG.match(seg))


def normalize_route(raw: str) -> str:
    """Strip query/hash and collapse dynamic segments to ``{}``."""
    p = re.sub(r"[?#].*$", "", raw.strip().strip("'\"`"))
    segs = ["{}" if _is_route_param(s) else s for s in p.split("/") if s]
    return "/" + "/".join(segs)


def _useful(norm: str) -> bool:
    # require at least one real (>=2 char, non-param) segment so '/{}' etc.
    # — which would match almost anything — never become routes. The app root
    # '/' is allowed by the caller as an explicit, meaningful page.
    return any(len(s) >= 2 and s != "{}" for s in norm.split("/"))


def _nextjs_route(rel_path: str) -> str | None:
    """Route path for a Next.js App Router ``page.*`` file, else None.

    Segments after the ``app/`` router root become the URL: route groups
    ``(name)`` contribute no segment, dynamic ``[x]``/``[...x]`` collapse to
    ``{}``. ``route.*`` API handlers are not pages and are skipped this phase.
    """
    if "node_modules" in rel_path or "module-federation" in rel_path:
        return None
    if not _NEXT_PAGE.search(rel_path):
        return None
    parts = rel_path.split("/")
    if "app" not in parts:
        return None
    app_i = len(parts) - 1 - parts[::-1].index("app")
    out: list[str] = []
    for s in parts[app_i + 1:-1]:  # segments under app/, excluding the page.* file
        if s.startswith("(") and s.endswith(")"):
            continue  # route group: no URL segment
        out.append("{}" if _is_route_param(s) else s)
    return "/" + "/".join(out)


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
        rid = make_id(repo_id, "route", route)
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

    # Next.js App Router: derived from the file path, no source needed.
    rp = _nextjs_route(rel_path)
    if rp is not None and (rp == "/" or _useful(rp)):
        emit(rp, 1)

    return nodes, edges
