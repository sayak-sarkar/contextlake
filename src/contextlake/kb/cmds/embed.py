"""`contextlake kb embed` -- build the semantic-search vector index."""

from __future__ import annotations

import importlib.util

from ... import style
from ...logging_setup import log
from .._util import _or_default
from ._common import (
    _content_targets,
    _guard_store,
    _open_store,
    _watch_loop,
    kb_config,
)


def _embed_unavailable_hint(cfg) -> str:
    """Why is there no embedder, and the exact one-liner to fix it.

    `build_embedder` returns None either because semantic search is off (the
    opt-in default) or because it is on but no engine is installed/reachable.
    Either way the old "Embeddings are disabled" message left the user stuck — so
    spell out the install + enable steps, tailored to what is actually missing.
    """

    has_local = importlib.util.find_spec("model2vec") is not None
    if not getattr(cfg, "enabled", False):
        install = ("" if has_local
                   else "install the embedder (pip install 'contextlake[kb-full]'), then ")
        return ("Semantic search is off (opt-in). To enable it: " + install
                + 'set [embeddings] enabled = true in kb.toml (provider "auto"). '
                "The first embed downloads a small CPU model (~30MB, one-time).")
    if not has_local:
        return ("Embeddings are enabled but no embedder is installed — "
                "install one with: pip install 'contextlake[kb-full]' (built-in CPU model), "
                "or run a local Ollama.")
    return ("Embeddings are enabled but no embedder resolved (no local Ollama, and the built-in "
            "engine failed to load). Run `contextlake doctor` for details.")


def _staleness_key(store, store_dir, repo_id: str) -> str | None:
    """What "unchanged since last embed" means for ``repo_id``.

    A real repo has an indexed HEAD, and that is the key. A partition
    (``@connect:``/``@enrich:``/``@wiki:``/``@ingest:``) has no ``repos`` row and so no
    HEAD at all, which would make the incremental skip never fire and every run
    re-embed every partition from scratch.

    Borrowing the owning repo's HEAD would be worse than that, and wrong in the
    dangerous direction: connector content changes on its own schedule while the code
    sits still, so a matching code HEAD would report the partition as current and skip
    content that had in fact changed. ``@ingest:<name>`` has no owning repo to borrow
    from in any case.

    The shard file is rewritten whenever a partition's content changes, so its
    (mtime, size) is the honest key -- the same identity ``read_shard_with_identity``
    uses to key its own derived caches. Returns None when there is no shard, which
    leaves the caller re-embedding rather than skipping: the safe direction.
    """
    repo = store.get_repo(repo_id)
    if repo is not None and repo.head_commit:
        return repo.head_commit
    from ..store.shards import shard_path
    try:
        st = shard_path(store_dir, repo_id).stat()
    except (OSError, ValueError):
        return None
    return f"shard:{st.st_mtime_ns}:{st.st_size}"


def cmd_embed(args) -> int:
    from ..embeddings import build_embedder
    from ..embeddings.index import embed_repo
    from ..embeddings.store import build_vector_store

    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "embed"):
        store.close()
        return 1
    try:
        cfg = kb_config(args)
        embedder = build_embedder(cfg.embeddings)
        if embedder is None:
            log(_embed_unavailable_hint(cfg.embeddings))
            return 0
        targets = _content_targets(args, store)
        if not targets:
            log("Nothing indexed to embed (run index first, or pass --workspace/--source)")
            return 0
        limit = getattr(args, "limit", None)
        vs = build_vector_store(store_dir / "embeddings.sqlite",
                                backend=cfg.embeddings.vector_backend,
                                chunk_size=cfg.embeddings.vector_chunk_size)
        try:
            # Guard against re-embedding this store with a different model/dim (the
            # brute search silently drops mismatched dims). Probe the dim once; a
            # network failure here is left for the per-repo loop to report.
            from ..embeddings.store import guard_store_identity
            try:
                probe = embedder.embed(["contextlake"])
            except Exception:  # noqa: BLE001 - unreachable embedder; loop reports it
                probe = None
            if probe and probe[0]:
                identity = getattr(embedder, "identity", None) or getattr(
                    embedder, "name", "embedder")
                guard_store_identity(vs, identity, len(probe[0]))
            # Incremental: skip a repo whose indexed HEAD hasn't moved since it was
            # last embedded. `--force` re-embeds; `--limit` (partial) never gates.
            from ..embeddings.index import EMBED_CONTENT_VERSION
            from ..embeddings.store import (
                get_content_version,
                get_embedded_head,
                set_content_version,
                set_embedded_head,
            )
            force = getattr(args, "force", False)
            incremental = limit is None and not force
            # A node->text mapping change makes every stored vector stale regardless
            # of HEADs: one intentional full re-embed, then incremental resumes.
            stored_cv = get_content_version(vs)
            if incremental and vs.count() and stored_cv != EMBED_CONTENT_VERSION:
                log(f"Embedding text format changed (v{stored_cv} -> "
                    f"v{EMBED_CONTENT_VERSION}) — re-embedding everything once")
                incremental = False

            def _embed_once() -> int:
                # Re-resolve targets each pass so `--watch` picks up newly indexed repos.
                pass_targets = _content_targets(args, store)
                if not pass_targets:
                    log("Nothing indexed to embed (run index first, or pass --workspace/--source)")
                    return 0
                # Pre-flight: the builtin embedder loads its model lazily, so a
                # missing extra (or an unreachable Ollama/API endpoint) only surfaces
                # on first use. Probe once here so a whole-environment problem fails
                # fast with one actionable message, instead of repeating the same
                # error for every repo in the fleet.
                try:
                    embedder.embed(["contextlake embedder readiness probe"])
                except Exception as e:  # noqa: BLE001
                    # str(e) alone has repeatedly been ambiguous in the field --
                    # several unrelated failure modes (a bad model id, a stale
                    # local cache, a corporate proxy mangling the HF Hub request)
                    # all surface differently-shaped messages that read the same
                    # to a human. The exception's own class name is nearly free
                    # to add and turns "guess which of these it is" into a
                    # one-line answer.
                    log(style.warn(f"Embedder unavailable — {type(e).__name__}: {e}"))
                    log(style.dim("  No vectors written. Fix the embedder above, then "
                                  "re-run: contextlake kb embed"))
                    return 1
                # "source(s)", not "repo(s)": the work set now includes connector and ingested
                # partitions, so a fleet of 470 repos would otherwise report 940 "repos".
                log(f"Embedding {len(pass_targets)} source(s) with {embedder.name} "
                    f"into the {vs.name} vector store")
                total = failed = skipped = 0
                progress = style.Progress(len(pass_targets), label="embed")
                for repo_id in pass_targets:
                    head = _staleness_key(store, store_dir, repo_id)
                    if incremental and head and get_embedded_head(vs, repo_id) == head:
                        skipped += 1
                        progress.advance(repo_id)
                        continue
                    try:
                        n = embed_repo(store_dir, vs, embedder, repo_id,
                                       batch_size=cfg.embeddings.batch_size, limit=limit)
                    except Exception as e:  # noqa: BLE001 - one repo must not abort the run
                        log(f"  {repo_id}: embed failed ({e})", inline=True)
                        failed += 1
                        progress.advance(repo_id)
                        continue
                    if limit is None:
                        set_embedded_head(vs, repo_id, head)
                    total += n
                    if n:
                        log(f"  {repo_id}: embedded {n} node(s)", inline=True)
                    progress.advance(repo_id)
                progress.done()
                tail = f", {skipped} already up to date" if skipped else ""
                if failed:
                    tail += f", {failed} failed"
                state = "warn" if failed else "ok"
                log(style.summary_line(state, f"Embed complete: {total} vector(s) written "
                                              f"({vs.count()} total in store){tail}"))
                # Honest exit: if every repo we actually tried to embed failed (e.g. the
                # embedder went unreachable mid-run), this is a failure, not success.
                # Up-to-date repos that were skipped don't count as attempts.
                attempted = len(pass_targets) - skipped
                if attempted and failed == attempted:
                    log(style.warn(
                        f"Embed failed for all {attempted} source(s) — no vectors written"))
                    return 1
                if failed:
                    log("  See the log above for which repos failed. Re-run to retry.")
                # Only a full, failure-free pass earns the new content-version stamp;
                # a partial (--limit) or partly-failed pass must stay marked stale.
                if limit is None and failed == 0:
                    set_content_version(vs, EMBED_CONTENT_VERSION)
                return 0

            if getattr(args, "watch", False):
                interval = _or_default(getattr(args, "interval", None), 60)
                log(f"{style.cyan('watch')}: re-embedding every {interval}s (Ctrl-C to stop)")
                _watch_loop(_embed_once, interval=interval)
                return 0
            return _embed_once()
        finally:
            vs.close()
    finally:
        store.close()
