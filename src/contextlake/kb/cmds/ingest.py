"""`contextlake kb ingest` -- aggregate external documents into the graph."""

from __future__ import annotations

import logging

from ... import style
from ...logging_setup import log
from ..connectors.text_match import link_documents_to_symbols, symbol_nodes_for_repo
from ..store.shards import GraphShard, write_shard
from ._common import (
    _guard_store,
    _open_store,
    kb_config,
)


def _embed_documents(vs, embedder, repo_id, nodes, texts, batch_size
                     ) -> tuple[int, int, str | None]:
    """Embed document *bodies* into the vector store (real RAG over content).

    A document is one NODE and one-or-more VECTORS. It used to be exactly one vector over
    the whole text, which for a long page is an average of everything it discusses, so a
    question about one paragraph in it matched poorly. Measured on 29 real documents:
    chunking took hit rate from 71.7% to 94.3% and MRR from 45.2% to 80.4%.

    The chunk index rides inside the stored KEY (`embeddings.store.chunk_key`), and
    `search()` collapses those keys back to node ids, so nothing downstream of the vector
    store knows chunks exist.

    Returns `(documents, vectors, stopped_early)`. Two counts, not one: with chunking they
    differ by an order of magnitude, and reporting vectors as "documents embedded" would
    claim a corpus seventeen times the size of the real one.
    """
    from ..embeddings.chunk import split_document
    from ..embeddings.store import chunk_key, guard_store_identity

    # Flattened up front so batching stays uniform: one flat list of (key, text) means a
    # batch never has to end on a document boundary, and a document larger than one batch
    # is not a special case.
    units: list[tuple[str, str, object]] = []
    for node, text in zip(nodes, texts, strict=True):
        pieces = split_document(text or "")
        # A document whose body is empty still has a node; it simply contributes no vector.
        # Embedding "" would store a vector that matches everything weakly.
        for i, piece in enumerate(pieces):
            units.append((chunk_key(node.id, i), piece, node))

    written_vectors = 0
    embedded_nodes: set = set()
    stopped_early: str | None = None
    for i in range(0, len(units), batch_size):
        batch = units[i:i + batch_size]
        try:
            vecs = embedder.embed([text for _key, text, _node in batch])
        except Exception as e:  # noqa: BLE001 - an unreachable embedder ends the phase
            # The count below used to be returned bare, so an embedder that died at
            # batch 3 of 200 produced `128 embedded`, exit 0 and no line saying the
            # phase ended early -- semantic search then answered confidently over a
            # fraction of the corpus. The number was true; the impression was not.
            stopped_early = f"{type(e).__name__}: {e}"
            log(f"embed: stopped after {len(embedded_nodes)} of {len(nodes)} document(s) "
                f"({written_vectors} of {len(units)} chunk(s)) -- {stopped_early}",
                level=logging.WARNING)
            break
        if i == 0 and vecs and vecs[0]:
            identity = (getattr(embedder, "identity", None)
                        or getattr(embedder, "name", "embedder"))
            guard_store_identity(vs, identity, len(vecs[0]))
        vs.upsert((key, repo_id, v)
                  for (key, _text, _node), v in zip(batch, vecs, strict=True))
        written_vectors += len(batch)
        embedded_nodes.update(id(node) for _key, _text, node in batch)
    return len(embedded_nodes), written_vectors, stopped_early


def cmd_ingest(args) -> int:
    """Aggregate external documents (built-in + plugin sources) into the graph and,
    when embeddings are enabled, the semantic store.

    Documents land in their own synthetic ``@ingest:<name>`` partition, which by
    itself says nothing about which *real* repo they describe. ``--for-repo``
    (or ``for_repo`` on a ``[[sources]]`` entry) supplies that association, and
    is what makes text-mention linking possible: without it there is no code to
    link to and the partition stays edge-free, exactly as it was before.
    """
    from ..model import Node
    from ..sources import build_source, discover_sources

    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "ingest"):
        store.close()
        return 1
    try:
        cfg = kb_config(args)
        registry = discover_sources()
        # Zero-config fast path: `ingest --path DIR [--source-type files]`.
        jobs = []  # (name, type, options, for_repo)
        cli_path = getattr(args, "path", None)
        if cli_path:
            jobs.append(("cli", getattr(args, "source_type", None) or "files",
                         {"path": cli_path}, getattr(args, "for_repo", None) or None))
        # An enabled source whose type this build cannot construct. `source add` now
        # refuses an unknown type at write time, but that does nothing for a config
        # written earlier, edited by hand, or authored ahead of installing the plugin
        # it names. The filter below used to drop those rows in silence, so an ingest
        # configured to read three sources could read one and still report success.
        unavailable: list[str] = []
        if not cli_path:
            for s in cfg.sources:
                if not s.enabled:
                    continue
                if s.type not in registry:
                    unavailable.append(f"{s.name or s.type} (type={s.type})")
                    continue
                # `for_repo` is ours, not the source's -- pop it out of the
                # extras (a copy: `model_extra` is the model's own dict) so it
                # never reaches the source constructor as an unknown kwarg.
                options = dict(getattr(s, "model_extra", None) or {})
                jobs.append((s.name or s.type, s.type, options,
                             options.pop("for_repo", None)))
        if unavailable:
            log(style.warn(
                f"{len(unavailable)} enabled source(s) name a type this build cannot "
                f"run and were skipped: {', '.join(unavailable)}"))
            log(f"  This build can run: {', '.join(sorted(registry))}. A plugin type is "
                "discovered automatically once its package is installed.")
        if not jobs:
            if unavailable:
                # Distinguished from "no sources configured": the config DOES ask for
                # ingestion and none of it could run, which is a failure, not an
                # empty inbox.
                log(style.fail("No source could be run, so nothing was ingested."))
                return 1
            log('No document sources. Try: contextlake kb ingest --path ./docs  '
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

        # Warn once, up front, about a `--for-repo`/`for_repo` naming nothing
        # indexed: silently linking nothing is indistinguishable from a typo.
        # Asked of the same table the linking itself reads (nodes), not of the
        # repos table -- a repo row with no indexed symbols would otherwise pass
        # this check and still link nothing.
        orphaned = [name for name, _t, _o, fr in jobs if not fr]
        if orphaned:
            # Without `for_repo` there is no code to link to, so the partition is
            # edge-free by construction: the documents are searchable by name and
            # reachable by NO traversal. That is a legitimate choice, but it used to be
            # the SILENT DEFAULT -- the flag is opt-in, was never suggested, and the run
            # still ended in `✓ Ingest complete`. Say it once, up front, and name the fix.
            log(style.summary_line(
                "warn", f"{len(orphaned)} source(s) have no --for-repo: "
                        f"{', '.join(orphaned[:3])}"
                        f"{f' (+{len(orphaned) - 3} more)' if len(orphaned) > 3 else ''}"))
            log("  Their documents will be searchable by name but linked to no code, so "
                "`blast_radius` and `find_dependents` cannot reach them.")
            log("  Pass --for-repo <repo-id> (or set for_repo on the [[sources]] entry) "
                "to link them.")
        for name, _t, _o, for_repo in jobs:
            if for_repo and not symbol_nodes_for_repo(store, for_repo):
                log(style.summary_line(
                    "warn", f"{name}: {for_repo!r} has no indexed symbols — its documents "
                            "will be stored but linked to nothing (index that repo first)"))

        # Seeded with the sources this build could not construct at all. They are as
        # much a failure of the run as a source that raised: the config asked for them
        # and the run does not carry their documents. Counting them only in the warning
        # above would leave the summary line and the exit code saying the run was clean.
        total = embedded = partial = 0
        # Vectors are counted apart from documents. With chunking the two differ by an
        # order of magnitude, and printing vectors where a reader expects documents
        # would report a corpus many times the size of the real one.
        vectors = 0
        failed = len(unavailable)
        try:
            for name, stype, options, for_repo in jobs:
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
                misses = list(getattr(src, "failures", ()))
                if not nodes:
                    # "empty" and "broken" produced the SAME line here, and the network
                    # sources swallowed their own errors so they never raised into the
                    # handler above. A wrong URL, an expired token, a 500 and a proxy
                    # block all read as a healthy source with nothing to say -- on a
                    # content pipeline that means ingestion silently stops.
                    if misses:
                        first = ", ".join(f"{tgt} ({why})" for tgt, why in misses[:2])
                        more = f" (+{len(misses) - 2} more)" if len(misses) > 2 else ""
                        log(f"  {name}: FAILED — could not read {len(misses)} target(s): "
                            f"{first}{more}", inline=True)
                        failed += 1
                    else:
                        log(f"  {name}: no documents (source reachable, nothing to ingest)",
                            inline=True)
                    continue
                # Documents are ABOUT a repo (`--for-repo`); link each one to the
                # symbols of that repo it names. Stored under this ingest
                # partition so a re-run's clear_repo above drops them with it.
                edges = link_documents_to_symbols(store, for_repo, nodes, texts,
                                                  "documented_by", "ingest")
                if for_repo and not edges:
                    # Asked to link and linked nothing: the documents name no symbol the
                    # graph knows. Not a failure -- but "0 edges" is the whole point of
                    # having passed --for-repo, so it cannot be silent.
                    log(f"  {name}: linked to no code in {for_repo!r} — no document "
                        f"named a symbol the graph knows", inline=True)
                store.clear_repo(repo_id)
                # Sweep this partition's vectors too, for the same reason the graph's
                # nodes are swept: the run is about to rewrite them. A document is now
                # SEVERAL vectors, so replace-by-key is no longer enough -- a page that
                # loses a section keeps the orphaned chunks of the text it no longer has
                # and can still be retrieved on them. It also drops the one whole-document
                # vector a pre-8.3 store holds under the bare node id, which would
                # otherwise sit alongside the new chunks and compete with them.
                # Placed after the `continue` above, like connect.py's sweep: only a
                # partition that is actually being rewritten gets cleared, so a source
                # that momentarily returns nothing does not wipe good vectors.
                if vs is not None:
                    vs.clear_repo(repo_id)
                store.upsert_nodes(repo_id, nodes)
                store.upsert_edges(repo_id, edges)
                write_shard(store_dir, GraphShard(repo=repo_id, head_commit="ingest",
                                                  nodes=nodes, edges=edges))
                total += len(nodes)
                msg = f"  {name}: {len(nodes)} document(s)"
                if misses:
                    # Partial: real documents came back AND targets were missed. Counted
                    # so the summary cannot call the run clean.
                    msg += f", {len(misses)} target(s) unreadable"
                    partial += len(misses)
                if embedder is not None and vs is not None:
                    n, n_vec, stopped = _embed_documents(
                        vs, embedder, repo_id, nodes, texts, cfg.embeddings.batch_size)
                    embedded += n
                    vectors += n_vec
                    if stopped:
                        # Named in the per-source line too, so the failure is visible
                        # without scrolling back to the warning.
                        msg += (f", EMBED INCOMPLETE: {n} of {len(nodes)} "
                                f"({stopped})")
                        partial += len(nodes) - n
                    else:
                        msg += (f", {n} embedded"
                                + (f" ({n_vec} chunks)" if n_vec != n else ""))
                log(msg, inline=True)
            # "as N chunks" only when it says something: a corpus of short documents
            # produces one chunk each, and the extra clause would be noise.
            tail = (f", {embedded} embedded into the semantic store"
                    + (f" as {vectors} chunk(s)" if vectors != embedded else "")
                    ) if embedded else ""
            # The summary states what could not be observed, and the exit code agrees
            # with it. Previously any run that produced a single document printed a
            # clean "Ingest complete" and exited 0, however many targets were
            # unreachable -- so a content pipeline degrading one source at a time was
            # invisible until somebody noticed the answers had got worse.
            if failed or partial:
                bits = []
                if failed:
                    bits.append(f"{failed} source(s) failed")
                if partial:
                    bits.append(f"{partial} target(s) unreadable")
                log(style.summary_line(
                    "warn", f"Ingest incomplete: {total} document(s) aggregated{tail} "
                            f"({'; '.join(bits)})"))
                # The flag is a PRE-command global, so the position has to be shown.
                # Naming it bare sends a user to `kb ingest --exit-zero-on-partial`,
                # which argparse rejects with exit 2 -- a worse outcome than the exit
                # code they were trying to suppress.
                log("  Nothing was silently dropped: each miss is logged above.")
                log("  For a scheduled run that should tolerate this:")
                log("    contextlake --exit-zero-on-partial kb ingest")
                return 0 if getattr(args, "exit_zero_on_partial", False) else 1
            log(style.summary_line("ok", f"Ingest complete: {total} document(s) aggregated{tail}"))
            return 0
        finally:
            if vs is not None:
                vs.close()
    finally:
        store.close()
