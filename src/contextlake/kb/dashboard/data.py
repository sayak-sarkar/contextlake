"""Pure, JSON-able data functions backing the dashboard.

Every function here reuses the exact logic behind an existing MCP tool (see
``kb/server.py``) so the dashboard surface and the agent surface never drift:

* :func:`fleet_overview` / :func:`derive_groups` — ``graph_stats`` + ``list_repos``.
* :func:`repo_detail` — ``get_repo_brief`` + ``get_readme`` + ``get_wiki`` +
  ``who_knows`` (``ownership.compute_owners``) + ``get_repo_links``.
* :func:`repo_relationships` — ``repo_dependencies`` / ``repo_flow`` /
  ``repo_event_flow`` (``arch.resolve``).
* :func:`impact` — ``blast_radius`` (``impact.blast_radius``).
* :func:`health` — ``graph_health`` (``commands.lint_result``).
* :func:`code_search` — ``search_code`` (``store.search``).

All text is passed through ``sanitize_label`` (as the MCP boundary does) so hostile
repo content can't inject into a browser. README / wiki Markdown is rendered to
sanitized HTML *server-side* by reusing ``visualize._md_to_html`` — no client-side
markdown, no new dependency.

The ``anonymize`` option hashes git-author identities ("Contributor a1b2") and strips
external link URLs, for a shareable ``--site`` export. It also DROPS the rendered
README/wiki prose entirely: ``_md_to_html`` promotes ``[text](https://…)`` to live
anchors, so author names / emails / internal URLs in free-text prose would otherwise
land verbatim in the export. Anonymized exports therefore carry structured-anonymized
facts only (anatomy, hashed owners, link kinds) — no README/wiki body.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..security import sanitize_label

# Relations that ``get_repo_links`` groups as external cross-links.
_LINK_RELS = {"tracked_by", "documented_by", "designed_in", "has_merge_request", "has_issue"}


def _conf(e) -> str:
    return e.confidence.value if hasattr(e.confidence, "value") else str(e.confidence)


def _store_dir(store, store_dir=None) -> Path:
    """The store's parent dir (where ``graph/`` shards + ``wiki/`` live)."""
    if store_dir is not None:
        return Path(store_dir)
    sp = getattr(store, "path", None)
    return Path(sp).parent if sp else Path(".")


def _anon_author(name: str | None, email: str | None) -> str:
    """A stable, non-reversible pseudonym for a git author (anonymized exports)."""
    h = hashlib.sha256((email or name or "").encode("utf-8")).hexdigest()[:4]
    return f"Contributor {h}"


# ---------------------------------------------------------------------------
# Fleet overview + domain grouping
# ---------------------------------------------------------------------------
def derive_groups(repo_ids, depth: int = 1) -> list[dict]:
    """Heuristic domain grouping from repo-id path prefixes (split on ``/``).

    A repo ``a/b/c`` at ``depth=1`` groups under ``a``; at ``depth=2`` under ``a/b``.
    A repo with no namespace beyond ``depth`` segments falls into ``(ungrouped)``.
    Mirrors the namespace bucketing in ``visualize._site_index`` — a starting point,
    not a true ownership map.
    """
    depth = max(1, int(depth))
    groups: dict[str, list[str]] = {}
    for r in repo_ids:
        parts = r.split("/")
        key = "/".join(parts[:depth]) if len(parts) > depth else "(ungrouped)"
        groups.setdefault(key, []).append(r)
    return [{"group": sanitize_label(k), "count": len(v),
             "repos": [sanitize_label(x) for x in sorted(v)]}
            for k, v in sorted(groups.items())]


def fleet_overview(store, group_depth: int = 1) -> dict:
    """Fleet stats + the domain-grouped repo grid (reuses ``stats`` + ``list_repos``).

    Languages per repo come from a single ``GROUP BY repo_id, lang`` pass (the
    ``overview_subgraph`` pattern), not a per-repo shard read.
    """
    st = store.stats()
    counts = dict(store.conn.execute(
        "SELECT repo_id, COUNT(*) FROM nodes GROUP BY repo_id").fetchall())
    langs_by_repo: dict[str, dict[str, int]] = {}
    for repo, lang, cnt in store.conn.execute(
            "SELECT repo_id, lang, COUNT(*) FROM nodes "
            "WHERE lang IS NOT NULL GROUP BY repo_id, lang").fetchall():
        langs_by_repo.setdefault(repo, {})[lang] = int(cnt)

    rows = store.conn.execute(
        "SELECT repo_id, default_branch, head_commit, indexed_at FROM repos "
        "ORDER BY repo_id").fetchall()
    depth = max(1, int(group_depth))
    repos = []
    repo_ids = []
    for r in rows:
        rid = r["repo_id"]
        repo_ids.append(rid)
        parts = rid.split("/")
        group = "/".join(parts[:depth]) if len(parts) > depth else "(ungrouped)"
        langs = dict(sorted(langs_by_repo.get(rid, {}).items(), key=lambda kv: -kv[1]))
        repos.append({
            "id": sanitize_label(rid),
            "group": sanitize_label(group),
            "node_count": int(counts.get(rid, 0)),
            "head_commit": sanitize_label(r["head_commit"]) if r["head_commit"] else None,
            "default_branch": r["default_branch"],
            "indexed_at": r["indexed_at"],
            "langs": langs,
        })
    return {
        "stats": {"repos": st.repos, "nodes": st.nodes, "edges": st.edges,
                  "by_confidence": st.by_confidence},
        "repos": repos,
        "groups": derive_groups(repo_ids, depth=depth),
    }


# ---------------------------------------------------------------------------
# Per-repo detail (anatomy + README + wiki + owners + links)
# ---------------------------------------------------------------------------
def _brief_out(brief: dict | None) -> dict | None:
    """Sanitize a ``wiki.generate.repo_brief`` dict for the browser (mirrors
    ``get_repo_brief``)."""
    if not brief:
        return None
    return {
        "repo": sanitize_label(brief["repo"]),
        "head": sanitize_label(brief["head"]) if brief.get("head") else None,
        "node_count": brief["node_count"],
        "edge_count": brief["edge_count"],
        "kinds": brief["kinds"],
        "langs": brief["langs"],
        "top_symbols": [{
            "kind": sanitize_label(t["kind"]),
            "name": sanitize_label(t["name"]),
            "file": sanitize_label(t["file"]) if t.get("file") else None,
            "signature": sanitize_label(t["signature"]) if t.get("signature") else None,
            "doc": sanitize_label(t["doc"]) if t.get("doc") else None,
        } for t in brief["top_symbols"]],
        "packages": [sanitize_label(p) for p in brief["packages"]],
        "files": [sanitize_label(f) for f in brief["files"]],
    }


def _owners_for(store, repo_id: str, *, anonymize: bool = False, limit: int = 10) -> list[dict]:
    """Recency-weighted owners/SMEs (reuses ``ownership.compute_owners``)."""
    from ..ownership import compute_owners

    r = store.get_repo(repo_id)
    if not r or not getattr(r, "path", None):
        return []
    owners = compute_owners(r.path, limit=max(1, min(limit, 50)))
    out = []
    for o in owners:
        name = _anon_author(o.name, o.email) if anonymize else sanitize_label(o.name)
        out.append({"name": name, "commits": o.commits, "lines": o.lines,
                    "last_active": o.last_active, "share": round(o.share, 4)})
    return out


def _safe_url(url):
    """Only allow web/mail schemes into hrefs — blocks ``javascript:``/``data:`` XSS
    from untrusted connector data (Jira/Figma/GitLab titles + URLs)."""
    if isinstance(url, str) and url.lower().startswith(("http://", "https://", "mailto:")):
        return url
    return None


def _links_for(store, repo_id: str, *, anonymize: bool = False) -> dict:
    """External cross-links grouped by relation (reuses ``get_repo_links`` logic)."""
    from ..ids import make_id

    grouped: dict[str, list[dict]] = {}
    for e in store.neighbors(make_id("repo", repo_id), direction="out"):
        if e.relation not in _LINK_RELS:
            continue
        n = store.get_node(e.dst)
        if not n:
            continue
        attrs = getattr(n, "attrs", None) or {}
        title = attrs.get("title") or attrs.get("summary")
        url = None if anonymize else _safe_url(attrs.get("url"))
        grouped.setdefault(e.relation, []).append({
            "kind": sanitize_label(n.kind),
            "name": sanitize_label(n.name),
            "url": sanitize_label(url) if url else None,
            "title": sanitize_label(title) if title else None,
            "status": sanitize_label(attrs["status"]) if attrs.get("status") else None,
            "confidence": _conf(e),
        })
    return grouped


def _readme_html(store, repo_id: str) -> str | None:
    """Render the repo's own README to sanitized HTML (reuses ``get_readme`` +
    ``visualize._md_to_html``)."""
    from ..visualize import _md_to_html

    r = store.get_repo(repo_id)
    base = Path(r.path) if r and getattr(r, "path", None) else None
    if base and base.is_dir():
        for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
            f = base / name
            if f.is_file():
                raw = f.read_text(encoding="utf-8", errors="replace")
                return _md_to_html(sanitize_label(raw, max_len=200_000))
    return None


def _wiki_out(store, store_dir: Path, repo_id: str) -> dict:
    """The generated wiki page rendered to sanitized HTML, with the staleness flag
    (reuses ``get_wiki`` logic + ``visualize._md_to_html`` / ``repo_slug``)."""
    from ..visualize import _md_to_html, repo_slug

    wiki_file = store_dir / "wiki" / (repo_slug(repo_id) + ".md")
    if not wiki_file.exists():
        return {"found": False, "stale": True, "html": None}
    raw = wiki_file.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"at commit `([^`]+)`", raw)
    wiki_commit = m.group(1) if m else None
    r = store.get_repo(repo_id)
    current = r.head_commit if r else None
    stale = wiki_commit is None or current is None or wiki_commit != current
    return {"found": True, "stale": stale,
            "html": _md_to_html(sanitize_label(raw, max_len=200_000))}


def repo_detail(store, store_dir, repo_id: str, *, anonymize: bool = False) -> dict:
    """A repo's full detail panel: anatomy brief, rendered README + wiki, owners, links.

    README/wiki are rendered to sanitized HTML server-side. ``anonymize`` hashes author
    identities, drops external link URLs, and DROPS the README/wiki prose bodies (which
    can carry author names / internal URLs as live anchors) — keeping only the wiki
    ``found`` / ``stale`` flags.
    """
    from ..wiki.generate import repo_brief

    sd = _store_dir(store, store_dir)
    wiki = _wiki_out(store, sd, repo_id)
    if anonymize:
        # Drop free-text prose: keep the wiki flags but no body, and no README HTML.
        readme_html = None
        wiki = {"found": wiki["found"], "stale": wiki["stale"], "html": None}
    else:
        readme_html = _readme_html(store, repo_id)
    return {
        "repo": sanitize_label(repo_id),
        "brief": _brief_out(repo_brief(sd, repo_id)),
        "readme_html": readme_html,
        "wiki": wiki,
        "owners": _owners_for(store, repo_id, anonymize=anonymize),
        "links": _links_for(store, repo_id, anonymize=anonymize),
    }


# ---------------------------------------------------------------------------
# Relationships / impact / health / search
# ---------------------------------------------------------------------------
def repo_relationships(store, repo_id: str) -> dict:
    """Repo->repo dependency, HTTP-flow and event-flow edges touching ``repo_id``.

    Reuses ``arch.resolve.repo_dependency_edges`` / ``repo_http_flow_edges`` /
    ``repo_event_flow_edges`` (all INFERRED, manifest/regex-derived undercounts).
    """
    from ..arch.resolve import (
        repo_dependency_edges,
        repo_event_flow_edges,
        repo_http_flow_edges,
    )

    def _norm(edges):
        return [{
            "src": sanitize_label(e["src"]),
            "dst": sanitize_label(e["dst"]),
            "relation": e["relation"],
            "confidence": e["confidence"],
            "weight": e.get("weight"),
            "context": e.get("context"),
        } for e in edges if e["src"] == repo_id or e["dst"] == repo_id]

    return {
        "dependencies": _norm(repo_dependency_edges(store)),
        "http_flow": _norm(repo_http_flow_edges(store)),
        "event_flow": _norm(repo_event_flow_edges(store)),
    }


def repo_relationships_bulk(store, repo_ids) -> dict:
    """``repo_relationships`` for many repos with THREE edge scans total, not three per
    repo. ``repo_relationships`` rescans every edge each call, so building a snapshot for
    hundreds of repos one-by-one is O(repos x edges); this buckets a single scan by repo.
    """
    from ..arch.resolve import (
        repo_dependency_edges,
        repo_event_flow_edges,
        repo_http_flow_edges,
    )

    ids = set(repo_ids)
    out = {rid: {"dependencies": [], "http_flow": [], "event_flow": []} for rid in repo_ids}

    def _bucket(edges, key):
        for e in edges:
            src, dst = e["src"], e["dst"]
            if src not in ids and dst not in ids:
                continue
            row = {
                "src": sanitize_label(src), "dst": sanitize_label(dst),
                "relation": e["relation"], "confidence": e["confidence"],
                "weight": e.get("weight"), "context": e.get("context"),
            }
            if src in ids:
                out[src][key].append(row)
            if dst in ids and dst != src:
                out[dst][key].append(row)

    _bucket(repo_dependency_edges(store), "dependencies")
    _bucket(repo_http_flow_edges(store), "http_flow")
    _bucket(repo_event_flow_edges(store), "event_flow")
    return out


def impact(store, node_id: str, hops: int = 3, limit: int = 100,
           repo: str | None = None) -> dict:
    """Reverse blast radius for a node (reuses ``impact.blast_radius``).

    Resolves a node id OR a bare symbol name via the shared ``resolve_target`` (exact
    id -> exact name -> fuzzy), so the explorer accepts a symbol name too. When the name
    is defined in several repos it returns ``found=False`` with ``ambiguous=True`` and a
    ``candidates`` list, rather than silently seeding an unrelated repo's symbol.
    """
    from ..impact import blast_radius, resolve_target

    node, candidates = resolve_target(store, node_id, repo=repo)
    if node is None:
        return {"seed": sanitize_label(node_id), "found": False, "hops": hops,
                "total": 0, "truncated": False, "hits": [],
                "ambiguous": bool(candidates),
                "candidates": [{"repo": sanitize_label(c.repo), "kind": sanitize_label(c.kind),
                                "name": sanitize_label(c.name)} for c in candidates[:10]]}
    hits, truncated = blast_radius(store, node.id, hops=hops, limit=limit)
    return {
        "seed": sanitize_label(node.id),
        "name": sanitize_label(node.name),
        "found": True,
        "hops": hops,
        "total": len(hits),
        "truncated": truncated,
        "hits": [{
            "id": sanitize_label(h.id), "repo": sanitize_label(h.repo),
            "kind": sanitize_label(h.kind), "name": sanitize_label(h.name),
            "hop": h.hop, "via": sanitize_label(h.via), "confidence": h.confidence,
        } for h in hits],
    }


def health(store, store_dir) -> dict:
    """Knowledge-graph health: stale repos + dangling edges (reuses ``lint_result``)."""
    from ..commands import lint_result

    res = lint_result(store, _store_dir(store, store_dir))
    return {
        "repos": res["repos"],
        "checked": res["checked"],
        "stale": res["stale"],
        "dangling": res["dangling"],
        "stale_repos": [sanitize_label(x) for x in res["stale_repos"]],
        "dangling_sample": [{
            "repo": sanitize_label(d["repo"]), "src": sanitize_label(d["src"]),
            "relation": d["relation"], "dst": sanitize_label(d["dst"]),
        } for d in res["dangling_sample"]],
    }


def _node_out(n) -> dict:
    attrs = getattr(n, "attrs", None) or {}
    return {
        "id": sanitize_label(n.id), "repo": sanitize_label(n.repo),
        "kind": sanitize_label(n.kind), "name": sanitize_label(n.name),
        "qualified_name": sanitize_label(n.qualified_name) or None,
        "file": sanitize_label(n.file) or None,
        "line_start": n.line_start, "line_end": n.line_end,
        "lang": sanitize_label(n.lang) or None,
        "signature": sanitize_label(attrs["signature"]) if attrs.get("signature") else None,
        "doc": sanitize_label(attrs["doc"]) if attrs.get("doc") else None,
    }


def code_search(store, q: str, kind: str | None = None, repo: str | None = None,
                limit: int = 20) -> dict:
    """Lexical code search over the graph (reuses ``store.search``)."""
    nodes = store.search(q, kind=kind, repo=repo, limit=max(1, min(limit, 200)))
    return {"query": sanitize_label(q), "semantic": False, "total": len(nodes),
            "results": [_node_out(n) for n in nodes]}


def semantic_search(store, q: str, *, vector_store=None, embedder=None,
                    repo: str | None = None, limit: int = 20) -> dict:
    """Optional semantic search — live-only, guarded on an embedder + vector store.

    Returns the same shape as :func:`code_search`. When the embedder or vector store
    is unavailable it degrades to lexical ``code_search`` (semantic is live-only and
    never part of an offline ``--site`` snapshot).
    """
    if embedder is None or vector_store is None:
        out = code_search(store, q, repo=repo, limit=limit)
        out["semantic"] = False
        return out
    try:
        from ..embeddings.hybrid import hybrid_search
        ranked = hybrid_search(store, vector_store, embedder, q,
                               k=max(1, min(limit, 200)), repo=repo)
        nodes = [n for nid, _ in ranked if (n := store.get_node(nid)) is not None]
    except Exception:  # noqa: BLE001 - any embedder failure degrades to lexical
        out = code_search(store, q, repo=repo, limit=limit)
        out["semantic"] = False
        return out
    return {"query": sanitize_label(q), "semantic": True, "total": len(nodes),
            "results": [_node_out(n) for n in nodes]}
