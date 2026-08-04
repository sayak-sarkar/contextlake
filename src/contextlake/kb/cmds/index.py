"""`contextlake kb index` -- walk a workspace, parse every repo into the graph."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from ... import style
from ...logging_setup import log
from ..config import load_kb_config
from ..model import Repo
from ..state import indexed_parser_version, mark_repo_indexed, needs_reindex
from ..store.shards import GraphShard, archive_shard, reindex_shard, write_shard
from ._common import (
    _git_head,
    _guard_store,
    _open_store,
    _watch_loop,
)


def _default_index_workers() -> int:
    return min(8, max(1, (os.cpu_count() or 2) - 1))


def _index_workspace(store, store_dir, workspace: Path, *, force: bool = False,
                     skip_generated: bool = True, max_file_bytes: int | None = None,
                     workers: int | None = None, repo_filter: str | None = None) -> int:
    from ..parse import (  # lazy: tree-sitter
        DEFAULT_MAX_FILE_BYTES,
        PARSER_VERSION,
        discover_repos,
        index_repo_dir,
    )

    if max_file_bytes is None:
        max_file_bytes = DEFAULT_MAX_FILE_BYTES
    from ..repo_migrate import migrate_stale_repo_ids
    migrate_stale_repo_ids(store, store_dir)
    repos = discover_repos(str(workspace))
    # --repos scopes indexing to a subset (so `bootstrap --repos ...` indexes only the
    # mirrored subset, even when the workspace holds the full fleet). repo_id is now
    # canonical (from the remote, not the local path -- see repo_identity.py), so a
    # path-shaped filter like "team/api" needs the local workspace-relative path too,
    # not just the canonical id, to keep matching what a user actually types.
    if repo_filter:
        from ...core import match_repo_filter, repo_filter_patterns
        patterns = repo_filter_patterns({"repo_filter": repo_filter})
        if patterns:
            def _local(path: str) -> str:
                try:
                    return Path(path).relative_to(workspace).as_posix()
                except ValueError:
                    return path

            repos = [(rid, path) for rid, path in repos
                     if match_repo_filter(rid, _local(path), patterns)]
    if not repos:
        # An empty workspace must fail loudly: an agent cannot cite from an empty
        # graph, so "success" here would be the hollow kind.
        extra = f" matching --repos {repo_filter!r}" if repo_filter else ""
        log(style.warn(f"No git repositories found under {workspace}{extra} — "
                       "nothing indexed."))
        log("  Point --workspace at a directory that contains git clones.")
        return 1
    mode = "full" if force else "incremental"
    failed = skipped = 0

    # Incremental filter first (cheap serial DB reads): repos whose HEAD moved, plus
    # repos whose graph was built by a parser this build no longer agrees with. The
    # second test is the difference between "unchanged" and "still current": a repo
    # sitting at the same commit but indexed by an older parser holds a graph this
    # build would not produce, and answering questions from it is precisely the
    # confident-but-wrong failure this tool exists to prevent. So it is re-indexed,
    # not merely reported -- the alternative is a green "unchanged" over a stale
    # graph, and no amount of wording makes that safe. Announced below, because the
    # first index after a parser bump doing real work must not come as a surprise.
    todo = []
    stale_parser: dict[str, int] = {}
    for repo_id, path in repos:
        head = _git_head(Path(path))
        if force or needs_reindex(store, repo_id, head):
            todo.append((repo_id, path, head))
            continue
        was = indexed_parser_version(store, store_dir, repo_id)
        if was != PARSER_VERSION:
            stale_parser[was or "unknown"] = stale_parser.get(was or "unknown", 0) + 1
            todo.append((repo_id, path, head))
        else:
            skipped += 1
    total = len(todo)
    progress = style.Progress(total=total, label="index")
    if workers is None or workers <= 0:
        workers = _default_index_workers()
    log(f"Found {len(repos)} repositories under {workspace} ({mode}); "
        f"indexing {total} with {workers} worker(s)")
    if stale_parser:
        n = sum(stale_parser.values())
        froms = ", ".join(sorted(stale_parser))
        log(f"{style.warn()} {n} repo(s) unchanged since their last index were built by an "
            f"older parser ({froms} -> {PARSER_VERSION}); re-indexing them, because the graph "
            f"they hold is not the one this build produces.")

    def _persist(repo_id, path, head, shard):
        store.upsert_repo(Repo(id=repo_id, path=path))
        write_shard(store_dir, shard)
        archive_shard(store_dir, shard)
        reindex_shard(store, store_dir, repo_id)
        # Stamp from the shard, never from PARSER_VERSION: the row then mirrors
        # the file that was actually written, so the two cannot drift.
        mark_repo_indexed(store, repo_id, head, shard.parser_version)

    def _report(repo_id, shard):
        progress.advance(repo_id)
        log(f"  {style.ok(repo_id)}: {len(shard.nodes)} nodes, "
            f"{len(shard.edges)} edges", inline=True)

    def _run_serial(items):
        nonlocal failed
        for repo_id, path, head in items:
            try:
                shard = index_repo_dir(path, repo_id, head_commit=head,
                                       skip_generated=skip_generated, max_file_bytes=max_file_bytes)
            except Exception as e:  # noqa: BLE001 - one repo must not abort the workspace
                failed += 1
                log(f"  {style.fail(repo_id)}: {e}", inline=True)
                continue
            try:
                _persist(repo_id, path, head, shard)
            except Exception as e:  # noqa: BLE001 - one repo's write must not abort the fleet
                failed += 1
                log(f"  {style.fail(repo_id)}: persist failed: {e}", inline=True)
                continue
            _report(repo_id, shard)

    if workers <= 1 or total <= 1:
        _run_serial(todo)
    else:
        # Parse repos in parallel (CPU-bound); persist serially here, since SQLite
        # must be written from a single process. Use the `spawn` start method on
        # every platform so behaviour and efficiency are identical on Linux, macOS
        # and Windows (Windows has only spawn; macOS defaults to it; on Linux spawn
        # benchmarks the same as fork). Workers re-import the package, which is safe
        # because every entry point is __main__-guarded; the per-worker startup is a
        # one-time cost amortised across the whole repo set.
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor, as_completed
        from concurrent.futures.process import BrokenProcessPool

        ctx = mp.get_context("spawn")
        try:
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
                futs = {
                    ex.submit(index_repo_dir, path, repo_id, head, None,
                              max_file_bytes=max_file_bytes, skip_generated=skip_generated):
                        (repo_id, path, head)
                    for repo_id, path, head in todo
                }
                for fut in as_completed(futs):
                    repo_id, path, head = futs[fut]
                    try:
                        shard = fut.result()
                    except Exception as e:  # noqa: BLE001 - one repo must not abort the workspace
                        failed += 1
                        log(f"  {style.fail(repo_id)}: {e}", inline=True)
                        continue
                    try:
                        _persist(repo_id, path, head, shard)
                    except Exception as e:  # noqa: BLE001 - one repo's write must not abort the fleet
                        failed += 1
                        log(f"  {style.fail(repo_id)}: persist failed: {e}", inline=True)
                        continue
                    _report(repo_id, shard)
        except (BrokenProcessPool, OSError) as e:
            # The worker pool could not run here (sandboxed env, no fork/spawn, …).
            # Re-run the full work-list serially — persist is upsert-based and
            # idempotent, so repos already written simply update in place.
            log(f"{style.warn()} Parallel indexing unavailable ({e}); "
                f"falling back to serial.")
            # Fresh Progress: some repos may already have advanced the old one
            # via _report before the pool broke, so re-running the full work-list
            # serially (see comment above) must not double-advance those counts.
            progress = style.Progress(total=total, label="index")
            _run_serial(todo)
    progress.done()
    st = store.stats()
    glyph = style.ok() if failed == 0 else style.warn()
    log(f"{glyph} Workspace indexed: {st.repos} repos, {st.nodes} nodes, "
        f"{st.edges} edges ({skipped} unchanged, {failed} failed)")
    if failed:
        log("  See the log above for which repos failed. Re-run to retry -- "
            "indexing is incremental, so only the unindexed/changed repos run again.")
    return 0 if failed == 0 else 1


def _store_and_index(store, store_dir, repo_id, repo_path, head, shard) -> int:
    store.upsert_repo(Repo(id=repo_id, path=str(repo_path)))
    write_shard(store_dir, shard)
    archive_shard(store_dir, shard)
    reindex_shard(store, store_dir, repo_id)
    mark_repo_indexed(store, repo_id, head, shard.parser_version)
    st = store.stats()
    log(f"Indexed {repo_id}: {len(shard.nodes)} nodes, {len(shard.edges)} edges "
        f"(store totals: {st.nodes} nodes, {st.edges} edges)")
    return 0


def cmd_index(args) -> int:
    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "index"):
        store.close()
        return 1
    cfg = load_kb_config(getattr(args, "config", None))
    parse_opts = dict(skip_generated=cfg.skip_generated, max_file_bytes=cfg.max_file_bytes)
    workers = cfg.index_workers
    try:
        workspace = getattr(args, "workspace", None)
        if workspace:
            force = getattr(args, "force", False)
            repo_filter = getattr(args, "repos", None)
            if getattr(args, "watch", False):
                interval = getattr(args, "interval", None) or 60
                log(f"{style.cyan('watch')}: re-indexing {workspace} every "
                    f"{interval}s (Ctrl-C to stop)")
                _watch_loop(
                    lambda: _index_workspace(store, store_dir, Path(workspace),
                                             force=force, workers=workers,
                                             repo_filter=repo_filter, **parse_opts),
                    interval=interval,
                )
                return 0
            return _index_workspace(store, store_dir, Path(workspace),
                                    force=force, workers=workers,
                                    repo_filter=repo_filter, **parse_opts)

        # `contextlake kb index PATH` and `index --source PATH` are the same thing.
        source = getattr(args, "source", None) or getattr(args, "path", None)
        if not source:
            # Zero-config: with no path/--source/--workspace, index the current
            # directory so `cd my-repo && contextlake kb index` just works.
            source = "."
            log(f"No --source/--workspace given; indexing the current directory "
                f"({Path(source).resolve()}). Pass --source PATH or --workspace DIR "
                f"to index elsewhere.")
        src = Path(source)

        if src.is_dir():
            from ..parse import index_repo_dir  # lazy: only needs tree-sitter when indexing code

            if not (src / ".git").exists():
                nested = [p.parent.name for p in src.glob("*/.git")]
                if nested:
                    log(style.warn(
                        f"{src.resolve()} isn't itself a git repo, but contains "
                        f"{len(nested)} that are ({', '.join(sorted(nested)[:5])}"
                        f"{', …' if len(nested) > 5 else ''}). Indexing it this way "
                        "bundles everything underneath into ONE repo -- if this is a "
                        "workspace mirroring several repos, use "
                        "`contextlake kb index --workspace .` instead, which indexes each "
                        "nested repo separately under its own identity."
                    ))
            repo_id = getattr(args, "repo", None) or src.resolve().name  # "." -> cwd name
            head = _git_head(src)
            shard = index_repo_dir(str(src), repo_id, head_commit=head, **parse_opts)
            return _store_and_index(store, store_dir, repo_id, src.resolve(), head, shard)

        # otherwise treat --source as a graph-shard JSON file
        try:
            raw = src.read_text(encoding="utf-8")
        except OSError as e:
            log(f"Cannot read source {source!r}: {e}")
            return 1
        try:
            shard = GraphShard.model_validate_json(raw)
        except ValidationError as e:
            log(f"{source!r} is not a valid graph shard ({e.error_count()} error(s)); "
                "expected a JSON object with repo, nodes, and edges")
            return 1
        return _store_and_index(
            store, store_dir, shard.repo, src.resolve(), shard.head_commit, shard
        )
    finally:
        store.close()
