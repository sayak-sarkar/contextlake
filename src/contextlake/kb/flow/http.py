"""HTTP/REST flow detection.

Finds, per file, the HTTP endpoints a repo **exposes** (server routes) and the
ones it **calls** (HTTP-client requests), as edges to a shared ``endpoint`` node
keyed by a *normalised path* so a caller in one repo and the exposer in another
land on the same node. The cross-repo join (``exposes ⨝ calls_http``) lives in
:mod:`..arch.resolve`.

Regex-based and framework-targeted (ASP.NET / Express / FastAPI·Flask), so every
edge is ``INFERRED`` — a likely undercount, never asserted as ground truth.
Paths are normalised (host/query stripped, params → ``{}``) and trivially-generic
paths (no real word segment) are dropped so unrelated repos don't falsely link.
"""

from __future__ import annotations

import re
from datetime import date

from ..ids import make_id
from ..model import Confidence, Edge, Node, Provenance

# language (from LANG_BY_EXT) -> framework family
_FAMILY = {"python": "py", "javascript": "js", "typescript": "js",
           "tsx": "js", "csharp": "cs"}

# (regex, method: group-index|literal|None, path-group)
_EXPOSE: dict[str, list[tuple[re.Pattern, object, int]]] = {
    "py": [(re.compile(r"@\w+\.(get|post|put|delete|patch|route)\(\s*['\"]([^'\"]+)['\"]"), 1, 2)],
    "js": [(re.compile(r"\b(?:app|router|api|server)\.(get|post|put|delete|patch)\("
                       r"\s*['\"`]([^'\"`]+)['\"`]"), 1, 2)],
    "cs": [(re.compile(r"\.Map(Get|Post|Put|Delete|Patch)\(\s*[@$]?\"([^\"]+)\""), 1, 2),
           (re.compile(r"\[Http(Get|Post|Put|Delete|Patch)\(\s*\"([^\"]+)\"\s*\)\]"), 1, 2),
           (re.compile(r"\[Route\(\s*\"([^\"]+)\"\s*\)\]"), "*", 1)],
}
_CALL: dict[str, list[tuple[re.Pattern, object, int]]] = {
    "py": [(re.compile(r"\b(?:requests|httpx|session|client|http)\.(get|post|put|delete|patch)\("
                       r"\s*['\"]([^'\"]+)['\"]"), 1, 2)],
    "js": [(re.compile(r"\b(?:axios|http|client|api)\.(get|post|put|delete|patch)\("
                       r"\s*['\"`]([^'\"`]+)['\"`]"), 1, 2),
           (re.compile(r"\bfetch\(\s*['\"`]([^'\"`]+)['\"`]"), "*", 1)],
    "cs": [(re.compile(r"\.(Get|Post|Put|Delete|Patch)(?:Async)?(?:<[^>]+>)?\("
                       r"\s*[@$]?\"([^\"]+)\""), 1, 2)],
}


def _is_param(seg: str) -> bool:
    return (seg.startswith(("{", ":", "$")) or "${" in seg or seg.isdigit()
            or bool(re.fullmatch(r"[0-9a-fA-F]{8,}|[0-9a-fA-F-]{16,}", seg)))


def normalize_path(raw: str) -> str:
    """Strip scheme/host/query and collapse path params to ``{}`` for matching."""
    p = re.sub(r"^https?://[^/]+", "", raw.strip().strip("'\"`"))
    p = re.sub(r"[?#].*$", "", p)
    segs = ["{}" if _is_param(s) else s for s in p.split("/") if s]
    return "/" + "/".join(segs)


def _useful(norm: str) -> bool:
    # require at least one real (>=2 char, non-param) segment so '/', '/{}' etc.
    # — which would match almost anything — never become shared endpoints
    return any(len(s) >= 2 and s != "{}" for s in norm.split("/"))


def extract_http_flow(repo_id: str, rel_path: str, source, lang: str,
                      verified_at: date | None = None) -> tuple[list[Node], list[Edge]]:
    """Endpoint nodes + ``exposes`` / ``calls_http`` edges for one file."""
    fam = _FAMILY.get(lang)
    if not fam:
        return [], []
    text = source.decode("utf-8", "replace") if isinstance(source, (bytes, bytearray)) else source
    verified_at = verified_at or date.today()
    file_id = make_id(repo_id, rel_path)
    nodes: list[Node] = []
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()

    def scan(patterns, relation):
        for rx, mspec, pgrp in patterns:
            for m in rx.finditer(text):
                path = m.group(pgrp)
                if relation == "calls_http" and "/" not in path:
                    continue
                norm = normalize_path(path)
                if not _useful(norm):
                    continue
                ep_id = make_id("endpoint", norm)
                if (relation, ep_id) in seen:
                    continue
                seen.add((relation, ep_id))
                method = (m.group(mspec) if isinstance(mspec, int) else mspec) or "*"
                nodes.append(Node(id=ep_id, repo=repo_id, kind="endpoint",
                                  name=norm, qualified_name=norm))
                edges.append(Edge(
                    src=file_id, dst=ep_id, relation=relation,
                    confidence=Confidence.INFERRED, context=method.upper(),
                    provenance=Provenance(source_file=rel_path,
                                          source_line=text.count("\n", 0, m.start()) + 1,
                                          verified_at=verified_at)))

    scan(_EXPOSE[fam], "exposes")
    scan(_CALL[fam], "calls_http")
    return nodes, edges
