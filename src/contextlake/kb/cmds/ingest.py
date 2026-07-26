"""`contextlake ingest` -- aggregate external documents into the graph."""

from __future__ import annotations

from ... import style
from ...logging_setup import log
from ..config import load_kb_config
from ..store.shards import GraphShard, write_shard
from ._common import (
    _open_store,
)


def _embed_documents(vs, embedder, repo_id, nodes, texts, batch_size) -> int:
    """Embed document *bodies* into the vector store (real RAG over content), keyed by
    node id — separate from code-node embedding, which embeds the name/signature."""
    from ..embeddings.store import guard_store_identity
    written = 0
    for i in range(0, len(nodes), batch_size):
        bn, bt = nodes[i:i + batch_size], texts[i:i + batch_size]
        try:
            vecs = embedder.embed(bt)
        except Exception:  # noqa: BLE001 - an unreachable embedder ends the embed phase
            break
        if i == 0 and vecs and vecs[0]:
            identity = (getattr(embedder, "identity", None)
                        or getattr(embedder, "name", "embedder"))
            guard_store_identity(vs, identity, len(vecs[0]))
        vs.upsert((n.id, repo_id, v) for n, v in zip(bn, vecs))
        written += len(bn)
    return written


def cmd_ingest(args) -> int:
    """Aggregate external documents (built-in + plugin sources) into the graph and,
    when embeddings are enabled, the semantic store."""
    from ..model import Node
    from ..sources import build_source, discover_sources

    store, store_dir = _open_store(args)
    try:
        cfg = load_kb_config(getattr(args, "config", None))
        registry = discover_sources()
        # Zero-config fast path: `ingest --path DIR [--source-type files]`.
        jobs = []  # (name, type, options)
        cli_path = getattr(args, "path", None)
        if cli_path:
            jobs.append(("cli", getattr(args, "source_type", None) or "files",
                         {"path": cli_path}))
        else:
            for s in cfg.sources:
                if s.type in registry and s.enabled:
                    jobs.append((s.name or s.type, s.type,
                                 getattr(s, "model_extra", None) or {}))
        if not jobs:
            log('No document sources. Try: contextlake ingest --path ./docs  '
                '(or add [[sources]] type="files" path="…" to kb.toml). '
                f"Available source types: {', '.join(sorted(registry))}")
            return 0

        embedder = vs = None
        if cfg.embeddings.enabled:
            from ..embeddings import build_embedder
            from ..embeddings.store import build_vector_store
            embedder = build_embedder(cfg.embeddings)
            if embedder is not None:
                vs = build_vector_store(store_dir / "embeddings.sqlite",
                                        backend=cfg.embeddings.vector_backend,
                                        chunk_size=cfg.embeddings.vector_chunk_size)

        total = embedded = failed = 0
        try:
            for name, stype, options in jobs:
                src = build_source(stype, **options)
                if src is None:
                    log(f"  {name}: unknown source type {stype!r}, skipping", inline=True)
                    failed += 1
                    continue
                repo_id = f"@ingest:{name}"
                nodes, texts = [], []
                try:
                    for doc in src.iter_documents():
                        nodes.append(Node(id=f"{repo_id}:{doc.id}", repo=repo_id,
                                          kind="document", name=doc.title,
                                          file=(doc.uri or None),
                                          attrs={**doc.attrs, "source": stype}))
                        texts.append(doc.text)
                except Exception as e:  # noqa: BLE001 - one source must not abort the run
                    log(f"  {name}: source failed ({e})", inline=True)
                    failed += 1
                    continue
                if not nodes:
                    log(f"  {name}: no documents", inline=True)
                    continue
                store.clear_repo(repo_id)
                store.upsert_nodes(repo_id, nodes)
                write_shard(store_dir, GraphShard(repo=repo_id, head_commit="ingest",
                                                  nodes=nodes, edges=[]))
                total += len(nodes)
                msg = f"  {name}: {len(nodes)} document(s)"
                if embedder is not None and vs is not None:
                    n = _embed_documents(vs, embedder, repo_id, nodes, texts,
                                         cfg.embeddings.batch_size)
                    embedded += n
                    msg += f", {n} embedded"
                log(msg, inline=True)
            tail = f", {embedded} embedded into the semantic store" if embedded else ""
            log(style.summary_line("ok", f"Ingest complete: {total} document(s) aggregated{tail}"))
            return 1 if (failed and total == 0) else 0
        finally:
            if vs is not None:
                vs.close()
    finally:
        store.close()

