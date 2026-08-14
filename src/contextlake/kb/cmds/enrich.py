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
            log(f"Enriching {len(targets)} repo(s) against {len(term_searchable)} "
                f"term-searchable source(s)")
            total = 0
            for repo_id, _path in targets:
                n = run_enrich_repo(store, store_dir, cfg, repo_id,
                                    embedder=embedder, vector_store=vector_store)
                total += n
                log(f"  {repo_id}: {n} document(s)", inline=True)
            log(style.summary_line("ok", f"Enrich complete: {total} document(s) stored"))
            return 0
        finally:
            if vector_store is not None:
                vector_store.close()
    finally:
        store.close()
