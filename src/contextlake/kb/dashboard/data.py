"""Pure, JSON-able data functions backing the dashboard.

Every function here reuses the exact logic behind an existing MCP tool (see
``kb/server.py``) so the dashboard surface and the agent surface never drift:

* :func:`fleet_overview` / :func:`derive_groups` — ``graph_stats`` + ``list_repos``.
* :func:`repo_detail` — ``get_repo_brief`` + ``get_readme`` + ``get_wiki`` +
  ``who_knows`` (``ownership.compute_owners``) + ``get_repo_links``.
* :func:`repo_relationships` — ``repo_dependencies`` / ``repo_flow`` /
  ``repo_event_flow`` (``arch.resolve``).
* :func:`data_flow` — raw ``reads``/``writes`` edges (``kb/flow/data.py``), intra-repo
  only; see its docstring for why this isn't a fourth ``repo_relationships`` key.
* :func:`impact` — ``blast_radius`` (``impact.blast_radius``).
* :func:`health` — ``graph_health`` (``commands.lint_result``).
* :func:`code_search` — ``search_code`` (``store.search``).
* :func:`mcp_console` — introspects a real ``server.build_server()`` instance
  for the live tool catalog; reuses ``steer.generate.mcp_server_entry`` for the
  client-config snippet. Live-only (not part of an offline ``--site`` export).
* :func:`settings` — the active ``kb.toml`` via ``config.load_kb_config``, read
  only. Live-only, same reason as above.

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
import os
import re
from pathlib import Path

from ..model import EXTERNAL_LINK_RELATIONS
from ..security import sanitize_label


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
def _symbol_out(t: dict) -> dict:
    row = {
        "kind": sanitize_label(t["kind"]),
        "name": sanitize_label(t["name"]),
        "file": sanitize_label(t["file"]) if t.get("file") else None,
        "signature": sanitize_label(t["signature"]) if t.get("signature") else None,
        "doc": sanitize_label(t["doc"]) if t.get("doc") else None,
    }
    if "count" in t:
        row["count"] = t["count"]
    return row


def _brief_out(brief: dict | None, *, anonymize: bool = False) -> dict | None:
    """Sanitize a ``wiki.generate.repo_brief`` dict for the browser (mirrors
    ``get_repo_brief``).

    ``readme_excerpt`` is dropped when ``anonymize`` -- same rule as
    ``readme_html``/wiki body: anonymized exports never carry README prose,
    which can hold author names / internal URLs as plain text.
    """
    if not brief:
        return None
    return {
        "repo": sanitize_label(brief["repo"]),
        "head": sanitize_label(brief["head"]) if brief.get("head") else None,
        "node_count": brief["node_count"],
        "edge_count": brief["edge_count"],
        "kinds": brief["kinds"],
        "langs": brief["langs"],
        "top_symbols": [_symbol_out(t) for t in brief["top_symbols"]],
        # Hubs (most called) / dispatchers (widest fan-out) -- the dashboard's
        # hotspot/risk ranking, split from the combined degree above. See
        # wiki.generate.repo_brief.
        "hubs": [_symbol_out(t) for t in brief.get("hubs", [])],
        "dispatchers": [_symbol_out(t) for t in brief.get("dispatchers", [])],
        "packages": [sanitize_label(p) for p in brief["packages"]],
        "files": [sanitize_label(f) for f in brief["files"]],
        "setup_signals": [sanitize_label(f) for f in brief.get("setup_signals", [])],
        "readme_excerpt": (None if anonymize or not brief.get("readme_excerpt")
                           else sanitize_label(brief["readme_excerpt"], max_len=4000)),
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


def _link_entry(n, e, *, anonymize: bool = False) -> dict:
    attrs = getattr(n, "attrs", None) or {}
    title = attrs.get("title") or attrs.get("summary")
    url = None if anonymize else _safe_url(attrs.get("url"))
    return {
        "kind": sanitize_label(n.kind),
        "name": sanitize_label(n.name),
        "url": sanitize_label(url) if url else None,
        "title": sanitize_label(title) if title else None,
        "status": sanitize_label(attrs["status"]) if attrs.get("status") else None,
        "confidence": _conf(e),
    }


def _links_for(store, repo_id: str, *, anonymize: bool = False) -> dict:
    """External cross-links grouped by relation -- the dashboard's Links panel.

    Shares :data:`~contextlake.kb.model.EXTERNAL_LINK_RELATIONS` with the MCP
    ``get_repo_links`` tool rather than re-listing the relations, so the two front
    doors onto this surface cannot drift apart (see that constant's docstring).
    """
    from ..ids import make_id

    grouped: dict[str, list[dict]] = {}
    for e in store.neighbors(make_id("repo", repo_id), direction="out"):
        if e.relation not in EXTERNAL_LINK_RELATIONS:
            continue
        n = store.get_node(e.dst)
        if not n:
            continue
        grouped.setdefault(e.relation, []).append(_link_entry(n, e, anonymize=anonymize))
    return grouped


def _symbol_tickets(store, node_id: str, *, anonymize: bool = False) -> list[dict]:
    """A symbol's OWN ``tracked_by`` links (per-symbol ticket attribution --
    see ``connectors/symbol_refs.py``), distinct from ``_links_for``'s
    repo-level links. Empty when the symbol has none, which is the normal
    case today (attribution needs a configured Atlassian source + a
    docstring/blame-derived issue key that a live JQL call confirmed)."""
    out = []
    for e in store.neighbors(node_id, relation="tracked_by", direction="out"):
        n = store.get_node(e.dst)
        if n:
            out.append(_link_entry(n, e, anonymize=anonymize))
    return out


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


def _wiki_out(store, store_dir: Path, repo_id: str, *, module: str | None = None) -> dict:
    """The generated wiki page rendered to sanitized HTML, with the staleness flag
    (reuses ``get_wiki`` logic + ``visualize._md_to_html`` / ``repo_slug``).

    ``module``, when given, reads a subsystem/module page (``wiki/_modules/``,
    Task 15's ``_module_wiki_filename`` convention) instead of the whole-repo page
    -- lazily imported from ``cmds.wiki`` (same lazy-import style this function
    already uses for ``visualize``) to reuse that filename-sanitization logic
    rather than duplicating it. The staleness check is unchanged: it still
    compares against ``repo_id``'s CURRENT ``head_commit`` -- a module page
    embeds the same ``at commit \\`...\\`` footer text as the whole-repo page
    (same repo, same commit), so the same regex extraction applies as-is.
    """
    from ..visualize import _md_to_html, repo_slug

    if module:
        from ..cmds.wiki import _module_wiki_filename

        wiki_file = store_dir / "wiki" / "_modules" / _module_wiki_filename(repo_id, module)
    else:
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


def cluster_detail(store, store_dir, namespace: str, *, anonymize: bool = False,
                   edges: list | None = None) -> dict | None:
    """A namespace cluster narrative: the rendered cluster wiki HTML plus member
    count and internal/boundary coupling counts. None if the namespace has no
    indexed repos. The prose HTML is dropped under ``anonymize`` (it can carry
    internal names / URLs as live anchors), keeping only the counts + found flag.
    ``edges`` lets a batch caller pass a precomputed ``cross_repo_edges``.
    """
    from ..visualize import _md_to_html
    from ..wiki.cluster import cluster_page_name, namespace_brief

    sd = _store_dir(store, store_dir)
    brief = namespace_brief(store, sd, namespace, edges=edges)
    if brief is None:
        return None
    wiki_file = sd / "wiki" / cluster_page_name(namespace)
    found = wiki_file.exists()
    html = None
    if found and not anonymize:
        raw = wiki_file.read_text(encoding="utf-8", errors="replace")
        html = _md_to_html(sanitize_label(raw, max_len=200_000))
    return {
        "namespace": sanitize_label(brief["namespace"]),
        "member_count": brief["member_count"],
        "internal": len(brief["internal_edges"]),
        "boundary": len(brief["boundary_edges"]),
        "found": found,
        "html": html,
    }


def cluster_index(store, store_dir, repo_ids, *, cap: int = 50,
                  anonymize: bool = False) -> dict:
    """``{namespace: cluster_detail}`` for every repo-id prefix that has a generated
    cluster page on disk. Bounded to ``cap`` clusters (build-time cost: each detail
    scans the cross-repo edges). Prefixes are derived from the repo ids so any
    ``--namespaces`` depth is picked up without decoding filenames."""
    from ..wiki.cluster import cluster_page_name, cross_repo_edges

    sd = _store_dir(store, store_dir)
    candidates = set()
    for r in repo_ids:
        parts = r.split("/")
        for d in range(1, len(parts)):        # every namespace prefix (not the full id)
            candidates.add("/".join(parts[:d]))
    edges = None
    out: dict = {}
    for ns in sorted(candidates):
        if len(out) >= cap:
            break
        if (sd / "wiki" / cluster_page_name(ns)).exists():
            if edges is None:                  # scan the store once for the whole index
                edges = cross_repo_edges(store)
            detail = cluster_detail(store, sd, ns, anonymize=anonymize, edges=edges)
            if detail:
                out[ns] = detail
    return out


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
        "brief": _brief_out(repo_brief(sd, repo_id, store=store), anonymize=anonymize),
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


def data_flow(store, repo_id: str, *, limit: int = 500) -> dict:
    """File -> table/view ``reads``/``writes`` edges for one repo (``kb/flow/data.py``,
    shipped v2.48.0 but never surfaced anywhere until now -- no CLI, dashboard, or
    ``visualize/`` consumer read these edges before this function).

    Deliberately NOT folded into :func:`repo_relationships` as a fourth
    ``dependencies``/``http_flow``/``event_flow``-style key: those three are
    repo→repo aggregates from ``arch.resolve``, keyed on a shared node id that's
    unconditionally synthesized (an endpoint/topic string needs no lookup against a
    prior definition, so two independently-parsed repos land on the same node for
    free). ``reads``/``writes`` instead *resolve* against a table/view actually
    defined in the SAME repo (``kb/parse.py``'s per-repo-only ``by_id``), so no
    cross-repo edge is ever created -- there is no repo→repo dataflow to aggregate,
    only file→table within one repo. Forcing that into the repo-pair shape would
    misrepresent it; this returns the honest row shape instead.

    Bounded like every other payload function here (``diagram``'s ``max_nodes``,
    ``impact``'s ``limit``) -- a data-access-heavy repo on the real fleet store could
    otherwise return thousands of unpaginated rows.
    """
    rows = store.conn.execute(
        """
        SELECT e.relation, e.source_file, e.source_line, n.name, n.kind
        FROM edges e JOIN nodes n ON n.node_id = e.dst
        WHERE e.repo_id = ? AND e.relation IN ('reads', 'writes')
        ORDER BY e.source_file, e.source_line
        LIMIT ?
        """,
        (repo_id, limit + 1),
    ).fetchall()
    return {
        "rows": [
            {
                "file": sanitize_label(file) if file else None,
                "line": line,
                "table": sanitize_label(name),
                "kind": kind,
                "relation": relation,
            }
            for relation, file, line, name, kind in rows[:limit]
        ],
        "truncated": len(rows) > limit,
    }


# Formats offered on a repo's Diagrams tab. sequencediagram is deliberately excluded:
# it needs a single symbol seed (graph --node/--name/--search), not a repo-wide view,
# so a repo-scoped payload would render its "needs exactly one seed" placeholder every
# time -- see kb/visualize/diagrams.py::to_sequence_diagram's docstring. It's served
# separately, from sequence_diagram() below, seeded by the dashboard's symbol page.
DIAGRAM_FORMATS = ("mermaid", "classdiagram", "statediagram", "erdiagram", "deploymentdiagram")


def diagram(store, repo_id: str, fmt: str, *, max_nodes: int = 500,
           max_edges: int = 400, module: str | None = None) -> dict:
    """Render one of :data:`DIAGRAM_FORMATS` for a repo's internal graph.

    Reuses the exact ``repo_subgraph`` -> ``to_payload`` -> renderer pipeline
    ``contextlake graph --repo <repo> --format <fmt>`` already uses (no new
    extraction, no new rendering logic) -- this is the CLI's own output, now also
    reachable from the dashboard. ``module``, when given, scopes the diagram to
    one top-level path segment (see :func:`repo_modules`) -- the fix for a repo
    too large to render meaningfully in one slice.
    """
    from .. import visualize as viz

    if fmt not in DIAGRAM_FORMATS:
        return {"repo": sanitize_label(repo_id), "format": fmt, "error": "unknown format"}

    meta: dict = {"mode": "repo", "repo": repo_id}
    nodes, edges = viz.repo_subgraph(store, repo_id, max_nodes=max_nodes,
                                     max_edges=max_edges, path_prefix=module, meta=meta)
    payload = viz.to_payload(nodes, edges, meta)
    renderer = {
        "mermaid": viz.to_mermaid,
        "classdiagram": viz.to_class_diagram,
        "statediagram": viz.to_state_diagram,
        "erdiagram": viz.to_er_diagram,
        "deploymentdiagram": viz.to_deployment_diagram,
    }[fmt]
    return {
        "repo": sanitize_label(repo_id),
        "format": fmt,
        "text": renderer(payload),
        "truncated": bool(meta.get("truncated")),
    }


def repo_modules(store, repo_id: str, *, within: str | None = None,
                 store_dir=None, wiki_pages: bool = False) -> dict:
    """Path segments worth offering as a "scope to one module" choice on a
    repo's Diagrams tab, per :func:`kb.visualize.payload.repo_modules`. ``within``
    requests the next level down instead of the top-level list -- see that
    function's docstring for why a repo can need more than one level.

    ``wiki_pages``, when true, adds a ``has_page`` flag to each module -- whether
    wiki generation actually wrote that module a subsystem page on disk (checked
    forward, via :func:`cmds.wiki._module_wiki_filename` on the module's own
    prefix -- that function's own docstring says a reverse filename->prefix
    mapping would be ambiguous, so this only ever checks "does this known prefix
    have a page", never the other direction). Wiki generation caps at
    ``_MAX_MODULE_PAGES_PER_REPO`` pages per repo (see that module), so on a
    repo with many more qualifying modules than the cap, most will have
    ``has_page: False`` -- this lets a caller (the dashboard's Wiki-tab module
    picker) filter to only the modules that will actually resolve, instead of
    offering dozens of options that 404 forever. Left off by default (a stat
    call per module) since the Diagrams tab's drill-down control uses this same
    function and has no use for wiki-page presence."""
    from .. import visualize as viz

    modules = viz.repo_modules(store, repo_id, within=within)
    if wiki_pages:
        from ..cmds.wiki import _module_wiki_filename

        sd = _store_dir(store, store_dir)
        wiki_modules_dir = sd / "wiki" / "_modules"
        for m in modules:
            wiki_file = wiki_modules_dir / _module_wiki_filename(repo_id, m["prefix"])
            m["has_page"] = wiki_file.exists()
    return {"repo": sanitize_label(repo_id), "modules": modules}


def repo_wiki(store, store_dir, repo_id: str, *, module: str | None = None) -> dict:
    """Wiki content for one repo, optionally scoped to a subsystem/module page
    (Task 15/16's per-subsystem pages for large federated repos).

    Served as its OWN lightweight route rather than a ``?module=`` query param
    tacked onto the base ``/api/repo/<id>`` route (which also carries brief/
    readme/owners/links) -- mirroring the existing ``diagram()``/``repo_modules()``
    precedent (both already separate sub-routes off ``/api/repo/<id>/...``, not
    params on the base route). This way the dashboard's Wiki-tab module picker
    can swap in a different subsystem's page with one small fetch, without
    re-fetching the rest of the repo-detail payload on every switch.
    """
    sd = _store_dir(store, store_dir)
    out = _wiki_out(store, sd, repo_id, module=module)
    out["repo"] = sanitize_label(repo_id)
    out["module"] = module
    return out


def sequence_diagram(store, node_id: str, *, hops: int = 2, max_nodes: int = 500,
                     max_fanout: int = 50) -> dict:
    """Render the sequencediagram Mermaid format for one already-resolved seed symbol
    (the id ``impact()`` returns as ``seed``, i.e. the dashboard's symbol/blast-radius
    page).

    Mirrors ``contextlake graph --node <id> --format sequencediagram``'s own
    ``extract_subgraph -> to_payload -> to_sequence_diagram`` pipeline (``kb/cmds/
    graph.py``) -- NOT ``diagram()``'s ``repo_subgraph`` pipeline: ``to_sequence_diagram``
    needs a view with exactly one BFS seed (``meta["seed_ids"]`` of length 1), which a
    repo-wide view can't supply (see ``DIAGRAM_FORMATS``'s docstring above).
    """
    from .. import visualize as viz

    node = store.get_node(node_id)
    if node is None:
        return {"seed": sanitize_label(node_id), "format": "sequencediagram",
                "error": "node not found"}

    meta: dict = {}
    nodes, edges = viz.extract_subgraph(store, [node.id], hops=hops, max_nodes=max_nodes,
                                        max_fanout=max_fanout, direction="both", meta=meta)
    meta.update(mode="neighborhood", seed_ids=[node.id], hops=hops)
    payload = viz.to_payload(nodes, edges, meta)
    return {
        "seed": sanitize_label(node.id),
        "format": "sequencediagram",
        "text": viz.to_sequence_diagram(payload),
        "truncated": bool(meta.get("truncated")),
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
           repo: str | None = None, *, anonymize: bool = False) -> dict:
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
        "repo": sanitize_label(node.repo),
        "found": True,
        "hops": hops,
        "total": len(hits),
        "truncated": truncated,
        "ticket": _symbol_tickets(store, node.id, anonymize=anonymize),
        "hits": [{
            "id": sanitize_label(h.id), "repo": sanitize_label(h.repo),
            "kind": sanitize_label(h.kind), "name": sanitize_label(h.name),
            "hop": h.hop, "via": sanitize_label(h.via), "confidence": h.confidence,
        } for h in hits],
    }


def path(store, src: str, dst: str, *, max_hops: int = 6, repo: str | None = None) -> dict:
    """Shortest route between two symbols/node ids -- "how does A reach B", as a
    numbered sequence of steps rather than a diagram (a route is what the
    question actually asks for; drawing the rest of the graph around it only
    adds places to get lost).

    Reuses the exact same pieces the MCP `shortest_path` tool and the Symbol
    page's blast-radius view already use: ``impact.resolve_target`` for id/name/
    fuzzy resolution (so a bare symbol name works, with the same
    ambiguous-across-repos handling ``impact()`` has) and ``server._bfs_path``
    for the BFS itself -- no new path-finding logic.
    """
    from .. import server as mcp_server
    from ..impact import resolve_target

    def _resolve(target):
        node, candidates = resolve_target(store, target, repo=repo)
        return node, [{"repo": sanitize_label(c.repo), "kind": sanitize_label(c.kind),
                      "name": sanitize_label(c.name)} for c in candidates[:10]]

    src_node, src_candidates = _resolve(src)
    if src_node is None:
        return {"from": sanitize_label(src), "to": sanitize_label(dst), "found": False,
                "steps": [], "which": "from", "ambiguous": bool(src_candidates),
                "candidates": src_candidates}
    dst_node, dst_candidates = _resolve(dst)
    if dst_node is None:
        return {"from": sanitize_label(src_node.name), "to": sanitize_label(dst), "found": False,
                "steps": [], "which": "to", "ambiguous": bool(dst_candidates),
                "candidates": dst_candidates}

    ids = mcp_server._bfs_path(store, src_node.id, dst_node.id, max_hops)
    if not ids:
        return {"from": sanitize_label(src_node.name), "to": sanitize_label(dst_node.name),
                "found": False, "steps": [], "which": None, "ambiguous": False, "candidates": []}
    steps = [n for nid in ids if (n := store.get_node(nid)) is not None]
    return {
        "from": sanitize_label(src_node.name), "to": sanitize_label(dst_node.name),
        "found": True, "hops": len(steps) - 1,
        "steps": [{
            "id": sanitize_label(n.id), "name": sanitize_label(n.name),
            "kind": sanitize_label(n.kind),
            "file": sanitize_label(n.file) if n.file else None,
        } for n in steps],
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


# ---------------------------------------------------------------------------
# MCP console + settings (live-only: describe this machine/process, not the
# graph, so neither is part of an offline --site snapshot)
# ---------------------------------------------------------------------------
def mcp_console(store, store_dir, *, config_path: str | None = None, sample: bool = False) -> dict:
    """Read-only MCP surface summary: the live tool catalog and copyable client
    config snippets. Reuses the exact code ``contextlake serve``/``steer`` already
    run — no new backend logic:

    * The tool list is introspected from a real :func:`server.build_server`
      instance (never started, no transport bound) so it can never drift from
      what ``contextlake serve`` actually exposes for this store.
    * ``semantic_search``/``hybrid_search`` are gated the same way ``cmd_serve``
      gates them (an embedder configured + a vector store on disk); a real
      embedder is built (cheap — construction doesn't load a model, matching
      how ``cmd_embed`` treats it as a separate readiness probe) but the vector
      store existence check alone decides whether to build the real one, since
      introspection never queries it.
    * The ``.mcp.json`` / ``.vscode/mcp.json`` snippets reuse
      :func:`steer.generate.mcp_server_entry` — the identical entry ``contextlake
      steer`` writes to disk.

    ``sample=True`` (the ``--sample`` demo fleet) skips the real config
    precedence chain entirely and uses bare :class:`KbConfig` defaults --
    ``load_kb_config(None)`` still merges the user's real
    ``~/.contextlake/kb.toml`` regardless of ``config_path``, which would leak
    real embedder settings into a surface billed as "nothing local is read".
    """
    from .. import server as mcp_server
    from ..config import KbConfig, load_kb_config
    from ..embeddings import build_embedder
    from ..steer.generate import mcp_server_entry

    cfg = KbConfig() if sample else load_kb_config(config_path)
    embedder = build_embedder(cfg.embeddings)
    vec_path = Path(store_dir) / "embeddings.sqlite"
    semantic_available = embedder is not None and vec_path.exists()
    # build_server only checks `vector_store is not None` at tool-registration
    # time -- the real object is queried later, inside a tool body, only when a
    # client actually calls semantic_search/hybrid_search. A cheap sentinel is
    # enough to make those two tools register for the catalog; opening a real
    # embeddings.sqlite vector store here would cost a file open on every
    # /api/mcp request just to list tool names.
    vector_store = object() if semantic_available else None

    mcp = mcp_server.build_server(store, embedder=embedder, vector_store=vector_store)
    tools = sorted(mcp._tool_manager.list_tools(), key=lambda t: t.name)
    entry = mcp_server_entry(config_path)
    return {
        "store_dir": sanitize_label(str(store_dir)),
        "semantic_search_available": semantic_available,
        "tool_count": len(tools),
        # Docstrings are first-party (defined in server.py), so the usual 256-char
        # untrusted-content cap would just mangle a legitimate multi-paragraph tool
        # description -- still run through sanitize_label for control-char stripping,
        # with headroom for a full docstring instead.
        "tools": [{"name": sanitize_label(t.name),
                  "description": sanitize_label(t.description, max_len=4000)}
                 for t in tools],
        "mcp_json": {"mcpServers": {"contextlake": entry}},
        "vscode_mcp_json": {"servers": {"contextlake": entry}},
    }


def settings(store, store_dir, *, config_path: str | None = None, sample: bool = False) -> dict:
    """Read-only summary of the active ``kb.toml``: store path/size/schema version,
    the mirror root (derived from indexed repo paths, not a separate config read),
    connector list, embedder, and LLM config. No in-browser editing — every field
    here just reflects a config the user already wrote; point them at the file
    (reported by ``store_dir``'s config precedence, same as every other command)
    to change anything.

    Connector rows show *configured* status only (name/type/enabled), not a live
    connectivity probe — probing every connector on every dashboard page load
    would be a real, surprising network side effect from a read-only view.
    ``contextlake source test <name>`` already does that on demand.

    ``sample=True`` (the ``--sample`` demo fleet) uses bare :class:`KbConfig`
    defaults instead of resolving the real precedence chain --
    ``load_kb_config(None)`` still merges the user's real
    ``~/.contextlake/kb.toml`` regardless of ``config_path``, which would leak
    real languages/embeddings/llm/connector config into a surface billed as
    "fictional data, safe to share".
    """
    from ..config import KbConfig, load_kb_config
    from ..store.sqlite_store import SCHEMA_VERSION

    cfg = KbConfig() if sample else load_kb_config(config_path)
    sd = Path(store_dir)

    size = 0
    if sd.is_dir():
        for p in sd.rglob("*"):
            if p.is_file():
                size += p.stat().st_size

    repo_paths = [r.path for r in store.list_repos() if r.path]
    mirror_root = None
    if repo_paths:
        try:
            mirror_root = os.path.commonpath(repo_paths)
        except ValueError:      # paths on different drives (Windows) — no common root
            mirror_root = None

    stored_schema = store.get_meta("schema_version")
    return {
        "store_dir": sanitize_label(str(sd)),
        "store_size_bytes": size,
        "schema_version": {
            "running": SCHEMA_VERSION,
            "stored": int(stored_schema) if stored_schema is not None else None,
        },
        "mirror_root": sanitize_label(mirror_root) if mirror_root else None,
        "languages": list(cfg.languages),
        "embeddings": {"enabled": cfg.embeddings.enabled, "provider": cfg.embeddings.provider,
                      "model": cfg.embeddings.model},
        "llm": {"enabled": cfg.llm.enabled, "provider": cfg.llm.provider, "model": cfg.llm.model},
        "sources": [{"name": sanitize_label(s.name), "type": s.type, "enabled": s.enabled}
                   for s in cfg.sources],
    }
