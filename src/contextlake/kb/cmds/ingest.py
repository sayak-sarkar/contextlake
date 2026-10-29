"""`contextlake kb ingest` -- aggregate external documents into the graph."""

from __future__ import annotations

import logging

from ... import style
from ...logging_setup import log
from ..connectors.text_match import link_documents_to_symbols, symbol_nodes_for_repo
from ..paths import is_plain_id
from ..store.shards import GraphShard, store_partition, write_shard
from ._common import (
    _guard_store,
    _open_store,
    kb_config,
)


def _embedder_identity(embedder) -> str:
    return getattr(embedder, "identity", None) or getattr(embedder, "name", "embedder")


class _EmbedderRefused(ValueError):
    """The store's identity guard refused the embedder (a changed model or dimension)."""


def _check_embedder(vs, embedder) -> tuple[bool, str | None]:
    """Run one probe vector from ``embedder`` through the identity guard of ``vs``.

    Returns ``(checked, refusal)``: ``refusal`` is why the embedder may not write to the
    store, or None; ``checked`` is True only when a vector was taken and the guard ran.
    An embedder that cannot be reached, or answers with no vector, is not refused, but
    nothing was proved either, so ``checked`` is False and the caller must not act as if
    the guard had passed. The embed phase reports an embedder that stays down.

    The identity guard used to run inside `_embed_documents`, after the partition's
    vectors had been cleared and its rows rewritten. A changed `[embeddings]` model
    therefore deleted every vector of the partition and then refused the run. One probe
    vector, taken before anything is cleared, is how `kb embed` orders the same steps.
    """
    from ..embeddings.store import guard_store_identity

    try:
        probe = embedder.embed(["contextlake"])
    except Exception:  # noqa: BLE001 - unreachable embedder; the embed phase reports it
        return False, None
    if not (probe and probe[0]):
        return False, None
    try:
        guard_store_identity(vs, _embedder_identity(embedder), len(probe[0]))
    except ValueError as e:
        return True, str(e)
    return True, None


def _embedder_refused(vs, embedder) -> bool:
    """Probe ``embedder`` against the identity guard of ``vs``; log why and return True if refused.

    For a command that clears a partition's vectors before it embeds (`kb enrich`,
    `kb wiki`). Called before the command's first write, so on True the caller returns 1
    and the stored graph, files and vectors are as the last run left them. False when the
    guard passed, and also when the probe got no answer: `_check_embedder` explains why that
    is not a pass.
    """
    _checked, refusal = _check_embedder(vs, embedder)
    if not refusal:
        return False
    log(style.fail(refusal))
    log("  This run changed nothing: the graph, the files and the vectors are as the last "
        "run left them.")
    return True


def _embed_documents(vs, embedder, repo_id, nodes, texts, batch_size, sweep=False
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

    ``sweep`` clears the partition's vectors here, after the first batch has passed the
    identity guard, for a caller that could not check the embedder before it cleared.
    `cmd_ingest` passes it only while no vector has passed the guard in the run: once one
    source has embedded, each later source is cleared before it writes. Within this call
    an embedder that never answers clears nothing, and a refused one leaves the vectors as
    they were. The one exception is a source whose document bodies are all empty: no batch
    runs, so the sweep clears the partition at the end, because the old vectors belong to
    text that no longer exists. `cmd_ingest` is the only caller that passes `sweep`. It
    clears before the call only after a probe or an earlier source has passed the guard, so
    a refusal after that clear means the first batch came back with another vector length.
    `kb enrich` and `kb wiki` clear before they call this. Each probes the embedder first
    (`_embedder_refused`), so a changed model is refused before the clear. When that probe
    got no answer they clear anyway, and an identity refusal here can still follow the clear.
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
    swept = not sweep
    first_len: int | None = None
    for i in range(0, len(units), batch_size):
        batch = units[i:i + batch_size]
        try:
            vecs = embedder.embed([text for _key, text, _node in batch])
            if (len(vecs or ()) != len(batch) or not vecs[0]
                    or any(len(v) != len(vecs[0]) for v in vecs)):
                # A short reply, an empty vector or a vector of another length: the guard
                # checks only the first vector, and the strict zip or the vector store
                # would raise after the sweep had run.
                raise ValueError("embedder returned no usable vector for this batch")
            if first_len is not None and len(vecs[0]) != first_len:
                # The guard runs on the first batch only. A later batch of another length
                # made sqlite-vec raise out of `upsert` (outside this try, so the run died
                # with a bare error) and made the brute store write a row its search skips.
                raise ValueError(f"embedder returned vectors of length {len(vecs[0])} "
                                 f"after vectors of length {first_len}")
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
        if i == 0:
            try:
                guard_store_identity(vs, _embedder_identity(embedder), len(vecs[0]))
            except ValueError as e:
                raise _EmbedderRefused(str(e)) from e
            first_len = len(vecs[0])
        if not swept:
            vs.clear_repo(repo_id)
            swept = True
        vs.upsert((key, repo_id, v)
                  for (key, _text, _node), v in zip(batch, vecs, strict=True))
        written_vectors += len(batch)
        embedded_nodes.update(id(node) for _key, _text, node in batch)
    if not swept and stopped_early is None:
        # Nothing to embed (every body is empty): the old vectors still have to go.
        vs.clear_repo(repo_id)
    return len(embedded_nodes), written_vectors, stopped_early


def _distinct_id(doc_id: str, taken: set[str], reserved: frozenset[str] = frozenset()) -> str:
    """``doc_id``, or ``doc_id#N`` (N counting up from 2) when ``taken`` already holds it.

    A node id is the partition plus the document id, so two documents of one source with
    the same id are ONE node: the later replaced the earlier, while the summary counted
    both. A suffix skips every id in ``reserved`` (the ids the source's own records carry),
    so a real id such as ``a#2`` never moves because an earlier record repeated ``a``. `#`
    is safe as a suffix: the chunk separator in a vector key is a control character
    (`embeddings.store.chunk_key`), so a suffixed id never reads as a chunk key.
    The result is added to ``taken``.
    """
    unique, n = doc_id, 2
    while unique in taken or (unique != doc_id and unique in reserved):
        unique = f"{doc_id}#{n}"
        n += 1
    taken.add(unique)
    return unique


def _previous_documents(store_dir, repo_id: str, fresh: list) -> tuple[list, list]:
    """The documents and edges a source's last run stored that ``fresh`` does not replace.

    Used when a run could not read every target. The partition is rewritten from what came
    back, so a page that failed this time (one 500 is enough) would lose the document the
    last run stored for it, and its vectors with it. Keeping the rest by id leaves that
    document in place until a run reads every target. A document that was removed from the
    source stays for as long as some target keeps failing, and goes on the next clean run.
    Returns ``([], [])`` when there is no earlier shard, or one that cannot be read.
    """
    from ..store.shards import read_shard

    try:
        previous = read_shard(store_dir, repo_id)
    except (OSError, ValueError):
        return [], []
    if previous is None:
        return [], []
    replaced = {n.id for n in fresh}
    nodes = [n for n in previous.nodes if n.id not in replaced]
    kept = {n.id for n in nodes}
    return nodes, [e for e in previous.edges if e.dst in kept]


# The option each source type reads its target from. `files` reads `path`; the network
# sources read `url`. `--path https://...` used to be passed as `path` for every type, which
# `web` and `api` swallow in `**_`, so the run dialled nothing and ended "complete".
# `graphql` is left out: it needs a `query` as well, and `--path` cannot supply one.
_URL_TARGET_TYPES = frozenset({"web", "api"})


def _cli_target_key(source_type: str, target: str) -> str:
    """The source option that ``--path TARGET`` sets for ``source_type``."""
    if source_type in _URL_TARGET_TYPES:
        return "url"
    # `mcp` takes a URL or a command; only a URL can be told from a plain path.
    if source_type == "mcp" and target.lower().startswith(("http://", "https://")):
        return "url"
    return "path"


def _own_source_hint(source_type: str, target: str) -> str:
    """The command that adds a ``--path TARGET`` run as a source of its own."""
    key = _cli_target_key(source_type, target)
    return (f"contextlake kb source add NAME --type {source_type} "
            f"--set {key}={'URL' if key == 'url' else 'PATH'}")


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
            cli_type = getattr(args, "source_type", None) or "files"
            jobs.append(("cli", cli_type, {_cli_target_key(cli_type, cli_path): cli_path},
                         getattr(args, "for_repo", None) or None))
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
                    "warn", f"{name}: {for_repo!r} has no indexed symbols - its documents "
                            "will be stored but linked to nothing (index that repo first)"))

        # Seeded with the sources this build could not construct at all. They are as
        # much a failure of the run as a source that raised: the config asked for them
        # and the run does not carry their documents. Counting them only in the warning
        # above would leave the summary line and the exit code saying the run was clean.
        total = embedded = partial = unembedded = 0
        # Vectors are counted apart from documents. With chunking the two differ by an
        # order of magnitude, and printing vectors where a reader expects documents
        # would report a corpus many times the size of the real one.
        vectors = 0
        failed = len(unavailable)
        probed = guarded = refused = False
        cleared_source: str | None = None
        not_run: list[str] = []
        try:
            for idx, (name, stype, options, for_repo) in enumerate(jobs):
                try:
                    src = build_source(stype, **options)
                except Exception as e:  # noqa: BLE001 - one bad option must not stop the other sources
                    log(f"  {name}: source failed ({e})", inline=True)
                    failed += 1
                    continue
                if src is None:
                    log(f"  {name}: unknown source type {stype!r}, skipping", inline=True)
                    failed += 1
                    continue
                repo_id = f"@ingest:{name}"
                if not is_plain_id(repo_id):
                    # The name becomes a path under the store. Checked before the fetch, so
                    # nothing is fetched or stored for a name `write_shard` would refuse.
                    log(f"  {name}: refused, a source name may not hold a '.' or '..' "
                        "segment or be a path", inline=True)
                    failed += 1
                    continue
                nodes, texts = [], []
                taken: set[str] = set()
                repeated = 0
                try:
                    docs = list(src.iter_documents())
                    reserved = frozenset(d.id for d in docs)
                    for doc in docs:
                        doc_id = _distinct_id(doc.id, taken, reserved)
                        if doc_id != doc.id:
                            repeated += 1
                        nodes.append(Node(id=f"{repo_id}:{doc_id}", repo=repo_id,
                                          kind="document", name=doc.title,
                                          file=(doc.uri or None),
                                          attrs={**doc.attrs, "source": stype}))
                        texts.append(doc.text)
                except Exception as e:  # noqa: BLE001 - one source must not abort the run
                    log(f"  {name}: source failed ({e})", inline=True)
                    failed += 1
                    continue
                if repeated:
                    fix = (". Set id_field to a field that is unique per record"
                           if stype in ("api", "graphql") else "")
                    log(f"  {name}: {repeated} document(s) repeated the id of an earlier "
                        f"one, so they are stored as <id>#N, with N counting up from 2 and "
                        f"skipping any id another record of the source has{fix}",
                        inline=True)
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
                        log(f"  {name}: FAILED - could not read {len(misses)} target(s): "
                            f"{first}{more}", inline=True)
                        failed += 1
                    elif getattr(src, "no_request_reason", None):
                        # No request went out, so nothing was shown about the endpoint.
                        # "Reachable" is a claim about one, and only a request makes it.
                        if cli_path:
                            # The target was typed on the command line, so dropping it is a
                            # failure. A [[sources]] entry may sit idle on purpose.
                            log(f"  {name}: FAILED, nothing was sent for {cli_path!r}: "
                                f"{src.no_request_reason}. `--path` sets one option; use a "
                                "[[sources]] entry in kb.toml to set more", inline=True)
                            failed += 1
                        else:
                            log(f"  {name}: nothing to ingest: {src.no_request_reason}",
                                inline=True)
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
                    log(f"  {name}: linked to no code in {for_repo!r} - no document "
                        f"named a symbol the graph knows", inline=True)
                if vs is not None and not probed:
                    # Before the first vector is cleared or row written. Every source shares
                    # the one store, so a refusal here applies to all of them and the run
                    # ends: nothing has changed yet, and each source would refuse the same.
                    probed = True
                    guarded, refusal = _check_embedder(vs, embedder)
                    if refusal:
                        log(style.fail(refusal))
                        log("  This run changed nothing: the documents and vectors are as "
                            "the last run left them.")
                        return 1
                # Sweep this partition's vectors, for the same reason the graph's nodes
                # are swept: the run is about to rewrite them. A document is now SEVERAL
                # vectors, so replace-by-key is no longer enough -- a page that loses a
                # section keeps the orphaned chunks of the text it no longer has and can
                # still be retrieved on them. It also drops the one whole-document vector
                # a pre-8.3 store holds under the bare node id, which would otherwise sit
                # alongside the new chunks and compete with them.
                # Placed after the `continue` above, like connect.py's sweep: only a
                # partition that is actually being rewritten gets cleared, so a source
                # that momentarily returns nothing does not wipe good vectors.
                # Vectors FIRST, then the shard, then the rows in one transaction: the
                # order `forget` and `kb index` use. That holds once a vector has passed
                # the guard (`guarded`: the probe answered, or an earlier source embedded
                # one). Before that, a source writes the shard and rows first and its sweep
                # waits for its first batch (see `sweep` below). A stop after the vector
                # sweep leaves documents with no vectors, which re-embedding repairs and
                # which shows as a missing semantic hit. The old order (rows first) left
                # vectors that answer queries for documents the partition no longer holds.
                # A run that missed targets keeps the documents it did not re-read (see
                # `_previous_documents`), so it must not sweep their vectors either. Each
                # fresh document overwrites its own chunk keys; a document that shrank
                # keeps its trailing chunks until the next run that reads every target.
                previous, previous_edges = (
                    _previous_documents(store_dir, repo_id, nodes)
                    if misses or cli_path else ([], []))
                kept_nodes, kept_edges = (previous, previous_edges) if misses else ([], [])
                # Cleared here only once a vector has passed the identity guard in this run:
                # the probe answered, or an earlier source embedded one. Until then
                # `_embed_documents` clears after the source's first batch passes, or at its
                # end when no body has text.
                sweep = vs is not None and not kept_nodes
                cleared = sweep and guarded
                if cleared:
                    vs.clear_repo(repo_id)
                write_shard(store_dir, GraphShard(repo=repo_id, head_commit="ingest",
                                                  nodes=nodes + kept_nodes,
                                                  edges=edges + kept_edges))
                store_partition(store, repo_id, nodes + kept_nodes, edges + kept_edges)
                if cli_path and previous and not misses:
                    # Every `--path` run writes the one `cli` partition, so a run on another
                    # folder replaced the first folder's documents and printed a clean
                    # `1 document(s)`. Said after the write: a failed write replaced nothing.
                    gone = [n.id.removeprefix(f"{repo_id}:") for n in previous]
                    more = f" (+{len(gone) - 3} more)" if len(gone) > 3 else ""
                    log(f"  {name}: replaced {len(gone)} document(s) the last run stored "
                        f"and this run did not read: {', '.join(gone[:3])}{more}. A --path "
                        "run owns the whole set. To keep several, add each as its own "
                        f"source: {_own_source_hint(stype, cli_path)}", inline=True)
                total += len(nodes)
                msg = f"  {name}: {len(nodes)} document(s)"
                if kept_nodes:
                    msg += f", kept {len(kept_nodes)} from the last run"
                if misses:
                    # Partial: real documents came back AND targets were missed. Counted
                    # so the summary cannot call the run clean.
                    msg += f", {len(misses)} target(s) unreadable"
                    partial += len(misses)
                if embedder is not None and vs is not None:
                    try:
                        n, n_vec, stopped = _embed_documents(
                            vs, embedder, repo_id, nodes, texts, cfg.embeddings.batch_size,
                            sweep=sweep and not guarded)
                    except _EmbedderRefused as e:
                        # The identity guard raises on the first batch. With the probe
                        # unanswered the sweep waits behind it, so the vectors are as they
                        # were. After an answered probe, or once an earlier source passed,
                        # the sweep above has run (`cleared`), and the refusal means this
                        # batch came back with another vector length than the probe or that
                        # source gave.
                        # The documents above are stored either way.
                        # Every source shares the vector store, so the ones after this one
                        # would be refused too, and the run ends.
                        log(f"{msg}, EMBED REFUSED: {e}", inline=True)
                        failed += 1
                        refused = True
                        cleared_source = name if cleared else None
                        not_run = [job[0] for job in jobs[idx + 1:]]
                        break
                    embedded += n
                    vectors += n_vec
                    guarded = guarded or n_vec > 0
                    if stopped:
                        # Named in the per-source line too, so the failure is visible
                        # without scrolling back to the warning.
                        msg += (f", EMBED INCOMPLETE: {n} of {len(nodes)} "
                                f"({stopped})")
                        # Stored but not embedded: a different cause from `partial`, which
                        # counts targets that could not be read, so the summary names each.
                        unembedded += len(nodes) - n
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
            if failed or partial or unembedded:
                bits = []
                if failed:
                    bits.append(f"{failed} source(s) failed")
                if partial:
                    bits.append(f"{partial} target(s) unreadable")
                if unembedded:
                    bits.append(f"{unembedded} document(s) not embedded")
                if not_run:
                    bits.append(f"{len(not_run)} source(s) not run")
                if refused:
                    more = f" (+{len(not_run) - 3} more)" if len(not_run) > 3 else ""
                    log((f"  The vectors of {cleared_source} were cleared before the refusal. "
                         "Run `kb ingest` again to rebuild them."
                         if cleared_source else "  The vectors were kept.")
                        + (f" Not run, because every source shares the vector store: "
                           f"{', '.join(not_run[:3])}{more}." if not_run else ""))
                log(style.summary_line(
                    "warn", f"Ingest incomplete: {total} document(s) aggregated{tail} "
                            f"({'; '.join(bits)})"))
                if refused:
                    # A changed embedder is a setting to fix, not a miss to tolerate, so the
                    # flag below does not apply. The run that probed first exits 1 the same way.
                    return 1
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
