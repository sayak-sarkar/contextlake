"""`contextlake wiki` -- generate provenance-stamped wiki pages, gated by the LLM council."""

from __future__ import annotations

import re

from ... import style
from ...logging_setup import log
from ..config import apply_llm_overrides, load_kb_config
from ..store.shards import GraphShard, read_shard, shard_path, write_shard
from ._common import (
    _connect_targets,
    _guard_store,
    _open_store,
)
from .ingest import _embed_documents


def _wiki_partition(repo_id: str) -> str:
    """Store partition holding a repo's wiki-page sections (advisory prose).

    ``repo_id`` here is a store *key*, not necessarily a repo id known to
    ``store.list_repos()`` -- it's purely string-based (no repo lookup), so a
    composite module key (``f"{repo}::{module_prefix}"``, see
    ``_qualifying_modules``) or a cluster page's namespace prefix are equally
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
                          attrs={"advisory": True, "source_repo": attributed_repo}))
        texts.append(f"{t}\n{body}")
    return nodes, texts


def _store_wiki_partition(store, store_dir, repo_id, page, filename, head,
                          embedder=None, vs=None, batch_size=64, *,
                          source_repo: str | None = None) -> int:
    """(Re)write a repo's ``@wiki`` partition from its page and embed the sections
    when the semantic tier is up. Returns the number of sections embedded."""
    nodes, texts = _wiki_section_nodes(repo_id, page, filename, source_repo=source_repo)
    part = _wiki_partition(repo_id)
    store.clear_repo(part)
    if not nodes:
        return 0
    store.upsert_nodes(part, nodes)
    write_shard(store_dir, GraphShard(repo=part, head_commit=head or "wiki",
                                      nodes=nodes, edges=[]))
    if embedder is not None and vs is not None:
        return _embed_documents(vs, embedder, part, nodes, texts, batch_size)
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


def _qualifying_modules(store, repo_id: str, node_count: int) -> list[dict]:
    """Modules worth their own wiki page: the repo is large AND genuinely
    federated (no single module dominates it) -- not just one big repo with
    one big top-level source directory, which the existing whole-repo page
    already grounds well enough."""
    if node_count < _FEDERATED_NODE_FLOOR:
        return []
    from ..visualize.payload import repo_modules

    modules = repo_modules(store, repo_id)
    if not modules:
        return []
    if modules[0]["nodes"] / node_count > _DOMINANT_MODULE_SHARE:
        return []
    return modules


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
    "team/app/src" both sanitize to "team__app__src.md")."""
    return f"{repo_id.replace('/', '__')}__{prefix.replace('/', '__')}.md"


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

    Matched on the partition-key prefix with SQLite's own LIKE wildcards
    escaped -- ``_`` is a single-character wildcard and appears in a great many
    real repo ids, so an unescaped pattern would match (and let the caller
    prune) another repo's partitions. Same escaping idiom as
    ``visualize.payload.repo_modules``.
    """
    head = _module_partition_head(repo_id)
    pattern = head.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = store.conn.execute(
        "SELECT repo_id, file FROM nodes WHERE repo_id LIKE ? ESCAPE '\\'", (pattern,)
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

    Run on every run, not only under `--force`: it costs one key-prefix query
    per repo plus a set difference, and no LLM call. ``modules`` must be the
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
    """
    fresh, paged = [], []
    for m in modules:
        target = paged if _module_page_file(wiki_dir, repo_id, m["prefix"]).exists() else fresh
        target.append(m)
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


def cmd_wiki(args) -> int:
    """Generate provenance-stamped wiki pages from the graph, gated by an LLM council."""
    from ..llm import build_llm, build_review_llm
    from ..wiki.cluster import (
        cluster_fingerprint,
        cluster_page_name,
        cross_repo_edges,
        generate_cluster_page,
        namespace_brief,
        namespaces_at_depth,
        render_cluster_prompt,
    )
    from ..wiki.council import LENSES, council_gate
    from ..wiki.generate import generate_page, render_prompt, repo_brief

    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "wiki"):
        store.close()
        return 1
    embedder = vs = None
    try:
        cfg = load_kb_config(getattr(args, "config", None))
        apply_llm_overrides(cfg, provider=getattr(args, "llm", None),
                            model=getattr(args, "llm_model", None))
        llm = build_llm(cfg.llm)
        if llm is None:
            log("LLM tier disabled — pass --llm builtin|ollama|openai "
                "(or set [llm] enabled = true in kb.toml)")
            return 0
        # The council reviews with `review_llm`, which IS `llm` unless [llm]
        # review_provider names a different backend -- letting a cheap local
        # generator be gated by a stronger judge (or the inverse).
        review_llm = build_review_llm(cfg.llm, llm)
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
                    "(see `contextlake status`).")
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
                        skipped += 1
                        progress.advance(ns)
                        continue
                try:
                    page = generate_cluster_page(llm, brief)
                    gate = council_gate(review_llm, page, render_cluster_prompt(brief),
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
                    log(f"  {style.warn(ns)}: rejected by council "
                        f"(score {gate['score']}{_abstain_note(gate)})", inline=True)
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
            of the repo doesn't itself have "subsystems".
            """
            # Relative to wiki_dir, not just the basename -- a module page lives
            # under wiki/_modules/, so its Node.file citation must say so
            # (unlike the basename-only convention the older cluster-page code
            # path uses, which is imprecise for its own wiki/_clusters/ pages).
            rel_filename = wiki_file.relative_to(wiki_dir).as_posix()
            head = shard.head_commit
            if not force and wiki_file.exists() and head:
                prev = wiki_file.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"at commit `([^`]+)`", prev)
                if m and m.group(1) == head:
                    # Backfill: a page written before the @wiki partition existed
                    # gets its sections stored/embedded without a new LLM call.
                    if store.get_node(f"{_wiki_partition(wiki_key)}:0") is None:
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
            brief_store = None if path_prefix else store
            brief = repo_brief(store_dir, repo_id, store=brief_store, path_prefix=path_prefix,
                               subsystem_modules=subsystem_modules)
            # A module `repo_modules()` (SQLite index) said has real content can
            # still come back empty here if the shard (JSON, a different
            # persistence layer -- see _qualifying_modules) disagrees, e.g. a
            # rebuild race or stale index. Treat that as "nothing to write"
            # rather than generating a near-empty, ungrounded page.
            if brief is None or (path_prefix and not brief["node_count"]):
                return "absent"
            try:
                page = generate_page(llm, store_dir, repo_id, store=brief_store,
                                     path_prefix=path_prefix,
                                     subsystem_modules=subsystem_modules)
                gate = council_gate(review_llm, page, render_prompt(brief, path_prefix=path_prefix),
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
            log(f"  {style.warn(label)}: rejected by council "
               f"(score {gate['score']}{_abstain_note(gate)})", inline=True)
            for issue in gate["issues"][:5]:
                log(f"      - {issue}")
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
            modules = _qualifying_modules(store, repo_id, node_count)
            # Before selecting or naming anything: drop pages for modules that
            # no longer qualify, so the overview below can't name a subsystem
            # page this run is about to delete.
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

