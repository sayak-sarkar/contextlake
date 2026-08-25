"""`contextlake kb index` -- walk a workspace, parse every repo into the graph."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from pydantic import ValidationError

from ... import observability, style
from ...logging_setup import log
from .._util import _or_default
from ..model import Repo
from ..state import indexed_parser_version, mark_repo_indexed, needs_reindex
from ..store.shards import GraphShard, archive_shard, reindex_shard, write_shard
from ._common import (
    _git_head,
    _guard_store,
    _open_store,
    _repo_id_suggestions,
    _watch_loop,
    kb_config,
)


def _default_index_workers() -> int:
    return min(8, max(1, (os.cpu_count() or 2) - 1))


def _index_workspace(store, store_dir, workspace: Path, *, force: bool = False,
                     skip_generated: bool = True, max_file_bytes: int | None = None,
                     workers: int | None = None, repo_filter: str | None = None,
                     languages: list[str] | None = None) -> int:
    from ..parse import (  # lazy: tree-sitter
        DEFAULT_MAX_FILE_BYTES,
        PARSER_VERSION,
        discover_repos,
        index_repo_dir,
    )

    if max_file_bytes is None:
        max_file_bytes = DEFAULT_MAX_FILE_BYTES
    # Discover FIRST, then migrate, and tell the migration what this run will index.
    # The order is the fix, not a tidy-up: the migration DELETES a repo whose stored id
    # is not canonical, on the promise that the loop below re-indexes it immediately.
    # Called before discovery it could not know whether that promise applied, so a run
    # pointed at a different workspace silently destroyed repos it was never going to
    # touch -- observed on a real store, with its shards and vectors gone too.
    from ..repo_migrate import migrate_stale_repo_ids
    # Collected, not merely logged: a directory git cannot open was warned about and then
    # dropped from discovery, so the tally below counted only what survived and reported
    # "0 failed" with exit 0. `docs/connecting-and-enriching.md` promises the opposite verdict.
    unusable: list[str] = []
    repos = discover_repos(str(workspace), unusable=unusable)
    # discover_repos returns (repo_id, path) PAIRS -- pass the paths, not the pairs.
    migrate_stale_repo_ids(store, store_dir, in_scope=[p for _rid, p in repos])
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
            # Scope the unreadable list by the SAME filter. Collected during discovery,
            # which runs before `--repos` is applied, so reporting it unscoped made
            # `kb index --workspace W --repos good` exit 1 over a broken directory the run
            # was explicitly told not to touch: an aggregate spanning a filter, presented
            # as the run's own result -- the exact defect this release is about, introduced
            # by the fix for it.
            unusable = [rel for rel in unusable
                        if match_repo_filter(rel, rel, patterns)]
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
    # Registered from the WALK, not from the store. _open_store registers whatever
    # the store already knows, which is empty on a first index -- exactly the run
    # that prints every repo id for the first time, and so exactly the run whose
    # log leaked them. The walk knows the ids before a single line is emitted.
    observability.add_repo_names(rid for rid, _ in repos)

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
                shard = index_repo_dir(path, repo_id, head_commit=head, languages=languages,
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
                    # 4th positional IS `languages`; it was hardcoded None here, which
                    # is half of why the config key did nothing.
                    ex.submit(index_repo_dir, path, repo_id, head, languages,
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
    # Scoped to *this* workspace's own repo list (the same `repos` the "Found N
    # repositories under ..." line above counted), not store.stats() -- that is
    # store-wide and would let an unrelated repo from a *different* --workspace
    # run (or a stale/duplicate id) inflate a line labelled "Workspace indexed".
    # repo_counts() matches the literal repo_id partition (same scoping
    # clear_repo/delete_repo use), so these numbers are exactly "what's in the
    # store for the repos this run just discovered under this workspace".
    ws_nodes = ws_edges = 0
    for repo_id, _ in repos:
        n, e = store.repo_counts(repo_id)
        ws_nodes += n
        ws_edges += e
    glyph = style.ok() if (failed == 0 and not unusable) else style.warn()
    log(f"{glyph} Workspace indexed: {len(repos)} repos, {ws_nodes} nodes, "
        f"{ws_edges} edges ({skipped} unchanged, {failed} failed"
        + (f", {len(unusable)} unreadable" if unusable else "") + ")")
    if failed:
        log("  See the log above for which repos failed. Re-run to retry -- "
            "indexing is incremental, so only the unindexed/changed repos run again.")
    if unusable:
        # Named, because "unreadable" and "failed to parse" are different repairs: this one
        # is fixed with a re-clone or a removal, not by re-running the index.
        log(f"  {len(unusable)} director(y/ies) could not be read as a git repository and were "
            f"NOT indexed: {', '.join(sorted(unusable)[:8])}"
            + (" ..." if len(unusable) > 8 else "")
            + ". Re-clone or remove them; re-running the index cannot help.")
    # A repo the graph is missing is a graph an agent will cite from that is not the one you
    # asked for, which is the same verdict `kb connect` gives for a skipped source.
    return 0 if (failed == 0 and not unusable) else 1


def _nested_repo_dirs(src: Path) -> list[Path]:
    """Every git working tree under ``src``, at any depth.

    The bundling check used to ask ``src.glob("*/.git")``, which matches direct
    children only. A fleet mirrored one level down --
    ``<workspace>/repositories/<repo>/.git`` -- was therefore invisible, and the
    warning reported "contains 1" where the truth was 20. The count is the whole
    point of that message: "1" reads as an edge case worth skipping past, "20" is
    a stop sign, so undercounting it by 95% muted the warning at exactly the
    moment it needed to be loudest.

    Shares :func:`~contextlake.kb.parse.iter_repo_dirs` with ``discover_repos``,
    so this count and the set ``--workspace`` would actually walk cannot drift
    apart, and applies the same vendored-clone exclusion for the same reason
    (a pure path test, no git calls, so it stays affordable on a large fleet).
    """
    from ..parse import is_vendored_repo, iter_repo_dirs  # lazy: tree-sitter

    return [p for p in iter_repo_dirs(src) if not is_vendored_repo(src, p)]


def _depth_phrase(depths: set[int]) -> str:
    """"at depth 2" / "at depths 1-3" -- how deep the nested repos sit, which is
    the part that explains why a shallower look would have missed them."""
    lo, hi = min(depths), max(depths)
    return f"at depth {lo}" if lo == hi else f"at depths {lo}-{hi}"


# How much indexable content outside the nested repositories makes bundling the
# right answer rather than the wrong one. Two tests, because neither holds alone:
# an absolute floor, since a handful of files at the top of a mirror is
# scaffolding (a setup.py, a couple of helper scripts) and not a codebase; and a
# share of the total, since a fleet holding tens of thousands of files inside its
# repositories is still a fleet with a hundred loose ones next to them, and a rule
# tuned only to small trees would send a large one back to bundling.
#
# Both are deliberately generous towards bundling. Refusing wrongly leaves a user
# unable to index anything, which reads as a broken tool; bundling wrongly is the
# behaviour that shipped for years, is now stated out loud, and is one flag away
# from being overridden either way.
_LOOSE_FILES_TRIVIAL = 10
_LOOSE_FILES_SHARE = 0.05

_WORKSPACE = "workspace"
_ONE_LEVEL_TOO_HIGH = "one-level-too-high"
_BUNDLE = "bundle"


def _bundle_shape(src: Path, nested: list[Path]) -> tuple[str, int]:
    """Which of three shapes ``src`` is, and how much content sits outside its
    nested repositories.

    The discriminator is that second number, because the failure mode being
    avoided is asymmetric. Prescribing ``--workspace`` indexes each nested
    repository and *nothing outside one*, so on a tree of the user's own loose
    sources that happens to carry a dependency with its own ``.git`` it would
    index the dependency and silently drop the sources -- strictly worse than the
    bundling it replaced, which at least captures them. Measuring the shape first
    is what makes "refuse and prescribe" safe where "infer and act" is not.

    Returns ``(shape, loose)``: ``_WORKSPACE`` (index each repository under its
    own identity), ``_ONE_LEVEL_TOO_HIGH`` (index the single repository directly)
    or ``_BUNDLE`` (there is real content outside the repositories, so bundling is
    what the user wants and the caller proceeds).
    """
    from ..parse import count_files_outside_repos, count_indexable_files

    loose = count_files_outside_repos(src)
    if len(nested) == 1:
        # Strictly zero, and this is the one threshold that must not be relaxed:
        # the vendored-dependency tree above is exactly this shape with a nonzero
        # count, and there is no reading of "index that repository instead" that
        # does not drop the user's files. Anything outside the repository is
        # evidence of that shape, so anything but nothing bundles.
        return (_ONE_LEVEL_TOO_HIGH if loose == 0 else _BUNDLE), loose
    if loose <= _LOOSE_FILES_TRIVIAL:
        return _WORKSPACE, loose
    # Only now is the inside-the-repositories count worth taking, and only as far
    # as the comparison needs: the question is whether the loose files are a small
    # enough share to be scaffolding, so counting stops at the first repository
    # that settles it (~19x the loose count at a 5% share) instead of walking a
    # fleet to produce a total nothing reads.
    need = int(loose * (1 - _LOOSE_FILES_SHARE) / _LOOSE_FILES_SHARE) + 1
    inside = 0
    for repo_dir in nested:
        inside += count_indexable_files(repo_dir, limit=need - inside)
        if inside >= need:
            return _WORKSPACE, loose
    return _BUNDLE, loose


def _found_phrase(src: Path, nested: list[Path], loose: int) -> str:
    """What the scan found: how many working trees, how deep they sit, and how
    much indexable content lies outside them. All three numbers, because they are
    what the verdict below rests on -- a reader who disagrees with the verdict can
    then see which number to argue with instead of taking it on trust."""
    names = sorted(p.name for p in nested)
    depths = {len(p.relative_to(src).parts) for p in nested}
    return (f"{src.resolve()} isn't itself a git repo, but contains "
            f"{len(nested)} git working tree(s) {_depth_phrase(depths)} "
            f"({', '.join(names[:5])}{', …' if len(names) > 5 else ''}); "
            f"{_outside_phrase(loose)}.")


def _outside_phrase(loose: int) -> str:
    """"no indexable file lies outside them" reads like something a person would
    write; "0 indexable files" does not, and zero is the case the real incident
    hit, so it is the sentence most readers will actually see."""
    if loose == 0:
        return "no indexable file lies outside them"
    if loose == 1:
        return "1 indexable file lies outside them"
    return f"{loose} indexable files lie outside them"


def _refuse_bundling(src: Path, source: str, nested: list[Path],
                     shape: str, loose: int) -> None:
    """Say what was found, which shape it is, the one command that fits it with
    the real path in it, and why the override exists.

    This used to be a warning that indexing then walked straight past, and a
    warning is one keystroke from being scrolled past. On one real store it was:
    the workspace was bundled into a pseudo-repository holding a duplicate of
    every mirrored repository, 63% of all nodes, and at the time no command could
    remove it.
    """
    log(style.fail(_found_phrase(src, nested, loose)))
    if shape == _WORKSPACE:
        log("  That is a workspace mirroring several repositories. Indexing it "
            "this way files all of them under ONE id, so every symbol they hold "
            "is in the graph twice and the copies cannot be told apart.")
        cmd = f"contextlake kb index --workspace {_typed_path(source)}"
        because = "indexes each repository separately under its own identity"
    else:
        rel = nested[0].relative_to(src).as_posix()
        log("  Nothing of your own sits outside that one repository, so this "
            "directory is simply one level above it. Indexing it this way files "
            f"{rel} under this directory's name rather than its own.")
        cmd = f"contextlake kb index {_typed_path(os.path.join(source, rel))}"
        because = "files it under the identity its origin remote gives it"
    log(style.bold(f"  → Run this instead, which {because}:"))
    log(style.bold(f"        {cmd}"))
    log("  → Or pass --bundle to index this directory as one repository anyway. "
        "That is the right answer for a tree of your own loose sources that "
        "happens to carry a dependency with its own .git, which is why this is a "
        "refusal you can override and not a rule.")


def _typed_path(source: str) -> str:
    """``source`` spelled the way it can be pasted back into a shell.

    The bundling advice used to hardcode ``--workspace .``. ``source`` is
    ``args.source``/``args.path`` and only falls back to ``"."`` when neither was
    given, so ``kb index /srv/fleet`` was told to run ``--workspace .`` -- the
    current directory, not the directory just named. Following that verbatim
    indexes the wrong tree, and in a real run it cost coverage a subtler way: the
    operator read `.` as wrong, inferred the fleet lived one level down, ran
    ``--workspace ./repositories``, and the repository sitting above that
    subdirectory was never indexed under its own identity at all.

    Echoes what was typed rather than the resolved absolute path. When the user
    did type ``.`` (or typed nothing, which becomes ``.``) the short form is both
    correct and the one they will recognise as their own command; printing
    ``/home/…/very/long/path`` there would be noise. Quoting is
    :func:`shlex.quote`'s, which leaves an ordinary path untouched and only
    intervenes for one that would not survive a shell.
    """
    return shlex.quote(source)


def _match_repo_id(store, needle: str):
    """The indexed repo ``needle`` names, or ``None``.

    Exact id first, then a unique match on the id's last segment, so the bare
    ``widgets`` a reader would type resolves when only one repo ends that way and
    stays ambiguous (``None``) when several do. Deliberately stricter than
    :func:`_repo_id_suggestions`, which is a did-you-mean list and happily returns
    fuzzy near-misses: this one decides what to re-index, and guessing there would
    rebuild the wrong repository's graph.
    """
    exact = store.get_repo(needle)
    if exact is not None:
        return exact
    tail = needle.strip("/").rsplit("/", 1)[-1].lower()
    if not tail:
        return None
    hits = [r for r in store.list_repos() if r.id.rsplit("/", 1)[-1].lower() == tail]
    return hits[0] if len(hits) == 1 else None


def _resolve_source_by_id(store, source: str) -> tuple[str, Path] | None:
    """``--source`` naming an indexed repo id rather than a path: resolve it to
    that repo's recorded checkout, or explain why it cannot be resolved.

    The dead end this closes was reported from a real fleet. ``kb lint`` reports a
    repository by its **logical** id, which is derived from the origin remote and
    has no relation to where the clone sits on disk, and every obvious way to act
    on that id failed:

        kb index --source ./repositories/example.com/team/widgets  -> No such file
        kb index --source ./repositories/team/widgets              -> No such file
        kb index --source example.com/team/widgets                 -> No such file

    The last of those is the id exactly as printed. The user had an identifier and
    no way to turn it into an action, which is a worse failure than a wrong answer:
    nothing in the output pointed anywhere.

    Returns ``(repo_id, path)`` when the id resolves to a directory, otherwise
    ``None`` after logging what was established. Every branch says something the
    reader can act on -- the recorded path when it is gone, near-miss ids when the
    name is wrong -- rather than repeating the id back.
    """
    repo = _match_repo_id(store, source)
    if repo is None:
        # The same did-you-mean list every other command gives an unknown repo id,
        # rather than a second spelling of it: it already covers typos (difflib)
        # and a namespace-tail spelling, and two commands answering "unknown repo"
        # differently is the sort of inconsistency this whole item is about.
        near = _repo_id_suggestions(store, source, n=5)
        known = [r.id for r in store.list_repos()]
        log(f"{source!r} is neither a path on disk nor an indexed repository id.")
        if near:
            log(f"  Did you mean: {', '.join(near)}")
        elif known:
            more = f" (+{len(known) - 5} more)" if len(known) > 5 else ""
            log(f"  Indexed ids include: {', '.join(sorted(known)[:5])}{more}")
        else:
            log("  Nothing is indexed in this store yet.")
        return None
    if not repo.path:
        log(f"{repo.id} is indexed but has no checkout path recorded, so there is "
            "nothing to re-index. Point --source at its clone, or index the "
            "workspace that holds it.")
        return None
    path = Path(repo.path)
    if not path.is_dir():
        log(f"{repo.id} was indexed from {repo.path}, which is no longer a directory. "
            "Re-clone it there, or index it from wherever it lives now.")
        return None
    return repo.id, path


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
    """`--watch` was honoured only on the `--workspace` path. On the single-source path the
    flag parsed, ran one pass and exited 0, without watching and without saying it would not:
    the flag's own help promises "keep re-running ... on an interval" with no such condition.
    The workspace branch keeps its own loop (it re-scans for repositories each pass), so only
    the single-source case is wrapped here.
    """
    if getattr(args, "watch", False) and not getattr(args, "workspace", None):
        interval = _or_default(getattr(args, "interval", None), 60)
        src = getattr(args, "source", None) or getattr(args, "path", None) or "."
        log(f"{style.cyan('watch')}: re-indexing {src} every {interval}s (Ctrl-C to stop)")
        _watch_loop(lambda: _cmd_index_once(args), interval=interval)
        return 0
    return _cmd_index_once(args)


def _cmd_index_once(args) -> int:
    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "index"):
        store.close()
        return 1
    cfg = kb_config(args)
    # `languages` was absent here, so `kb.toml`'s setting was validated, displayed in the
    # dashboard, documented as a filter -- and then never reached the parser.
    parse_opts = dict(skip_generated=cfg.skip_generated, max_file_bytes=cfg.max_file_bytes,
                      languages=cfg.languages)
    workers = cfg.index_workers
    try:
        workspace = getattr(args, "workspace", None)
        if workspace:
            force = getattr(args, "force", False)
            repo_filter = getattr(args, "repos", None)
            if getattr(args, "watch", False):
                interval = _or_default(getattr(args, "interval", None), 60)
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
        # `--source` names a place OR an identity. A path that is not on disk is
        # the only case where it can be the latter, so the lookup costs nothing on
        # the ordinary path and is tried before the graph-shard branch, which would
        # otherwise report a missing file for a perfectly valid repo id.
        id_repo_id = None
        if not src.exists():
            resolved = _resolve_source_by_id(store, source)
            if resolved is None:
                return 1
            id_repo_id, src = resolved
            log(f"{source!r} is an indexed repository id; re-indexing its recorded "
                f"checkout at {src}.")

        if src.is_dir():
            from ..parse import index_repo_dir  # lazy: only needs tree-sitter when indexing code
            from ..repo_identity import resolve_repo_id

            # A directory that is not itself a repository but holds some: bundling
            # it is occasionally what the user wants and usually a mistake, so
            # measure which before doing anything. --bundle is read before any of
            # that, so the escape hatch costs nothing and cannot itself be refused
            # by a defect in the diagnosis.
            if not (src / ".git").exists() and not getattr(args, "bundle", False):
                nested = _nested_repo_dirs(src)
                if nested:
                    shape, loose = _bundle_shape(src, nested)
                    if shape != _BUNDLE:
                        _refuse_bundling(src, source, nested, shape, loose)
                        return 1
                    # Bundling IS the answer here, and saying so is still worth a
                    # line: the reader learns the nested repositories were seen and
                    # deliberately folded in, rather than wondering later whether
                    # they were missed.
                    log(style.warn(
                        f"{_found_phrase(src, nested, loose)} Indexing it this way "
                        "bundles everything underneath into ONE repo, which is what "
                        "that content outside them calls for. To index each nested "
                        "repo separately under its own identity instead, use "
                        f"`contextlake kb index --workspace {_typed_path(source)}`, "
                        "which indexes only the repos and not the rest."
                    ))
            # An id resolved from --source outranks the directory name (the whole
            # point of resolving one was to re-index THAT repo's graph, and a
            # canonical id derived from the origin remote rarely equals its
            # directory name, so falling back would write a second, duplicate row).
            # An explicit --repo still outranks both: it is the flag whose only job
            # is to say what to file this under.
            # ...and failing both, the id the REMOTE gives, not the directory name.
            # `--workspace` has always filed repos under their canonical id; this path
            # used `src.resolve().name`, so a clone of `gitlab.com/ns/proj` sitting in a
            # directory called `target` was stored as `target`. Every connector matches
            # on that id, so the whole enrichment tier silently found nothing for any
            # repo indexed this way -- including the zero-config `cd my-repo && kb index`
            # the tool advertises. Measured: 0 links via --source, 285 via --workspace,
            # same clone, same store. `resolve_repo_id` falls back to name@root-commit
            # for a repo with no origin, and to the directory name for a non-repo.
            repo_id = (getattr(args, "repo", None) or id_repo_id
                       or (resolve_repo_id(str(src)) if (src / ".git").exists()
                           else src.resolve().name))
            head = _git_head(src)
            # Same incremental gate --workspace applies, for the same reasons and
            # in the same order: HEAD unmoved AND the graph built by this parser
            # means there is nothing to redo. Only --workspace honoured it, so a
            # single-repo index always re-parsed, contradicting --force's own help
            # ("only repos whose HEAD moved"). --force still bypasses both tests.
            if not getattr(args, "force", False) and not needs_reindex(store, repo_id, head):
                from ..parse import PARSER_VERSION

                if indexed_parser_version(store, store_dir, repo_id) == PARSER_VERSION:
                    log(f"{repo_id} is unchanged since its last index "
                        f"(HEAD {(head or '?')[:8]}); nothing to do. "
                        "Pass --force to re-index anyway.")
                    return 0
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
