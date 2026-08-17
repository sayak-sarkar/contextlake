"""`contextlake kb wiki` -- generate provenance-stamped wiki pages, gated by the LLM council."""

from __future__ import annotations

import re

from ... import style
from ...logging_setup import log
from ..config import apply_llm_overrides
from ..connectors.text_match import link_documents_to_symbols
from ..store.shards import GraphShard, read_shard, shard_path, write_shard
from ._common import (
    _connect_targets,
    _guard_store,
    _open_store,
    kb_config,
)
from .ingest import _embed_documents


def _wiki_partition(repo_id: str) -> str:
    """Store partition holding a repo's wiki-page sections (advisory prose).

    ``repo_id`` here is a store *key*, not necessarily a repo id known to
    ``store.list_repos()`` -- it's purely string-based (no repo lookup), so a
    composite module key (``f"{repo}::{module_prefix}"``, see
    ``_module_page_plan``) or a cluster page's namespace prefix are equally
    safe to pass in.
    """
    return f"@wiki:{repo_id}"


def _wiki_section_nodes(repo_id: str, page: str, filename: str, *,
                        source_repo: str | None = None):
    """Split a wiki page into ``##`` sections -> (nodes, texts) for the ``@wiki``
    partition. Sections embed as advisory prose alongside the code vectors
    (mirroring the ``@connect``/``@ingest`` partition pattern), so a
    natural-language question can land on the wiki's explanation and still cite
    the page file. Ids are per-section-index, so a regenerated page cleanly
    replaces its predecessor.

    ``source_repo``, when given, is the actual repo id the page's nodes
    belong to, stored in ``attrs["source_repo"]`` -- distinct from ``repo_id``
    when the latter is a composite partition key (a module/subsystem page's
    ``f"{repo}::{prefix}"``, or a cluster page's namespace prefix), so a
    consumer filtering wiki nodes by "which real repo does this belong to"
    gets the actual repo rather than the composite/namespace string. Defaults
    to ``repo_id`` -- unchanged behavior for existing (whole-repo, cluster)
    callers.
    """
    from ..model import Node

    part = _wiki_partition(repo_id)
    attributed_repo = repo_id if source_repo is None else source_repo
    sections, title, buf = [], "Overview", []
    for line in page.splitlines():
        if line.startswith("## "):
            if "\n".join(buf).strip():
                sections.append((title, "\n".join(buf).strip()))
            title, buf = (line[3:].strip() or "Section"), []
        else:
            buf.append(line)
    if "\n".join(buf).strip():
        sections.append((title, "\n".join(buf).strip()))
    nodes, texts = [], []
    for i, (t, body) in enumerate(sections):
        nodes.append(Node(id=f"{part}:{i}", repo=part, kind="wiki",
                          name=f"{repo_id} wiki: {t}", file=f"wiki/{filename}",
                          attrs={"advisory": True, "source_repo": attributed_repo,
                                 "symbol_links": True}))
        texts.append(f"{t}\n{body}")
    return nodes, texts


def _partition_needs_backfill(store, wiki_key: str) -> bool:
    """Whether ``wiki_key``'s stored partition predates something this build
    writes, and so must be re-stored even though its page is commit-fresh.

    Two eras to catch: no partition at all, and a partition written before
    section->symbol linking existed. The marker lives on the *nodes*
    (``attrs["symbol_links"]``) rather than being inferred from whether the
    partition has edges, because "this page mentions no symbols" is a perfectly
    normal zero-edge outcome -- the page's own title stub section is always one
    -- and inferring from edges would re-store and re-embed those pages on
    every single run, forever.
    """
    first = store.get_node(f"{_wiki_partition(wiki_key)}:0")
    return first is None or not first.attrs.get("symbol_links")


def _store_wiki_partition(store, store_dir, repo_id, page, filename, head,
                          embedder=None, vs=None, batch_size=64, *,
                          source_repo: str | None = None) -> int:
    """(Re)write a repo's ``@wiki`` partition from its page and embed the sections
    when the semantic tier is up. Returns the number of sections embedded.

    Each section is also linked to the symbols it names (``documented_by``), so
    "where is this function explained?" is a graph hop. The lookup goes through
    the page's *real* repo (``source_repo``, falling back to ``repo_id``) rather
    than the partition key, which for a module page is the composite
    ``repo::prefix`` and names no repo at all. A cluster page's namespace prefix
    names no repo either, so those pages link nothing -- correctly, since a
    cluster page is about many repos, not one. Symbols are repo-scoped, not
    module-scoped: a module page mentioning a name that also exists in a sibling
    module links to both, which is why these edges are ``AMBIGUOUS``.

    Only the symbol-side edges are written (``repo_fallback=False``): the
    repo-level edge feeds the "external knowledge" panels (``get_repo_links``,
    the dashboard's ``_links_for``), and a wiki page is contextlake's own output,
    not a third-party cross-link like a Jira issue or a Figma frame.
    """
    nodes, texts = _wiki_section_nodes(repo_id, page, filename, source_repo=source_repo)
    part = _wiki_partition(repo_id)
    store.clear_repo(part)
    if not nodes:
        return 0
    edges = link_documents_to_symbols(store, source_repo or repo_id, nodes, texts,
                                      "documented_by", "wiki", repo_fallback=False)
    store.upsert_nodes(part, nodes)
    store.upsert_edges(part, edges)
    write_shard(store_dir, GraphShard(repo=part, head_commit=head or "wiki",
                                      nodes=nodes, edges=edges))
    if embedder is not None and vs is not None:
        # Unpacked: `_embed_documents` reports (count, early-stop reason). Every caller
        # of THIS function discards the result, so the reason is surfaced by the WARNING
        # the helper logs rather than threaded up. Returning the tuple here would make
        # this function's own contract (an int) quietly untrue.
        written, _stopped = _embed_documents(vs, embedder, part, nodes, texts, batch_size)
        return written
    return 0


# --- per-subsystem pages for large, genuinely federated repos ---------------

_FEDERATED_NODE_FLOOR = 5000        # below this, one page is fine regardless of shape
_DOMINANT_MODULE_SHARE = 0.6        # a module owning this share of nodes -> not federated
# A repo with "hundreds of independent top-level subsystems" (the motivating
# case) would otherwise trigger hundreds of LLM + council-gate calls in one
# `wiki` run; cap how many pages one run generates so a single run stays
# bounded. Which modules fill those slots is decided by
# `_select_module_pages` (never-yet-paged first), so the cap bounds one run
# without stranding the tail across runs.
_MAX_MODULE_PAGES_PER_REPO = 20


def _module_page_plan(store, repo_id: str, node_count: int) -> tuple[list[dict], bool]:
    """``(modules worth their own wiki page, may we prune stale module pages?)``

    A module qualifies when the repo is large AND genuinely federated (no
    single module dominates it) -- not just one big repo with one big
    top-level source directory, which the existing whole-repo page already
    grounds well enough.

    The second value exists because an empty module list has two very
    different causes, and one of them must NOT authorize
    ``_prune_orphan_module_pages`` to delete every module page this repo has:

    - The repo genuinely stopped qualifying -- too small now (judged from the
      shard, which the caller has in hand), or one module has come to dominate
      it (the index did report modules, so it is answering). Every existing
      module page really is an orphan; pruning is correct.
    - ``node_count`` (the shard) says the repo is still large, but
      ``repo_modules`` (the SQLite index -- a different persistence layer,
      which ``_run_page`` already handles disagreeing) reports no module
      structure at all. That is an index that is empty, mid-rebuild or
      otherwise not answering, not evidence the repo's modules are gone.
      Deleting on it would cost a full LLM regeneration per page plus the
      embeddings until then, so this run leaves the pages alone and a later
      run (with a working index) prunes if they really are stale.
    """
    if node_count < _FEDERATED_NODE_FLOOR:
        return [], True
    from ..visualize.payload import repo_modules

    modules = repo_modules(store, repo_id)
    if not modules:
        return [], False
    if modules[0]["nodes"] / node_count > _DOMINANT_MODULE_SHARE:
        return [], True
    return modules, True


def _safe_name(part: str) -> str:
    """One path-safe filename component: ``/`` -> ``__``, anything else that is not
    ``\\w``/``.``/``-`` -> ``_``. Never returns a path separator."""
    import re as _re

    return _re.sub(r"[^\w.\-]", "_", part.replace("/", "__"))


def _module_wiki_filename(repo_id: str, prefix: str) -> str:
    """Filename for a module/subsystem wiki page, sanitized like the whole-repo
    page (``repo_id.replace("/", "__")``) but applied to both ``repo_id`` and
    ``prefix`` -- module prefixes returned by ``repo_modules()`` can themselves
    contain ``/`` (e.g. ``"src/foo"`` from a deeper ``within`` scope), so naive
    concatenation would try to write into a nonexistent subdirectory or
    collide with a sibling module's page. Callers must place this under a
    dedicated subdirectory (``wiki/_modules/``, mirroring ``wiki/_clusters/``
    for cluster pages) -- otherwise a module page's sanitized name can collide
    with an unrelated repo's whole-repo page (e.g. module "src" of repo
    "team/app" and the whole-repo page for a repo literally named
    "team/app/src" both sanitize to "team__app__src.md").

    The ``/`` -> ``__`` mapping runs first and is what existing page names depend
    on; every *other* character that a filesystem could read as a path separator is
    then folded to ``_`` by allowlist. That ordering matters twice over. It keeps
    legitimate prefixes byte-identical -- letters, digits, ``_``, ``.`` and ``-``
    all pass through, so ``src/foo`` stays ``…__src__foo.md`` and no existing page
    orphans -- and it closes the traversal that the ``/``-only replacement left
    open: ``module`` arrives here straight off a dashboard query string, and on
    Windows a ``\\``-separated value walked out of the wiki directory (POSIX
    neutralised it by accident, not by design). The allowlist is ``\\w``-based, so
    non-ASCII directory names keep working rather than collapsing to underscores."""
    return f"{_safe_name(repo_id)}__{_safe_name(prefix)}.md"


def _module_page_file(wiki_dir, repo_id: str, prefix: str):
    """On-disk path of one module/subsystem page (the single place that
    composes ``wiki/_modules/`` with :func:`_module_wiki_filename`)."""
    return wiki_dir / "_modules" / _module_wiki_filename(repo_id, prefix)


def _module_partition_head(repo_id: str) -> str:
    """The ``@wiki:{repo}::`` key prefix every module partition of ``repo_id``
    starts with (the whole-repo page's own key is ``@wiki:{repo}``, without the
    ``::``, so it can never be matched by this)."""
    return f"{_wiki_partition(repo_id)}::"


def _existing_module_partitions(store, repo_id: str) -> dict[str, str | None]:
    """``{partition key: the wiki file its sections cite}`` for every
    module/subsystem page already stored for ``repo_id``.

    Matched as a half-open key RANGE rather than a ``LIKE`` prefix pattern:
    ``nodes.repo_id`` is indexed with SQLite's default BINARY collation, which
    a range scan uses (``SEARCH nodes USING INDEX ix_nodes_repo``) and a
    ``LIKE`` does not -- SQLite's LIKE optimization requires
    ``case_sensitive_like``, which this store doesn't set, so a pattern would
    degrade to ``SCAN nodes``: a full pass over every node in the store, per
    repo, on every run. It also sidesteps having to escape LIKE's own
    wildcards, ``_`` above all, which appears in a great many real repo ids
    and would otherwise match another repo's partitions.
    """
    head = _module_partition_head(repo_id)
    upper = head[:-1] + chr(ord(head[-1]) + 1)   # "...::" -> "...:;", exclusive
    rows = store.conn.execute(
        "SELECT repo_id, file FROM nodes WHERE repo_id >= ? AND repo_id < ?",
        (head, upper),
    ).fetchall()
    out: dict[str, str | None] = {}
    for r in rows:
        out.setdefault(r["repo_id"], r["file"])
    return out


def _prune_orphan_module_pages(store, store_dir, wiki_dir, repo_id: str,
                               modules: list[dict], vs=None) -> int:
    """Delete every stored module page of ``repo_id`` whose module is not in
    ``modules`` (the currently-qualifying set). Returns how many were removed.

    A module that shrinks below ``repo_modules()``' floor, or a repo whose
    directory tree is restructured -- or that stops qualifying as federated at
    all, in which case EVERY module page is an orphan -- otherwise leaves its
    page, its ``@wiki:{repo}::{prefix}`` partition and that partition's shard
    and vectors behind forever, so `ask`/search keep returning a page
    describing a module that no longer exists. `--force` didn't prune them
    either: it regenerates what qualifies today and never looks at what used to.

    Run on every run, not only under `--force`: it costs one indexed key-range
    query per repo plus a set difference, and no LLM call. ``modules`` must be the
    FULL qualifying list, never the subset selected for this run's page
    generation (see ``_select_module_pages``) -- pruning against the selection
    would delete the previous run's pages to make room for this run's, and
    successive runs would thrash instead of accumulating coverage.

    Orphan-ness is judged by the same index the pages were generated from, so
    a module the index has genuinely lost is pruned here and simply generated
    again on a later run once the index reports it.
    """
    live = {m["prefix"] for m in modules}
    head = _module_partition_head(repo_id)
    removed = 0
    for part, cited_file in sorted(_existing_module_partitions(store, repo_id).items()):
        prefix = part[len(head):]
        if prefix in live:
            continue
        # The stored citation is authoritative for where the page actually
        # lives; `_module_wiki_filename`'s "/" -> "__" mapping is lossy, so
        # re-deriving the name is only the fallback for a partition that
        # somehow has no file recorded.
        page = ((store_dir / cited_file) if cited_file
                else _module_page_file(wiki_dir, repo_id, prefix))
        page.unlink(missing_ok=True)
        store.clear_repo(part)
        try:
            shard_path(store_dir, part).unlink(missing_ok=True)
        except ValueError:      # unusable as a path -- nothing was ever written there
            pass
        if vs is not None:
            vs.clear_repo(part)
        removed += 1
        log(f"  {repo_id}: pruned the wiki page for `{prefix}`, "
            "which is no longer a qualifying module", inline=True)
    return removed


def _select_module_pages(modules: list[dict], wiki_dir, repo_id: str,
                         cap: int = _MAX_MODULE_PAGES_PER_REPO) -> list[dict]:
    """Which of ``modules`` get a page generated THIS run, at most ``cap``.

    Modules with no page on disk yet come first (each group keeping
    ``repo_modules()``' deterministic largest-first order), then the
    already-paged ones. Without this, a repo with hundreds of qualifying
    modules gave pages to its N largest and permanently stranded the rest:
    a later run with the same head_commit re-picked the exact same top-N
    (and freshness-skipped every one of them), and a new commit re-picked a
    top-N too, never the tail. Preferring the never-paged tail means
    repeated `wiki` runs walk the whole repo instead, while each single run
    stays bounded by ``cap``.

    A repo's first run has no module pages at all, so this degrades to
    exactly the old largest-first top-N.

    A module whose page is STRUCTURAL counts as never-paged. The structural stage writes
    a page at every qualifying module's path before generation runs, so treating mere
    existence as "already generated" made `fresh` permanently empty: rotation could only
    ever re-pick from `paged`, and generated module pages stopped reaching new modules at
    all. Same defect as the freshness check read, in a second reader, and the rotation
    test caught it rather than the change being noticed.
    """
    from ..wiki.structural import is_structural_page

    fresh, paged = [], []
    for m in modules:
        f = _module_page_file(wiki_dir, repo_id, m["prefix"])
        generated = f.exists() and not is_structural_page(
            f.read_text(encoding="utf-8", errors="replace"))
        (paged if generated else fresh).append(m)
    return (fresh + paged)[:cap]


def _reviewed_by(llm, review_llm) -> str:
    """Banner suffix naming the reviewer, only when it isn't the generator."""
    return "" if review_llm is llm else f" reviewed by {review_llm.name}"


def _abstain_note(gate: dict) -> str:
    """Rejection-log suffix counting reviewers that returned nothing parseable.

    Worth surfacing because a *broken* reviewer and a *strict* one look identical
    otherwise: a reviewer that errors back an empty string (a missing API key, a
    CLI not on PATH -- CliLlm returns "" on non-zero exit rather than raising)
    abstains on every lens, which rejects every page at score 0.0. Without this
    the run reads as a page-quality problem instead of a misconfiguration.
    """
    abstained = gate.get("abstained") or 0
    return f", {abstained} reviewer(s) returned nothing parseable" if abstained else ""


def _log_rejection(label: str, gate: dict) -> None:
    """Say why a page was dropped, in the vocabulary of whichever gate dropped it.

    A structural rejection (``wiki.validate.structural_gate``) carries a
    ``reason`` and never reached the council, so reporting it as a council score
    would point an operator at the reviewer model for a defect no reviewer was
    asked about.
    """
    reason = gate.get("reason")
    if reason:
        log(f"  {style.warn(label)}: rejected, {reason} (not sent to the council)",
            inline=True)
    else:
        log(f"  {style.warn(label)}: rejected by council "
            f"(score {gate['score']}{_abstain_note(gate)})", inline=True)
    for issue in gate["issues"][:5]:
        log(f"      - {issue}")


# Structural module pages are capped far higher than generated ones, and for a different
# reason. The generated cap bounds LLM calls per run; this one bounds scoped `repo_brief`
# calls, which are a node/edge filter over the shard -- not free, but nowhere near the cost
# of a model. A repository with hundreds of near-equal subsystems is exactly the shape this
# feature exists for, so the number is set to clear real repos and the truncation is
# REPORTED when it binds, never applied silently.
_MAX_STRUCTURAL_MODULE_PAGES = 200


def _write_if_not_generated(path, page: str) -> bool:
    """Write ``page`` unless the file already holds GENERATED prose. Returns whether it
    wrote.

    A repository has one wiki page per scope, and the structural stage runs on every `kb
    wiki`. Without this check a scheduled run would replace an accepted, reviewed prose
    page with tables every time, which is a regression wearing the costume of a refresh.

    The kind is read out of the file itself rather than tracked beside it, so a page
    somebody restored from a backup or copied into place is classified correctly.
    """
    from ..wiki.structural import is_structural_page

    if path.exists():
        current = path.read_text(encoding="utf-8", errors="replace")
        if current and not is_structural_page(current):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return True


def _structural_stage(store, store_dir, args, cfg, wiki_dir) -> tuple[int, dict]:
    """Write every repository's structural page and its module pages, unconditionally.

    Runs BEFORE the LLM is built and regardless of whether one is configured, because
    that is the point: a generated page needs a backend, and a user without one used to
    get nothing at all out of `kb wiki`.

    Returns ``(pages written, briefs)``, where ``briefs`` is keyed by
    ``(repo_id, path_prefix)``. Handing the briefs back is not an optimisation detail:
    every brief here is built with the SAME arguments the generated path would use, so
    that path reuses them instead of building a second, identical one. A test asserts
    each page's brief is built exactly once, and it caught this stage doubling the count
    the moment it was added.

    Not gated on ``--force``. These pages are deterministic and derived from the shard, so
    regenerating costs milliseconds and always leaves the page agreeing with the graph.
    The freshness machinery on the generated side exists to avoid paying an LLM twice,
    and there is nothing here to pay.
    """
    from ..visualize import repo_slug
    from ..wiki.generate import repo_brief
    from ..wiki.structural import render_structural_page, repo_dependencies, repo_owners

    # The CANONICAL wiki paths, not a parallel directory. A repository has ONE wiki page
    # per scope: the structural page IS that page until something verified replaces it,
    # rather than a second artefact beside it that a reader has to choose between. That
    # also means the dashboard, the MCP server and search all reach it with no changes.
    out_dir = wiki_dir
    anonymize = getattr(cfg, "anonymize", "never") == "always"
    written = 0
    briefs: dict[tuple[str, str | None], dict] = {}
    for repo_id in [r.id for r in store.list_repos()]:
        # The module plan first, because the whole-repo brief takes it: the generated
        # path passes `subsystem_modules` so its page can name the subsystem pages, and
        # a brief built without it is a DIFFERENT brief that path cannot reuse.
        #
        # The node count for that plan comes from the SHARD, not from a preliminary
        # brief. Building one brief to size the repo and a second to carry the modules
        # is two briefs per repo, and a test asserts one -- the parts a brief does not
        # cache are a live README read and an enrichment-shard read, so the second one
        # is real work rather than a cache hit.
        shard = read_shard(store_dir, repo_id)
        if shard is None or not shard.nodes:
            # The SAME refusal the generated path makes, for the same reason: a repository
            # that indexed to no symbols has nothing for a page to be ABOUT, and a
            # confident artefact about nothing is worse than no artefact. Measured once on
            # a repo that indexed to zero nodes and still published 119 lines presenting
            # the forge's boilerplate README as the project's architecture.
            continue
        modules, _prune = _module_page_plan(store, repo_id, len(shard.nodes))
        brief = repo_brief(store_dir, repo_id, store=store,
                           subsystem_modules=modules or None)
        if brief is None or not brief.get("coverage_total"):
            # No FILE-BACKED symbol either: the graph holds only fleet-wide package or
            # module nodes, so every section would describe something that is not this
            # repository's code. Same threshold the generated path applies.
            continue
        briefs[(repo_id, None)] = brief
        deps = repo_dependencies(store, repo_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        page = render_structural_page(
            brief, repo_id=repo_id, modules=modules,
            owners=repo_owners(store, repo_id, anonymize=anonymize),
            dependencies=deps)
        if _write_if_not_generated(out_dir / (repo_slug(repo_id) + ".md"), page):
            written += 1
        planned = modules[:_MAX_STRUCTURAL_MODULE_PAGES]
        if len(modules) > len(planned):
            log(f"  {repo_id}: {len(modules)} modules qualify; writing structural pages "
                f"for the largest {len(planned)}. The rest have no page this run.")
        for m in planned:
            prefix = m["prefix"]
            # `store=None`, matching the generated path exactly. With a store, the
            # brief carries the REPO's README excerpt and its live setup scan, and a
            # module page would present both as that module's -- the same scope
            # mislabelling the page title and the dependency heading already guard
            # against, in a third place.
            scoped = repo_brief(store_dir, repo_id, store=None, path_prefix=prefix)
            if scoped is None or not scoped.get("node_count"):
                continue
            briefs[(repo_id, prefix)] = scoped
            if _write_if_not_generated(_module_page_file(out_dir, repo_id, prefix),
                                       render_structural_page(
                                           scoped, repo_id=repo_id, path_prefix=prefix,
                                           owners=repo_owners(store, repo_id,
                                                              path_prefix=prefix,
                                                              anonymize=anonymize),
                                           dependencies=deps)):
                written += 1
    return written, briefs


def cmd_wiki(args) -> int:
    """Generate provenance-stamped wiki pages from the graph, gated by an LLM council."""
    from ..llm import build_llm, build_review_llm
    from ..wiki.cluster import (
        CLUSTER_PROMPT_INSTRUCTIONS,
        cluster_fingerprint,
        cluster_page_name,
        cross_repo_edges,
        generate_cluster_page,
        namespace_brief,
        namespaces_at_depth,
        render_cluster_prompt,
    )
    from ..wiki.council import LENSES, council_gate
    from ..wiki.generate import (
        PROMPT_INSTRUCTIONS,
        generate_page,
        grounded_symbol_count,
        recorded_subsystems,
        render_prompt,
        repo_brief,
        subsystem_names,
    )
    from ..wiki.validate import structural_gate

    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "wiki"):
        store.close()
        return 1
    embedder = vs = None
    try:
        # Copy before overriding: apply_llm_overrides mutates in place, and the
        # config object is now shared across one invocation (see _common.kb_config)
        # -- including `bootstrap`, which hands one argparse Namespace to every
        # stage in turn. `--llm` must configure this wiki run, not silently switch
        # the LLM tier on for whatever runs after it.
        cfg = kb_config(args).model_copy(deep=True)
        apply_llm_overrides(cfg, provider=getattr(args, "llm", None),
                            model=getattr(args, "llm_model", None))
        # BEFORE the LLM is built, and on every run. The structural page needs no
        # backend, so it is written whether or not one is configured -- which is the
        # whole change: this command used to print "LLM tier disabled" and produce
        # nothing, so a local-first user got no wiki at all.
        #
        # Skipped only for the cluster/namespace modes, which write pages about groups
        # of repositories rather than about one, and have no structural equivalent.
        wiki_dir = store_dir / "wiki"
        # Bound before the branch, not inside it. The cluster path returns before
        # `_run_page` is ever called, so an unbound name would be latent rather than
        # loud -- and latent is how it would survive until somebody reordered the code.
        structural_briefs: dict = {}
        if not (getattr(args, "namespace", None) or getattr(args, "namespaces", False)):
            n, structural_briefs = _structural_stage(store, store_dir, args, cfg,
                                                     wiki_dir)
            if n:
                log(f"Wrote {n} structural page(s) → {wiki_dir}")

        llm = build_llm(cfg.llm)
        if llm is None:
            log("LLM tier disabled, so no generated prose pages were written. "
                "The structural pages above need no model and are complete. "
                "For prose on top of them, pass --llm builtin|ollama|openai "
                "(or set [llm] enabled = true in kb.toml).")
            return 0
        # The council reviews with `review_llm`, which IS `llm` unless [llm]
        # review_provider names a different backend -- letting a cheap local
        # generator be gated by a stronger judge (or the inverse).
        review_llm = build_review_llm(cfg.llm, llm)
        # Before announcing anything. A backend whose prerequisite is missing used to
        # get a "Generating wiki for N repo(s)..." banner and a note about reviewer
        # quality first, then fail per repo -- claiming work that never started and
        # advising on a council that could not convene.
        for role, client in (("generation", llm), ("review", review_llm)):
            # getattr, not a direct call: `preflight` is an optional hook on the base
            # class, and a client only has to be generate()-shaped. Requiring the
            # method would break every duck-typed implementation -- which is exactly
            # what a direct call did to this command's own test doubles.
            check = getattr(client, "preflight", None)
            if not callable(check):
                continue
            try:
                check()
            except Exception as e:  # noqa: BLE001 - the message is the whole point
                log(f"Cannot generate: the {role} backend "
                    f"({getattr(client, 'name', '?')}) is not usable.\n{e}")
                return 1
        if review_llm.name == "builtin":
            # The builtin 0.5B is a weak reviewer (near-constant ~0.95 scores, mostly
            # rubber-stamping) -- still functional, but a real backend gates meaningfully.
            log("Note: the builtin model is a weak council reviewer (tends to accept "
                "almost everything). For meaningful accept/reject gating, point the "
                "council at a real backend: set [llm] review_provider = "
                "\"anthropic\"|\"openai\"|\"ollama\"|\"cli\" (keeping generation local), "
                "or switch both with --llm.")
        targets = _connect_targets(args, store)
        if not targets:
            wanted = [a for a in (getattr(args, "args", None) or []) if a]
            if wanted:
                log(f"No indexed repo matches {', '.join(wanted)} — check the exact repo id "
                    "(see `contextlake mirror status`).")
                return 1
            log("No indexed repos (run index first, or pass --workspace/--source)")
            return 0
        wiki_dir = store_dir / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        log(f"Generating wiki for {len(targets)} repo(s) with {llm.name} "
            f"(council of {len(LENSES)}{_reviewed_by(llm, review_llm)})")
        # Semantic tier (optional): accepted pages also embed into the @wiki
        # partition so NL search can land on the prose (labeled advisory).
        if cfg.embeddings.enabled:
            from ..embeddings import build_embedder
            from ..embeddings.store import build_vector_store
            embedder = build_embedder(cfg.embeddings)
            if embedder is not None:
                vs = build_vector_store(store_dir / "embeddings.sqlite",
                                        backend=cfg.embeddings.vector_backend,
                                        chunk_size=cfg.embeddings.vector_chunk_size)
        force = getattr(args, "force", False)

        # Cluster (namespace) wiki: --namespace <prefix> or --namespaces --depth N.
        namespace = getattr(args, "namespace", None)
        if namespace or getattr(args, "namespaces", False):
            if namespace:
                ns_list = [namespace]
            else:
                depth = getattr(args, "depth", None) or 2
                ns_list = namespaces_at_depth([r.id for r in store.list_repos()], depth)
            if not ns_list:
                log("No namespaces to generate cluster wiki for (index some repos first)")
                return 0
            log(f"Generating cluster wiki for {len(ns_list)} namespace(s) with {llm.name} "
                f"(council of {len(LENSES)}{_reviewed_by(llm, review_llm)})")
            all_edges = cross_repo_edges(store)   # scan the store once, not per namespace
            written = rejected = skipped = failed = 0
            progress = style.Progress(len(ns_list), label="wiki-cluster")
            for ns in ns_list:
                brief = namespace_brief(store, store_dir, ns, edges=all_edges)
                if brief is None:
                    log(f"  {ns}: no indexed repos under this namespace, skipping", inline=True)
                    progress.advance(ns)
                    continue
                page_file = wiki_dir / cluster_page_name(ns)
                # freshness: skip when the member commit fingerprint is unchanged.
                if not force and page_file.exists():
                    prev = page_file.read_text(encoding="utf-8", errors="replace")
                    m = re.search(r"cluster-commits: ([0-9a-f]+)", prev)
                    if m and m.group(1) == cluster_fingerprint(brief):
                        # Same rule as the per-repo page (see _run_page): a page
                        # this path skips was never put through the gate, because
                        # the gate only ever ran on a freshly generated draft.
                        defect = structural_gate(prev, CLUSTER_PROMPT_INSTRUCTIONS)
                        if defect is None:
                            skipped += 1
                            progress.advance(ns)
                            continue
                        log(f"  {ns}: member commits unchanged, but the page on disk fails "
                            f"the structural gate ({defect['reason']}); regenerating it",
                            inline=True)
                try:
                    page = generate_cluster_page(llm, brief)
                    # Structurally broken output is rejected here without paying for
                    # the council: no judge is needed to see a page that echoed its
                    # own instructions or looped one sentence, and a weak one won't.
                    gate = structural_gate(page, CLUSTER_PROMPT_INSTRUCTIONS) or council_gate(
                        review_llm, page, render_cluster_prompt(brief),
                        accept_score=cfg.llm.accept_score,
                        council_size=getattr(cfg.llm, "council_size", None))
                except Exception as e:  # noqa: BLE001 - one cluster must not abort the run
                    log(f"  {style.fail(ns)}: {e}", inline=True)
                    failed += 1
                    progress.advance(ns)
                    continue
                if gate["accepted"]:
                    page_file.parent.mkdir(parents=True, exist_ok=True)   # wiki/_clusters/
                    page_file.write_text(page, encoding="utf-8")
                    # embed cluster prose into @wiki:<namespace> (labeled advisory).
                    _store_wiki_partition(store, store_dir, ns, page, page_file.name, None,
                                          embedder, vs, cfg.embeddings.batch_size)
                    written += 1
                    log(f"  {style.ok(ns)}: written (score {gate['score']})", inline=True)
                else:
                    rejected += 1
                    _log_rejection(ns, gate)
                progress.advance(ns)
            progress.done()
            fail_tail = f", {failed} failed" if failed else ""
            glyph = style.warn() if failed else style.ok()
            log(f"{glyph} Cluster wiki: {written} written, {rejected} rejected, "
                f"{skipped} unchanged (skipped){fail_tail} → {wiki_dir}  "
                f"(--force to regenerate)")
            if failed:
                log("  See the log above for which namespaces failed. Re-run to retry.")
            # An explicit --namespace that matched no repos is a user error, not success.
            if namespace and not (written or rejected or skipped):
                return 1
            if failed and not written and not rejected:
                return 1
            return 0

        written = rejected = skipped = failed = 0
        progress = style.Progress(len(targets), label="wiki")

        def _run_page(*, shard, repo_id, wiki_key, path_prefix, wiki_file, label,
                      subsystem_modules=None) -> str:
            """Freshness-check, generate+gate, and write ONE wiki page -- either
            the whole-repo page (``path_prefix=None``, ``wiki_key=repo_id``) or a
            module/subsystem slice (``path_prefix=<module prefix>``,
            ``wiki_key=f"{repo_id}::{prefix}"``). Shared shape so a module
            page's freshness/backfill semantics exactly mirror the whole-repo
            page's rather than being reinvented. ``shard`` is the caller's
            already-read shard (same for every page of one repo_id, regardless
            of path_prefix, since head_commit is repo-wide) -- avoids a
            redundant read per module. Returns "written" | "rejected" |
            "skipped" | "failed" | "absent" (no brief, or an empty scoped
            brief -- see the module/index-vs-shard mismatch note below).

            ``subsystem_modules``, when given, is threaded into both the
            gate's own ``repo_brief``/``render_prompt`` call and the
            ``generate_page`` call below so the whole-repo page can name its
            subsystem pages -- callers must only pass this for the whole-repo
            page (``path_prefix=None``); a module page describing one slice
            of the repo doesn't itself have "subsystems". It is also half of
            the freshness check: a page is only skipped when its commit is
            unchanged AND it already names these same subsystems.
            """
            # Relative to wiki_dir, not just the basename -- a module page lives
            # under wiki/_modules/, so its Node.file citation must say so
            # (unlike the basename-only convention the older cluster-page code
            # path uses, which is imprecise for its own wiki/_clusters/ pages).
            rel_filename = wiki_file.relative_to(wiki_dir).as_posix()
            # Before anything else, including the freshness check: a repo that
            # indexed to no symbols at all has nothing for a page to be about,
            # and that is knowable from the shard alone. Asked first so an
            # ungrounded page already on disk is not kept alive by the backfill
            # below, which is the one path that makes a page newly searchable.
            if not shard.nodes:
                log(f"  {style.warn(label)}: no page — {repo_id} indexed to 0 symbols, so "
                    "there is nothing to ground one in"
                    + (f" (delete the stale {rel_filename} by hand)"
                       if wiki_file.exists() else ""), inline=True)
                return "rejected"
            head = shard.head_commit
            # A STRUCTURAL page is never "already generated", however fresh its commit
            # stamp is. Both page kinds live at this one path now, and the structural
            # footer carries the same `at commit \`...\`` text this check reads -- so
            # without the kind test the first structural page a repo ever got would make
            # the generated page look up-to-date and prose would never be written at all.
            # Found by an existing summary test reporting 0 written where it expected 1.
            prev_is_structural = False
            if wiki_file.exists():
                from ..wiki.structural import is_structural_page
                prev_is_structural = is_structural_page(
                    wiki_file.read_text(encoding="utf-8", errors="replace"))
            if not force and not prev_is_structural and wiki_file.exists() and head:
                prev = wiki_file.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"at commit `([^`]+)`", prev)
                pm = re.search(r"at commit `[^`]+` \(parser ([^)]+)\)", prev)
                # Three cases, and the third is the one that bites:
                #   page stamped, matches the shard   -> genuinely fresh, skip
                #   page stamped, differs             -> parser moved, regenerate
                #   page NOT stamped                  -> regenerate ONCE, after which
                #     it carries a stamp and settles.
                # But when the SHARD carries no version either, nothing can be
                # established, and demanding a match would regenerate that page on
                # every single run forever rather than once. So an unstamped shard
                # falls back to the commit-only question it asked before.
                want = shard.parser_version
                parser_matches = want is None or (pm is not None and pm.group(1) == want)
                if m and m.group(1) == head and parser_matches:
                    # An unchanged commit means the page's CONTENT inputs are
                    # unchanged -- it does NOT mean the page was generated with
                    # the generation inputs in force today. A store wiki'd
                    # before subsystem naming shipped (or whose module set
                    # moved without a commit moving) is commit-fresh and
                    # field-stale; skipping on the commit alone froze it that
                    # way until its commit changed or --force was passed. Ask
                    # the two questions separately: the page's own footer
                    # records what it names, so compare that against what this
                    # run would name.
                    want = subsystem_names(subsystem_modules)
                    if recorded_subsystems(prev) != want:
                        log(f"  {label}: commit unchanged, but the page does not name the "
                            "subsystem pages this repo now has; regenerating it", inline=True)
                    elif (defect := structural_gate(prev, PROMPT_INSTRUCTIONS)) is not None:
                        # The third freshness question, and the only one asked of
                        # the page itself. A draft is gated when it is generated,
                        # so a page written before the gate shipped (or by a
                        # provider whose output it would now reject) was never
                        # gated at all -- and this path returns before any draft
                        # exists, so it never would be. It stayed on disk, and the
                        # backfill below kept it searchable, until somebody
                        # happened to pass --force. The gate is model-free and
                        # linear in the page, so asking is nearly free.
                        log(f"  {label}: commit unchanged, but the page on disk fails the "
                            f"structural gate ({defect['reason']}); regenerating it",
                            inline=True)
                    else:
                        # Backfill: a page written before the @wiki partition
                        # existed -- or before that partition carried symbol
                        # links -- gets its sections (re)stored, linked and
                        # embedded without a new LLM call. The second case is
                        # the whole reason linking lands at all on a store that
                        # has run `wiki` before: its pages are commit-fresh, so
                        # every one of them would otherwise skip this write and
                        # stay edge-free until an unrelated commit moved or the
                        # user paid a full --force regeneration of the fleet.
                        # Guarded by the gate above: the backfill is the one
                        # thing on this path that makes a page newly searchable,
                        # so it must not do that for a page the gate rejects.
                        if _partition_needs_backfill(store, wiki_key):
                            _store_wiki_partition(store, store_dir, wiki_key, prev,
                                                  rel_filename, head, embedder, vs,
                                                  cfg.embeddings.batch_size,
                                                  source_repo=repo_id)
                        return "skipped"
            # `store` unlocks two repo-root-only, never-path_prefix-scoped
            # live-checkout reads inside repo_brief (readme_excerpt, and
            # setup_signals' recursive legacy-build-tooling walk + top-level
            # config listing -- see repo_brief's own docstring, flagged there
            # by Task 14 as a latent gap for whichever caller first combines
            # `store` with `path_prefix`). Passing both together on a module
            # page would present whole-repo README/setup facts as if they
            # described just this module -- the same mislabeling this task
            # exists to fix for the title/footer, so omit `store` here and let
            # both fields degrade to shard-only (None / skip the live scan),
            # which is exactly repo_brief's documented behavior for that case.
            # Reused from the structural stage when it built one for this exact scope.
            # Every brief there is built with these same arguments precisely so this
            # lookup is sound; a mismatch would silently hand the generated page a brief
            # scoped differently from the one it asked for, which no test could see
            # because both are valid briefs.
            # Bound unconditionally: `generate_page` below takes it too, so leaving it
            # inside the fallback made it unbound on every reused-brief run. Ruff cannot
            # see that (both uses are in the same function) and the tests found it, which
            # is the argument for running them rather than reading the diff.
            brief_store = None if path_prefix else store
            brief = structural_briefs.get((repo_id, path_prefix))
            if brief is None:
                brief = repo_brief(store_dir, repo_id, store=brief_store,
                                   path_prefix=path_prefix,
                                   subsystem_modules=subsystem_modules)
            # A module `repo_modules()` (SQLite index) said has real content can
            # still come back empty here if the shard (JSON, a different
            # persistence layer -- see _module_page_plan) disagrees, e.g. a
            # rebuild race or stale index. Treat that as "nothing to write"
            # rather than generating a near-empty, ungrounded page.
            if brief is None or (path_prefix and not brief["node_count"]):
                return "absent"
            # The finer half of the same question, now that the brief exists: a
            # scope with no FILE-BACKED symbol has nothing derived from code
            # behind it, so every sentence a model wrote would come from the
            # README and the prompt's own framing. Measured on a one-file repo
            # that indexed to 0 nodes and still published a confident 119-line
            # page, scored 0.987, presenting the forge's boilerplate README as
            # the project's own architecture.
            #
            # Decided here rather than inside `structural_gate`, which by
            # contract sees only the draft: grounding is a property of the
            # INPUT, known before the model is called at all. Gating on the
            # draft would pay for a generation and a council review to reject
            # something already known to be unwritable, and would put a second,
            # differently-shaped argument into a gate whose whole value is that
            # it is model-free and text-only.
            #
            # Counted through the same helper the provenance footer's coverage
            # ratio uses, so the refusal and the disclosure cannot disagree
            # about what grounding means for one page.
            if not grounded_symbol_count(brief):
                log(f"  {style.warn(label)}: no page — 0 file-backed symbols in scope, so "
                    "nothing in it would be derived from the code", inline=True)
                return "rejected"
            try:
                # `brief` above is exactly the brief this page needs (same
                # store/path_prefix/subsystem_modules), so hand it over rather
                # than have generate_page build an identical second one: the
                # README read, the legacy-build-tooling walk and the
                # enrichment-shard read are all outside repo_brief's cached
                # core, so that second build is real I/O -- per page, and a
                # federated repo generates up to 21 of them in one run.
                page = generate_page(llm, store_dir, repo_id, store=brief_store,
                                     path_prefix=path_prefix,
                                     subsystem_modules=subsystem_modules,
                                     brief=brief)
                # Structural defects (the page echoed its own instructions, or
                # looped one sentence) are decided here, before the council and
                # without a model: they are mechanically visible, and a weak
                # reviewer demonstrably rubber-stamps them. Rejecting early also
                # saves the council's round trips on a page that cannot pass.
                gate = structural_gate(page, PROMPT_INSTRUCTIONS) or council_gate(
                    review_llm, page, render_prompt(brief, path_prefix=path_prefix),
                    accept_score=cfg.llm.accept_score,
                    council_size=getattr(cfg.llm, "council_size", None))
            except Exception as e:  # noqa: BLE001 - one page must not abort the run
                log(f"  {style.fail(label)}: {e}", inline=True)
                return "failed"
            if gate["accepted"]:
                wiki_file.parent.mkdir(parents=True, exist_ok=True)
                wiki_file.write_text(page, encoding="utf-8")
                _store_wiki_partition(store, store_dir, wiki_key, page, rel_filename,
                                      brief.get("head"), embedder, vs,
                                      cfg.embeddings.batch_size, source_repo=repo_id)
                log(f"  {style.ok(label)}: written (score {gate['score']})", inline=True)
                return "written"
            _log_rejection(label, gate)
            return "rejected"

        for repo_id, _ in targets:
            # Freshness check first, off the cheap shard-only head_commit --
            # repo_brief(..., store=store) below also runs setup_signals'
            # live-checkout scan, which a skipped (unchanged) repo shouldn't pay for.
            shard = read_shard(store_dir, repo_id)
            if shard is None:
                progress.advance(repo_id)
                continue
            node_count = len(shard.nodes)
            # Computed here (once, before the whole-repo page) rather than
            # after it, so the whole-repo page can name its subsystem pages
            # (Task 16) -- reused below for the module-page loop too, so this
            # is still exactly one `repo_modules()` query per repo per run,
            # not two.
            modules, may_prune = _module_page_plan(store, repo_id, node_count)
            # Before selecting or naming anything: drop pages for modules that
            # no longer qualify, so the overview below can't name a subsystem
            # page this run is about to delete. Skipped when the empty module
            # list came from an index that isn't answering rather than from
            # the repo actually changing shape (see `_module_page_plan`).
            if may_prune:
                _prune_orphan_module_pages(store, store_dir, wiki_dir, repo_id, modules, vs)
            selected = _select_module_pages(modules, wiki_dir, repo_id)
            # Name every module that either already HAS a page from an earlier
            # run or is getting one this run -- never a module that has
            # neither, which would point the reader at a page that doesn't
            # exist. Since `_select_module_pages` rotates through the tail,
            # the named set grows run over run until every qualifying module
            # is covered, then stays put.
            selected_prefixes = {m["prefix"] for m in selected}
            named_modules = [
                m for m in modules
                if m["prefix"] in selected_prefixes
                or _module_page_file(wiki_dir, repo_id, m["prefix"]).exists()
            ]
            wiki_file = wiki_dir / (repo_id.replace("/", "__") + ".md")
            outcome = _run_page(shard=shard, repo_id=repo_id, wiki_key=repo_id,
                                path_prefix=None, wiki_file=wiki_file, label=repo_id,
                                subsystem_modules=named_modules)
            if outcome == "written":
                written += 1
            elif outcome == "rejected":
                rejected += 1
            elif outcome == "skipped":
                skipped += 1
            elif outcome == "failed":
                failed += 1
                # Fail fast: the whole-repo page and every module page of this
                # repo go through the same LLM + council, so whatever broke
                # here (backend unreachable, auth rejected, model missing)
                # will almost certainly break each of up to
                # _MAX_MODULE_PAGES_PER_REPO module pages too. Reporting the
                # failure now costs 1 round trip instead of 21.
                log(f"  {repo_id}: whole-repo page failed — not attempting its "
                    "subsystem pages this run", inline=True)
                progress.advance(repo_id)
                continue
            # "absent": no shard / no brief -- matches the prior silent skip.

            # Per-subsystem pages for large, genuinely federated repos --
            # generated IN ADDITION to (never instead of) the whole-repo page
            # above. Folds into the same written/rejected/skipped/failed
            # counters (the summary line below is now page-level, not
            # repo-level). `selected` was already computed above (before the
            # whole-repo page) so its modules could be named there -- reused
            # here as-is, not recomputed.
            if len(modules) > len(selected):
                # Not stranded: `_select_module_pages` puts never-paged
                # modules first, so the modules skipped here are the ones a
                # subsequent run picks up. Say how many are still waiting so
                # the operator knows to re-run rather than assuming the repo
                # is fully covered.
                log(f"  {repo_id}: {len(modules)} qualifying modules, generating "
                   f"{len(selected)} this run "
                   f"({len(modules) - len(selected)} deferred to a later run)",
                   inline=True)
            for module in selected:
                prefix = module["prefix"]
                module_key = f"{repo_id}::{prefix}"
                module_label = module_key
                module_file = _module_page_file(wiki_dir, repo_id, prefix)
                m_outcome = _run_page(shard=shard, repo_id=repo_id, wiki_key=module_key,
                                      path_prefix=prefix, wiki_file=module_file,
                                      label=module_label)
                if m_outcome == "written":
                    written += 1
                elif m_outcome == "rejected":
                    rejected += 1
                elif m_outcome == "skipped":
                    skipped += 1
                elif m_outcome == "failed":
                    failed += 1
                elif m_outcome == "absent":
                    log(f"  {style.warn(module_label)}: repo_modules() reported this "
                       "module but its scoped brief came back empty (shard/index "
                       "mismatch) -- skipping this subsystem page", inline=True)
            progress.advance(repo_id)
        progress.done()
        fail_tail = f", {failed} failed" if failed else ""
        glyph = style.warn() if failed else style.ok()
        log(f"{glyph} Wiki: {written} written, {rejected} rejected, "
            f"{skipped} unchanged (skipped){fail_tail} → {wiki_dir}  (--force to regenerate)")
        # Honest exit: failures with nothing written and nothing council-rejected
        # means the LLM was effectively unreachable for the whole run -> not success.
        if failed and not written and not rejected:
            log(style.warn(f"Wiki generation failed for all {failed} repo(s) — none written"))
            return 1
        if failed:
            log("  See the log above for which repos failed. Re-run to retry.")
        return 0
    finally:
        if vs is not None:
            vs.close()
        store.close()
