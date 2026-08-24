"""``contextlake kb forget`` -- remove one repository from the store, in every tier.

Why this exists. contextlake could already tell you a stored repo was wrong -- the
id-migration pass says "git can't find a repository here at all ... re-clone or
remove it", and ``lint`` reports unreadable and stale repos -- while offering no way
to act on it. Nineteen ``kb`` subcommands and not one could remove a repo, so the
only supported repair for a mis-indexed store was deleting the whole store and
re-indexing every repo in it.

The case that motivated it: running bare ``kb index`` in a directory that *contains*
git repos rather than being one bundles everything underneath into a single
pseudo-repo named after the directory. contextlake warns first, and the warning
prints the right command, but a warning is one keystroke from being ignored. On one
real store that pseudo-repo held 63% of all nodes, duplicating every mirrored repo
under a second identity, and ``embed`` then spent 91% of its vectors on it.

A repo lives in four tiers and all four have to go, or the leftovers are worse
than the original: orphaned vectors still answer semantic queries under a repo id
that no longer resolves, and an orphaned wiki page still reads as current.

The fourth tier is the files. Rows are the small half -- the parsed graph sits in
``graph/<id>.json`` and every commit ever indexed in ``history/<id>/``, and on a real
store that shard was 173 MB against a few MB of rows. Removing rows alone reclaimed
nothing, which misses the point of the case above: the user forgetting a bloated
pseudo-repo wants the space back, and was told the repo had been removed.
"""

from __future__ import annotations

import difflib

from ... import style
from ...logging_setup import log
from ._common import _guard_store, _open_store, kb_config


def _wiki_pages(wiki_dir, repo_id: str) -> list:
    """Every wiki file belonging to ``repo_id``: whole-repo page and module pages.

    Both names are composed here from the same sanitization the writers use
    (``repo_id.replace("/", "__")``) rather than by globbing a prefix. A prefix
    glob is wrong: repo ``team/app`` sanitizes to ``team__app``, and so does the
    module page of repo ``team`` for module ``app`` -- and ``team__app*`` would
    also sweep up the unrelated repo ``team/appendix``. Module pages live under
    ``_modules/`` (see ``wiki._module_page_file``), which is what keeps the two
    namespaces apart on disk, so each is matched in its own directory.
    """
    if not wiki_dir.is_dir():
        return []
    safe = repo_id.replace("/", "__")
    found = []
    whole = wiki_dir / f"{safe}.md"
    if whole.is_file():
        found.append(whole)
    modules = wiki_dir / "_modules"
    if modules.is_dir():
        # A module page is "<repo>__<module>.md", so the repo's own pages are
        # exactly those prefixed with "<repo>__". The trailing separator is what
        # stops repo "team/app" from claiming repo "team/appendix"'s pages.
        found += sorted(p for p in modules.glob(f"{safe}__*.md") if p.is_file())
    return found


def _partitions(repo_id: str) -> list[str]:
    """Every store partition a repo owns: its code shard and its connector shards.

    ``connect``/``enrich`` write into ``@connect:<repo>``/``@enrich:<repo>`` on
    purpose, so re-indexing code never clobbers connector output. That isolation is
    a write-side concern, and ``clear_repo``/``delete_repo`` match on the literal id
    alone -- so removing only the literal partition leaves the connector nodes,
    edges and vectors behind, still answering queries under a repo id that no longer
    resolves. Mirrors ``embeddings.store._repo_scope``, which expands the same way
    for search, and degrades to the literal id if the connectors are not installed.
    """
    try:
        from ..connectors.enrich import enrich_partition
        from ..connectors.orchestrate import connect_partition
    except ImportError:
        return [repo_id]
    return [repo_id, connect_partition(repo_id), enrich_partition(repo_id)]


def _disk_artifacts(store_dir, parts: list[str], repo_id: str) -> list:
    """The repo's on-disk files: each partition's shard, plus its history snapshots.

    The database rows are only half of a repo. Its parsed graph also sits in
    ``graph/<id>.json``, and every commit ever indexed sits in ``history/<id>/``, and
    those are the *large* half -- on one real store a single shard was 173 MB while the
    rows it mirrored were a few MB of SQLite pages. Removing the rows alone reclaimed
    nothing, which defeats the case this command was written for: a store bloated by a
    mis-index, where reclaiming the space is the entire point.

    ``shard_path`` rejects an id that would escape ``graph/``; the history directory is
    derived from that validated path rather than joined again, so both go through one
    check instead of two that could disagree.
    """
    from ..store.shards import shard_path

    found = []
    for part in parts:
        try:
            p = shard_path(store_dir, part)
        except ValueError:
            continue  # a path-escaping id owns no file we are willing to touch
        if p.is_file():
            found.append(p)
    hist = store_dir / "history" / repo_id
    if hist.is_dir():
        found.append(hist)
    return found


def _bytes_of(path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _human(n: int) -> str:
    """Bytes as a short human string. Local on purpose: the only other size
    formatter in the tree is the dashboard's, in JavaScript."""
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GB"


def _prune_sentinels(store) -> int:
    """Sweep sentinel nodes the removal just left unreferenced. Returns how many.

    The sentinels (``(shared)``, ``(packages)``, ``(external)``, ``(system)``) hold
    the nodes no single repo owns, so nothing above removes them: an imported
    package or an HTTP route can be produced or consumed by many repos at once, the
    store dedupes it to one row, and per-repo attribution lives on its *edges*
    rather than on the node's own ``repo`` (see ``kb/model.py``). Measured on a real
    store, forgetting the only repo in it left 734 such nodes behind, still listed,
    still searchable, describing packages and routes belonging to nothing.

    Deleting them per-repo would be the worse bug, and exactly the one the stable
    sentinel exists to prevent: it would take the packages the *surviving* repos
    still import. So the rule is reachability, not ownership. A sentinel node is
    garbage once no edge anywhere references it, which is precisely what
    ``prune_orphan_nodes`` already tests, and forgetting a repo deletes that repo's
    edges, so the nodes only it referenced fall out of the graph by themselves.

    Store-wide, and deliberately so: it can also clear sentinel litter that predates
    this repo. ``connect`` sweeps ``(external)`` the same way for the same reason.
    Must run after the rows are gone (nothing is orphaned before that) and before
    the VACUUM (so the pages it frees are handed back too).
    """
    from ..model import is_sentinel_repo

    return sum(store.prune_orphan_nodes(part)
               for part in store.list_partitions() if is_sentinel_repo(part))


def _compact(store, store_dir) -> int:
    """VACUUM the index, returning the bytes handed back to the filesystem.

    Deleting rows frees SQLite *pages*, it does not shrink the *file*: the freed pages
    go on the freelist and the file stays at its high-water mark. Measured on a real
    store, deleting one repo left a 197 MB index of which 188 MB was freelist -- so a
    user forgetting a bloated pseudo-repo to reclaim space watched the largest file in
    the store not move.

    VACUUM is normally something to be careful with, but not at this size and not here:
    it took 0.1s on that 197 MB index and returned it to 9 MB. `forget` is explicit,
    rare and destructive, and reclaiming space is the reason it was run.

    Never raise: the repo is already gone by this point, and a store that is merely
    larger than it needs to be is not a failure worth reporting as one.
    """
    db = store_dir / "index.sqlite"
    try:
        before = db.stat().st_size
        store.conn.execute("VACUUM")
        return max(0, before - db.stat().st_size)
    except Exception:  # noqa: BLE001 - cosmetic; the removal itself already succeeded
        return 0


def cmd_forget(args) -> int:
    repo_id = args.repo
    dry_run = bool(getattr(args, "dry_run", False))
    cfg = kb_config(args)
    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "forget"):
        store.close()
        return 1
    try:
        parts = _partitions(repo_id)
        counts = [store.repo_counts(p) for p in parts]
        nodes = sum(n for n, _ in counts)
        edges = sum(e for _, e in counts)
        known = store.get_repo(repo_id) is not None
        if not known and not nodes and not edges:
            log(f"{style.warn()} No repo {repo_id!r} in the store at {store_dir}.")
            # Name the candidates HERE rather than sending the reader to `kb lint`. lint
            # surfaces ids only through its FINDING categories (stale, empty, parser-stale,
            # unreadable), so on a healthy store it lists none and the pointer answers nothing.
            # A repo id also carries an @<commit> suffix, which is the usual reason a
            # hand-typed id misses by a hair.
            known_ids = [r.id for r in store.list_repos()]
            near = difflib.get_close_matches(repo_id, known_ids, n=3, cutoff=0.5)
            # An id carries an @<commit> suffix, so the usual near-miss is a bare name that
            # difflib scores below the cutoff on a long hash. Match the stem explicitly.
            near = near or [r for r in known_ids if r.split("@", 1)[0] == repo_id][:3]
            if near:
                log(f"  Did you mean: {', '.join(near)}")
                log("  A repo id includes its @<commit> suffix.")
            elif known_ids:
                log(f"  The store holds {len(known_ids)} repo(s), including: "
                    f"{', '.join(sorted(known_ids)[:3])}")
            else:
                log("  The store holds no repositories.")
            return 1

        pages = _wiki_pages(store_dir / "wiki", repo_id)
        vec, vectors = None, 0
        if cfg.embeddings.enabled:
            vec_path = store_dir / "embeddings.sqlite"
            if vec_path.exists():
                from ..embeddings.store import build_vector_store

                vec = build_vector_store(vec_path,
                                         backend=cfg.embeddings.vector_backend)
                vectors = sum(vec.count_repo(p) for p in parts)

        disk = _disk_artifacts(store_dir, parts, repo_id)
        reclaim = sum(_bytes_of(p) for p in disk)

        log(f"{repo_id}")
        log(f"  nodes    {nodes}")
        log(f"  edges    {edges}")
        log(f"  vectors  {vectors}")
        log(f"  wiki     {len(pages)} page(s)")
        log(f"  on disk  {len(disk)} file(s)/dir(s), {_human(reclaim)}")
        # strict: both lists come from `parts`, so a length mismatch would mean the
        # counts no longer line up with the partitions they describe.
        extra = [p for p, (n, e) in zip(parts[1:], counts[1:], strict=True) if n or e]
        if extra:
            log(f"  including connector partitions: {', '.join(extra)}")

        if dry_run:
            # Say so rather than print a number: the index's reclaimable space is its
            # freelist AFTER this delete, which cannot be known without doing it. A
            # confident under-estimate here would read as the whole answer.
            log("  shared nodes nothing references any more are pruned afterwards")
            log("  the index is compacted afterwards, reclaiming more")
            log(f"{style.ok()} Dry run: nothing removed.")
            return 0

        # delete_repo also drops the `repos` row; the connector partitions have no
        # such row of their own, so clear_repo is the right verb for them.
        # Declared before the first thing that can fail to remove, so the wiki pages and
        # the disk artefacts below both record into one list and one check decides.
        survived: list[str] = []
        store.delete_repo(repo_id)
        for part in parts[1:]:
            store.clear_repo(part)
        if vec is not None:
            for part in parts:
                vec.clear_repo(part)
            vec.close()
        # Verified, not assumed. The summary counts these as removed, and an unlink that
        # silently did nothing (a read-only directory, a file held open) made the count a
        # claim rather than a measurement -- the same gap the byte figure below was already
        # corrected for.
        pages_left = 0
        for page in pages:
            page.unlink(missing_ok=True)
            if page.exists():
                pages_left += 1
                survived.append(str(page))
        # Shards last: the rows are gone by now, so a failure here leaves an orphaned
        # file rather than a store whose rows disagree with the files backing them.
        import shutil

        from ..store.shards import _cache_evict

        # `reclaim` above is a PREDICTION, summed before anything was deleted, and it
        # was printed as an outcome -- so a `rmtree(ignore_errors=True)` that removed
        # nothing still reported the full figure as space freed. Measure after, and
        # count only what actually went away.
        freed = 0
        for p in disk:
            before = _bytes_of(p)
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
                # This process may hold the shard in its read cache; a later read in
                # the same run must not resurrect what was just deleted.
                _cache_evict(str(p))
            if p.exists():
                survived.append(str(p))
            else:
                freed += before
        reclaim = freed
        if survived:
            log(style.warn(
                f"  {len(survived)} path(s) could not be removed and are still on disk: "
                f"{', '.join(survived[:3])}"
                f"{f' (+{len(survived) - 3} more)' if len(survived) > 3 else ''}"))
        pruned = _prune_sentinels(store)
        reclaim += _compact(store, store_dir)
    finally:
        store.close()

    if pruned:
        # Its own line, never folded into `nodes`: that figure is what this repo
        # owned, and a shared node never belonged to it.
        log(f"  pruned {pruned} shared node(s) nothing references any more")
    # The glyph follows the outcome, and so does the exit code. A ⚠ naming paths still on
    # disk and a green ✓ over the same operation one line apart is the summary contradicting
    # its own counts -- and this command is framed as the fix for a bloated store, so a user
    # reading the ✓ to confirm space was reclaimed is told the wrong thing. The graph rows
    # ARE gone either way, which is why the wording says partly rather than not at all.
    if survived:
        log(style.summary_line(
            "warn", f"Partly forgot {repo_id}: {nodes} node(s), {edges} edge(s), "
                    f"{vectors} vector(s), {len(pages) - pages_left} wiki page(s) removed "
                    f"from the graph, "
                    f"{_human(reclaim)} reclaimed, but {len(survived)} path(s) are still on "
                    f"disk (named above)."))
        log("  Remove them by hand, or re-run once whatever holds them has let go.")
        return 1
    log(f"{style.ok()} Forgot {repo_id}: {nodes} node(s), {edges} edge(s), "
        f"{vectors} vector(s), {len(pages)} wiki page(s), {_human(reclaim)} reclaimed.")
    log("  If that was a mistake, re-index it -- `kb index --workspace DIR` "
        "indexes each nested repo under its own identity.")
    return 0
