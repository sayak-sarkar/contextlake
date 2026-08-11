"""Fleet / namespace-level cluster wiki.

A cluster page describes a *group* of repos (a namespace prefix) and how they
fit together: which services call which over HTTP, publish/consume which events,
and share which packages. It is grounded strictly in the cross-repo edges the
graph already resolved (``arch/resolve``) plus each member repo's brief, so the
model narrates rather than invents. It reuses the per-repo wiki's grounding
contract (``generate.SYSTEM`` + the council gate) at cluster scope.
"""

from __future__ import annotations

import hashlib
from datetime import date

from .generate import SYSTEM, repo_brief


def members(store, namespace: str) -> list[str]:
    """Repo ids in ``namespace``: exactly ``namespace`` or under ``namespace/``."""
    ns = namespace.rstrip("/")
    return sorted(r.id for r in store.list_repos()
                  if r.id == ns or r.id.startswith(ns + "/"))


def namespaces_at_depth(repo_ids, depth: int = 2) -> list[str]:
    """Distinct raw namespace prefixes at ``depth`` (``a/b/c`` -> ``a/b`` at 2).

    Mirrors the bucketing in ``dashboard.data.derive_groups`` / ``visualize.
    _site_index`` but returns the RAW prefixes (no sanitizing) for generation; a
    repo with no namespace beyond ``depth`` segments is skipped (the derive_groups
    ``(ungrouped)`` bucket is not a cluster)."""
    depth = max(1, int(depth))
    out = set()
    for r in repo_ids:
        parts = r.split("/")
        if len(parts) > depth:
            out.add("/".join(parts[:depth]))
    return sorted(out)


def cross_repo_edges(store) -> list[dict]:
    """Every repo->repo edge (dependency / HTTP flow / event flow), each tagged
    with a ``flavor`` so the narrative can phrase it correctly."""
    from ..arch.resolve import (
        repo_dependency_edges,
        repo_event_flow_edges,
        repo_http_flow_edges,
    )

    out: list[dict] = []
    for fn, flavor in ((repo_dependency_edges, "depends"),
                       (repo_http_flow_edges, "http"),
                       (repo_event_flow_edges, "event")):
        for e in fn(store):
            out.append({**e, "flavor": flavor})
    return out


def split_edges(edges: list[dict], member_set: set) -> tuple[list[dict], list[dict]]:
    """Partition edges into (internal, boundary): internal = both endpoints in
    the namespace; boundary = exactly one endpoint in it."""
    internal, boundary = [], []
    for e in edges:
        s_in, d_in = e["src"] in member_set, e["dst"] in member_set
        if s_in and d_in:
            internal.append(e)
        elif s_in or d_in:
            boundary.append(e)
    return internal, boundary


def _compressed_role(store_dir, repo_id: str) -> dict:
    """A member repo's one-line role: languages, top symbols, head commit."""
    b = repo_brief(store_dir, repo_id)
    if b is None:
        return {"repo": repo_id, "langs": {}, "top": [], "head": None}
    return {
        "repo": repo_id,
        "langs": b["langs"],
        "top": [t["name"] for t in b["top_symbols"][:5]],
        "head": b["head"],
    }


def namespace_brief(store, store_dir, namespace: str, *, max_repos: int = 40,
                    edges: list | None = None) -> dict | None:
    """Grounded facts about a cluster, or None if the namespace has no repos.

    ``edges`` lets a caller pass a precomputed ``cross_repo_edges(store)`` so a
    multi-namespace run does not rescan the whole store per namespace.
    """
    mem = members(store, namespace)
    if not mem:
        return None
    member_set = set(mem)
    truncated = len(mem) > max_repos
    roles = [_compressed_role(store_dir, r) for r in mem[:max_repos]]
    if edges is None:
        edges = cross_repo_edges(store)
    internal, boundary = split_edges(edges, member_set)
    # Freshness fingerprint covers ALL members (the current indexed head from the
    # repos table), not just the shown roles, so a change in member 41+ is noticed.
    # (head, parser) per member, not head alone: a parser bump changes what the graph
    # holds at an unchanged commit, so a commit-only fingerprint reports a cluster page
    # built from a graph that no longer exists as fresh. Read from the repos row's
    # stamp, so this costs one indexed lookup per member and never imports the parser.
    heads = {}
    for r in mem:
        repo = store.get_repo(r)
        if repo is not None and repo.head_commit:
            heads[r] = repo.head_commit
    parsers = {r: store.get_repo_parser_version(r) for r in heads}
    return {
        "namespace": namespace.rstrip("/"),
        "repos": roles,
        "member_count": len(mem),
        "internal_edges": internal,
        "boundary_edges": boundary,
        "heads": heads,
        "parsers": parsers,
        "truncated": truncated,
    }


def cluster_page_name(namespace: str) -> str:
    """Storage path (relative to the wiki dir) for a cluster page. Lives in its own
    ``_clusters/`` subdir so a namespace slug can never collide with a per-repo page
    (which sits directly under the wiki dir)."""
    return "_clusters/" + namespace.rstrip("/").replace("/", "__") + ".md"


def cluster_fingerprint(brief: dict) -> str:
    """Stable short hash of the member (repo, head, parser) triples, for freshness skip.

    The parser version is part of the key because a cluster page can go stale without a
    single member commit moving: the parser changes what it extracts from the same code,
    so the page then describes a graph that no longer exists.

    ``usedforsecurity=False`` because this is a cache key, not a signature: it answers
    "have the member commits moved" and nothing trusts it. Without the flag a
    FIPS-enabled host refuses SHA-1 outright and raises, so `kb wiki --namespaces`
    crashes there on a hash whose weakness is irrelevant to what it is used for.
    """
    parsers = brief.get("parsers") or {}
    pairs = sorted((r, h, parsers.get(r))
                   for r, h in (brief.get("heads") or {}).items())
    return hashlib.sha1(repr(pairs).encode("utf-8"),
                        usedforsecurity=False).hexdigest()[:12]


def _busiest_coupling(internal_edges: list[dict], *, top_n: int = 5) -> list[dict]:
    """Internal edges sorted by shared-edge weight, highest first -- the
    cluster-level equivalent of a per-repo "hub": changes to these two repos'
    shared surface likely ripple across the namespace. No new metric --
    ``weight`` already exists on every edge (see ``_phrase_edge``)."""
    return sorted(internal_edges, key=lambda e: e.get("weight", 1), reverse=True)[:top_n]


def _leakiest_repos(boundary_edges: list[dict], member_ids: set, *,
                    top_n: int = 5) -> list[tuple[str, int]]:
    """Member repos ranked by how many boundary edges touch them -- a repo with
    the most connections crossing out of the namespace has the widest external
    blast radius for a change at its edge."""
    from collections import Counter

    counts: Counter = Counter()
    for e in boundary_edges:
        if e["src"] in member_ids:
            counts[e["src"]] += 1
        if e["dst"] in member_ids:
            counts[e["dst"]] += 1
    return counts.most_common(top_n)


def _phrase_edge(e: dict) -> str:
    s, d, w = e["src"], e["dst"], e.get("weight", 1)
    if e["flavor"] == "http":
        return f"{s} calls {d} over HTTP ({w} shared endpoint(s))"
    if e["flavor"] == "event":
        return f"{s} publishes events consumed by {d} ({w} shared topic(s))"
    return f"{s} depends on {d} ({w} shared package(s))"


# The cluster prompt's directive prose, split out for the same reason as
# `generate.PROMPT_INSTRUCTIONS` (see there): the draft validator matches a
# generated page against these strings, so an instruction the model echoed
# instead of followed is caught even after someone rewords it here.
_SECTIONS_INSTRUCTION = (
    "Ground every statement in the facts above; do not speculate or invent "
    "any coupling not listed. Omit a section entirely if the facts above give you "
    "nothing to say for it -- do not write a heading with no content."
)

# Deliberately just the one: the other directive in this prompt ("No coupling
# between these repositories was detected in the graph. Do NOT invent
# connections; state that the coupling is not detected.") *asks* the page to
# restate its first sentence, so a compliant page repeating it verbatim is
# correct output, not leakage.
CLUSTER_PROMPT_INSTRUCTIONS = (_SECTIONS_INSTRUCTION,)


def render_cluster_prompt(brief: dict) -> str:
    """A grounded prompt: member roles + internal/boundary coupling, with an
    explicit no-invention fallback when the graph shows no coupling."""
    shown = len(brief["repos"])
    header = f"{brief['member_count']} repositories in this cluster"
    if brief.get("truncated"):
        header += f" (showing the first {shown})"
    lines = [f"Namespace: {brief['namespace']}", header + ":"]
    for r in brief["repos"]:
        langs = ", ".join(r["langs"]) if r["langs"] else "?"
        top = ", ".join(r["top"][:5])
        lines.append(f"  - {r['repo']} [{langs}]" + (f": {top}" if top else ""))
    lines.append("")
    if brief["internal_edges"]:
        lines.append("How they talk (within this namespace):")
        lines += [f"  - {_phrase_edge(e)}" for e in brief["internal_edges"]]
    else:
        lines.append("No coupling between these repositories was detected in the graph. "
                     "Do NOT invent connections; state that the coupling is not detected.")
    if brief["boundary_edges"]:
        lines.append("")
        lines.append("Couples to repositories outside this namespace:")
        lines += [f"  - {_phrase_edge(e)}" for e in brief["boundary_edges"]]
    member_ids = {r["repo"] for r in brief["repos"]}
    busiest = _busiest_coupling(brief["internal_edges"])
    leakiest = _leakiest_repos(brief["boundary_edges"], member_ids)
    if busiest or leakiest:
        lines.append("")
        lines.append("Coupling risk signal (from the graph, not invented):")
        if busiest:
            lines.append("  Busiest internal coupling (highest shared-edge weight -- "
                         "changes here likely ripple across the namespace):")
            lines += [f"    - {_phrase_edge(e)}" for e in busiest]
        if leakiest:
            lines.append("  Leakiest repos (most connections crossing outside this "
                         "namespace -- a boundary change here has the widest external "
                         "blast radius):")
            lines += [f"    - {repo} ({count} external connection(s))"
                     for repo, count in leakiest]
    sections = "Overview, Services (one line each), How they talk (internal), External coupling"
    if busiest or leakiest:
        sections += ", Gotchas"
    sections += ", Shared dependencies"
    lines += [
        "",
        f"Write a cluster wiki page in Markdown with sections: {sections}, in that "
        "order. " + _SECTIONS_INSTRUCTION,
    ]
    return "\n".join(lines)


def cluster_provenance_footer(brief: dict, verified_at: date | None = None) -> str:
    repos = ", ".join(f"`{r}`" for r in sorted(brief.get("heads") or {}))
    return (
        "\n\n---\n"
        f"*Cluster wiki for `{brief['namespace']}` generated from the knowledge graph "
        f"on {verified_at or date.today()}."
        + (f" Member repos: {repos}." if repos else "")
        + f" cluster-commits: {cluster_fingerprint(brief)}.*"
    )


def generate_cluster_page(llm, brief: dict, *, verified_at: date | None = None) -> str:
    """Council-gate this in the caller (like the per-repo path): this only drafts
    the page + provenance footer from an already-built namespace brief."""
    body = llm.generate(render_cluster_prompt(brief), system=SYSTEM).strip()
    return (f"# {brief['namespace']} (cluster)\n\n{body}"
            + cluster_provenance_footer(brief, verified_at))
