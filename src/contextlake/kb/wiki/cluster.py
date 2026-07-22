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
    """A member repo's one-line role: langs, top symbols, packages, head."""
    b = repo_brief(store_dir, repo_id)
    if b is None:
        return {"repo": repo_id, "langs": {}, "kinds": {}, "top": [], "packages": [], "head": None}
    return {
        "repo": repo_id,
        "langs": b["langs"],
        "kinds": b["kinds"],
        "top": [t["name"] for t in b["top_symbols"][:5]],
        "packages": b["packages"][:8],
        "head": b["head"],
    }


def namespace_brief(store, store_dir, namespace: str, *, max_repos: int = 40) -> dict | None:
    """Grounded facts about a cluster, or None if the namespace has no repos."""
    mem = members(store, namespace)
    if not mem:
        return None
    member_set = set(mem)
    truncated = len(mem) > max_repos
    roles = [_compressed_role(store_dir, r) for r in mem[:max_repos]]
    internal, boundary = split_edges(cross_repo_edges(store), member_set)
    heads = {r["repo"]: r["head"] for r in roles if r.get("head")}
    return {
        "namespace": namespace.rstrip("/"),
        "repos": roles,
        "member_count": len(mem),
        "internal_edges": internal,
        "boundary_edges": boundary,
        "heads": heads,
        "truncated": truncated,
    }


def cluster_page_name(namespace: str) -> str:
    """Storage filename for a cluster page (``_ns__`` prefix so it can never
    collide with a per-repo page)."""
    return "_ns__" + namespace.rstrip("/").replace("/", "__") + ".md"


def cluster_fingerprint(brief: dict) -> str:
    """Stable short hash of the member (repo, head) pairs, for freshness skip."""
    pairs = sorted((r, h) for r, h in (brief.get("heads") or {}).items())
    return hashlib.sha1(repr(pairs).encode("utf-8")).hexdigest()[:12]


def _phrase_edge(e: dict) -> str:
    s, d, w = e["src"], e["dst"], e.get("weight", 1)
    if e["flavor"] == "http":
        return f"{s} calls {d} over HTTP ({w} shared endpoint(s))"
    if e["flavor"] == "event":
        return f"{s} publishes events consumed by {d} ({w} shared topic(s))"
    return f"{s} depends on {d} (shared package)"


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
    lines += [
        "",
        "Write a cluster wiki page in Markdown with sections: Overview, Services "
        "(one line each), How they talk (internal), External coupling, Shared "
        "dependencies. Ground every statement in the facts above; do not speculate "
        "or invent any coupling not listed.",
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
