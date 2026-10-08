"""`contextlake enrich` -- query-driven fan-out to connected sources."""

from __future__ import annotations

import sqlite3

from ... import style
from ...logging_setup import log
from ._common import (
    _connect_targets,
    _guard_store,
    _open_store,
    kb_config,
)


def _print_plan(store_dir, targets, sources) -> int:
    """`kb enrich --dry-run`: the terms and queries each repo would send, sending none.

    Prints no header and no credential: an `api` source's query URLs carry the search
    terms only, and its token stays in the environment variable the config names.
    """
    from ..connectors.enrich import build_terms, render_search_urls
    from ..connectors.mcp_query import _cfg_get

    requests = 0
    log(f"Dry run: {len(targets)} repo(s), {len(sources)} term-searchable source(s). "
        f"Nothing is sent and nothing is stored.")
    for repo_id, _path in targets:
        terms = build_terms(store_dir, repo_id)
        if not terms:
            log(f"  {repo_id}: no graph shard, nothing would be searched", inline=True)
            continue
        log(f"  {repo_id}: {len(terms)} term(s): {', '.join(terms)}", inline=True)
        for src in sources:
            name = _cfg_get(src, "name", "?")
            if _cfg_get(src, "type") == "api":
                urls = render_search_urls(_cfg_get(src, "search_url") or "", terms)
                requests += len(urls)
                for url in urls:
                    log(f"    {name}: GET {url}", inline=True)
                if not urls:
                    log(f"    {name}: search_url has no {{term}} or {{terms}} "
                        f"placeholder, so it would be reported unavailable", inline=True)
            else:
                requests += 1
                log(f"    {name}: one search for {' '.join(terms)!r}", inline=True)
    log(f"Dry run complete: {requests} request(s) would be sent.")
    return 0


def cmd_enrich(args) -> int:
    """Turn each target repo's own codebase into search terms, fan them out to
    every configured term-searchable source (a generic MCP ``tool``, or an
    ``atlassian`` cross-search), and store the results in its isolated
    ``@enrich:<repo>`` partition."""
    from ..connectors.enrich import EnrichBudget, run_enrich_repo

    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "enrich"):
        store.close()
        return 1
    try:
        cfg = kb_config(args)
        term_searchable = [s for s in cfg.sources
                           if s.enabled and (s.tool or s.type == "atlassian"
                                             or (s.type == "api"
                                                 and getattr(s, "search_url", None)))]
        if not term_searchable:
            log("No term-searchable sources configured (add an `mcp` source with "
                "a `tool`, an `atlassian` source, or an `api` source with a "
                "`search_url`)")
            return 0

        targets = _connect_targets(args, store)
        if not targets:
            log("No repos to enrich (index some first, or pass --workspace, or "
                "name a repo)")
            return 0

        if getattr(args, "dry_run", False):
            return _print_plan(store_dir, targets, term_searchable)
        limit = getattr(args, "max_documents", None)
        budget = EnrichBudget(limit) if limit is not None else None

        embedder = vector_store = None
        if cfg.embeddings.enabled:
            from ..embeddings import build_embedder
            from ..embeddings.store import build_vector_store
            embedder = build_embedder(cfg.embeddings)
            if embedder is not None:
                vector_store = build_vector_store(
                    store_dir / "embeddings.sqlite",
                    backend=cfg.embeddings.vector_backend,
                    chunk_size=cfg.embeddings.vector_chunk_size,
                )

        try:
            from ..resilience import degraded_calls

            # The verdict is taken from the calls, not from exceptions. Source methods
            # here are contractually non-raising -- an unreachable source yields nothing so
            # one dead source cannot break the run -- which makes a try/except blind to
            # exactly the failure that matters. `kb connect` has read this counter since it
            # learned the same lesson; this command sat beside it returning 0 whatever
            # happened, so a run where EVERY source call was written off printed the same
            # green line as a healthy run over repos with nothing to find.
            degraded_before = degraded_calls()
            # Read from the target list BEFORE the loop, and never from the counters
            # below. `planned` computed as the sum of the buckets would make the
            # "everything is accounted for" check a tautology that holds however
            # many repos the loop drops.
            planned = len(targets)
            log(f"Enriching {planned} repo(s) against {len(term_searchable)} "
                f"term-searchable source(s)")
            total = 0
            edge_total = 0
            # Five buckets, one per repo, no overlap. `kb wiki` had to grow a
            # `suppressed` counter after its four numbers quietly added up to less
            # than the run planned, and six missing pages read the same as a repo
            # that had none.
            enriched = nothing_returned = unattached = failed = skipped = 0
            # A sixth bucket for the repos whose sources could not be reached. Their
            # stored results were left alone, so they are neither "nothing returned"
            # (a source answered empty and the partition was cleared) nor "failed" (a
            # store or shard write raised).
            kept = 0
            # Repos not searched because `--max-documents` was reached. Their previous
            # results are kept, as for an unavailable source.
            capped = 0
            for repo_id, _path in targets:
                try:
                    counts = run_enrich_repo(store, store_dir, cfg, repo_id,
                                             embedder=embedder, vector_store=vector_store,
                                             budget=budget)
                except (OSError, sqlite3.Error) as e:
                    # Narrow on purpose. `search_source` is contractually non-raising,
                    # so the only failures that reach here are the store and shard
                    # writes: a full disk, a locked or corrupt database. Anything
                    # else still aborts the run rather than being filed as one bad
                    # repo.
                    failed += 1
                    log(f"  {style.warn(repo_id)}: enrichment failed ({e})", inline=True)
                    continue
                total += counts.documents
                edge_total += counts.edges
                if not counts.terms:
                    skipped += 1
                    log(f"  {repo_id}: skipped (no graph shard to build terms from, "
                        f"so nothing was searched for; run index first)", inline=True)
                elif counts.capped:
                    capped += 1
                    log(f"  {repo_id}: not searched, the --max-documents cap was "
                        f"reached; kept the previous results", inline=True)
                elif counts.unavailable:
                    kept += 1
                    log(f"  {style.warn(repo_id)}: {counts.unavailable} source(s) "
                        f"unavailable, kept the previous results", inline=True)
                elif not counts.documents:
                    nothing_returned += 1
                    log(f"  {repo_id}: {counts.terms} term(s), nothing returned",
                        inline=True)
                elif not counts.edges:
                    # A state, not a failure, and deliberately not styled as one. The
                    # matcher is whole-word with a 3-character floor, so a document
                    # that discusses this repo in prose without naming a symbol
                    # correctly attaches to nothing.
                    unattached += 1
                    log(f"  {repo_id}: {counts.terms} term(s), {counts.documents} "
                        f"document(s), 0 edges to code (returned, unattached)",
                        inline=True)
                else:
                    enriched += 1
                    log(f"  {repo_id}: {counts.terms} term(s), {counts.documents} "
                        f"document(s), {counts.edges} edge(s) to code", inline=True)
            # Built once and printed on EVERY exit path below. A run that ends early
            # still has to say where its repos went: an accounting line that appears
            # only on the happy path leaves the reader guessing on the one run where
            # the numbers matter most.
            # Repo-level counters get repo-level nouns. `kb wiki` once printed a
            # page-level counter as "failed for all N repo(s)".
            buckets_line = (f"  {planned} repo(s) planned: {enriched} enriched, "
                            f"{nothing_returned} nothing returned, {unattached} "
                            f"returned but unattached, {failed} failed, "
                            f"{skipped} skipped, {kept} kept previous results"
                            + (f", {capped} not searched (cap reached)" if capped else ""))
            degraded = degraded_calls() - degraded_before
            if degraded:
                log(style.warn(
                    f"{degraded} source call(s) returned nothing because the source was "
                    "unavailable (reasons logged above); these results are incomplete"))
            # Nothing stored AND calls written off is not an empty result, it is a failed
            # one. An expired token, a dead host and a 404 all land here, and exiting 0
            # made them indistinguishable from a clean run over repos with nothing to find.
            if (degraded or kept) and not total:
                # Says what was MEASURED. "No source could be reached" is `kb connect`'s
                # wording under a stricter condition it actually tracks (every attempt
                # failed); this command counts written-off calls, not attempts, so one
                # healthy source returning nothing beside one dead source would have made
                # that sentence false.
                log(style.summary_line(
                    "fail", f"Enrich failed: nothing stored, and {degraded} source call(s) "
                            f"were written off as unavailable"))
                log(buckets_line)
                return 1
            if unattached:
                log(f"  {unattached} repo(s) returned documents that name none of their "
                    f"symbols (returned, unattached). That is the correct result when "
                    f"the documents discuss the repo in prose. Check the repo is "
                    f"indexed and that its symbol names appear in the text.")
            # Partial degradation with results exits 0 with these warnings, the rule
            # `kb connect` has: a source that is down for good would otherwise fail every
            # scheduled `bootstrap`. A repo whose source failed keeps its previous results.
            kind = "warn" if (degraded or failed or kept) else "ok"
            word = "incomplete" if (degraded or failed or kept) else "complete"
            # Both numbers, never one instead of the other: documents stored answers
            # "did the sources have anything", edges to code answers "can a question
            # about the code reach it".
            log(style.summary_line(
                kind, f"Enrich {word}: {total} document(s) stored, "
                      f"{edge_total} edge(s) to code"))
            log(buckets_line)
            if planned and failed == planned:
                log(style.warn(f"Enrich failed for all {planned} repo(s): "
                               f"nothing was stored"))
                return 1
            if degraded or kept:
                log("  Nothing was silently dropped: each unavailable source is logged "
                    "above, and the repos listed there kept their previous results.")
            return 0
        finally:
            if vector_store is not None:
                vector_store.close()
    finally:
        store.close()
