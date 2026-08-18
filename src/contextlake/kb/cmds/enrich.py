"""`contextlake enrich` -- query-driven fan-out to connected sources."""

from __future__ import annotations

from ... import style
from ...logging_setup import log
from ._common import (
    _connect_targets,
    _guard_store,
    _open_store,
    kb_config,
)


def cmd_enrich(args) -> int:
    """Turn each target repo's own codebase into search terms, fan them out to
    every configured term-searchable source (a generic MCP ``tool``, or an
    ``atlassian`` cross-search), and store the results in its isolated
    ``@enrich:<repo>`` partition."""
    from ..connectors.enrich import run_enrich_repo

    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "enrich"):
        store.close()
        return 1
    try:
        cfg = kb_config(args)
        term_searchable = [s for s in cfg.sources
                           if s.enabled and (s.tool or s.type == "atlassian")]
        if not term_searchable:
            log("No term-searchable sources configured (add an `mcp` source with "
                "a `tool`, or an `atlassian` source)")
            return 0

        targets = _connect_targets(args, store)
        if not targets:
            log("No repos to enrich (index some first, or pass --workspace, or "
                "name a repo)")
            return 0

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
            log(f"Enriching {len(targets)} repo(s) against {len(term_searchable)} "
                f"term-searchable source(s)")
            total = 0
            for repo_id, _path in targets:
                n = run_enrich_repo(store, store_dir, cfg, repo_id,
                                    embedder=embedder, vector_store=vector_store)
                total += n
                log(f"  {repo_id}: {n} document(s)", inline=True)
            degraded = degraded_calls() - degraded_before
            if degraded:
                log(style.warn(
                    f"{degraded} source call(s) returned nothing because the source was "
                    "unavailable (reasons logged above); these results are incomplete"))
            # Nothing stored AND calls written off is not an empty result, it is a failed
            # one. An expired token, a dead host and a 404 all land here, and exiting 0
            # made them indistinguishable from a clean run over repos with nothing to find.
            if degraded and not total:
                # Says what was MEASURED. "No source could be reached" is `kb connect`'s
                # wording under a stricter condition it actually tracks (every attempt
                # failed); this command counts written-off calls, not attempts, so one
                # healthy source returning nothing beside one dead source would have made
                # that sentence false.
                log(style.summary_line(
                    "fail", f"Enrich failed: nothing stored, and {degraded} source call(s) "
                            f"were written off as unavailable"))
                return 1
            # Partial degradation with results still exits 0, which is `kb connect`'s
            # existing rule and is deliberately copied rather than tightened: two sibling
            # commands giving different verdicts for the same event is the defect this
            # whole batch is about, and a stricter rule invented here would recreate it.
            kind = "warn" if degraded else "ok"
            word = "incomplete" if degraded else "complete"
            log(style.summary_line(kind, f"Enrich {word}: {total} document(s) stored"))
            return 0
        finally:
            if vector_store is not None:
                vector_store.close()
    finally:
        store.close()
