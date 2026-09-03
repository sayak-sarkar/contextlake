"""`contextlake kb docs` -- generated documentation, distinct from the wiki.

The wiki answers "what is this repository" in one page. This writes narrower documents that
answer one question each: the API reference (the public surface with its real call sites)
and the design notes (what the repository's own files record about how it was built). Both
are model-free by default; `--llm` adds one marked orientation paragraph above the API
reference and changes nothing else.

Separate from `kb wiki` on purpose rather than as another section of it. A reference is
looked things up in, a wiki page is read start to finish, and one document that tries to be
both serves neither.
"""

from __future__ import annotations

from typing import NamedTuple

from ... import style
from ...logging_setup import log
from ..store.shards import read_shard
from ._common import _connect_targets, _guard_store, _open_store, kb_config

# The store subdirectory each output writes into. One directory per KIND of document, so a
# repository's API reference and its design notes cannot collide on a filename.
API_DIR = ("docs", "api")
DESIGN_DIR = ("docs", "design")
# One page for the whole store, not one per repo, so it lives beside them rather
# than among them.
FLEET_DIR = ("docs", "fleet")


class DocsCounts(NamedTuple):
    """What one pass over a target list did, one number per outcome.

    Five separate fields and not one total, because the repairs differ: `failed` needs a
    person, `missing` needs a store repaired, `skipped` needs nothing, and `unchanged` is
    the freshness skip working. A caller that merges them reports "5 documents not written"
    over four different causes, which is the mislabelled-counter defect `kb wiki` had.
    """

    written: int = 0
    unchanged: int = 0
    skipped: int = 0
    missing: int = 0
    failed: int = 0


def _stamped_commit(path, *, kind: str, repo_id: str) -> str | None:
    """The commit a document on disk was generated from, or ``None``.

    ``None`` covers five states -- the file is absent, it cannot be read, it carries no
    marker, its marker says `unknown`, or its marker describes some other document --
    because they mean one thing to a caller deciding whether to regenerate: this page
    cannot prove which code it describes. Reading file EXISTENCE as "already generated" is
    the defect `kb wiki` shipped once (`cmds/wiki.py:349-357`), where a present page was
    never rewritten again.

    The whole marker is checked, not the commit alone. `read_stamp` returns `kind` and
    `repo` beside the commit, and a commit read without them proves less than it looks:
    two repositories sitting at one commit make one repository's page a valid-looking
    stamp for the other, so a page copied or moved onto the wrong path is read as current
    and never rewritten.

    `repo_id` is compared as the caller spells it. `stamp` rewrites whitespace and angle
    brackets before it writes the field, so an id holding one of those can never match
    here and its pages are re-rendered on every run. That is the safe direction of the
    two: extra work, never a page trusted on a marker that does not name it.
    """
    from ..docs.stamp import UNKNOWN, read_stamp

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = read_stamp(raw)
    if parsed is None or parsed[2] == UNKNOWN:
        return None
    if parsed[0] != kind or parsed[1] != repo_id:
        return None
    return parsed[2]


def generate_docs(store, store_dir, targets, *, llm=None, max_symbols=None,
                  skip_unchanged: bool = False, rebuilt: set | None = None,
                  fleet: list | None = None,
                  unreadable: list | None = None) -> DocsCounts:
    """Write the API reference and the design notes for each `(repo_id, path)` in `targets`.

    Takes an EXPLICIT target list rather than deriving one from `args`, because the two
    callers resolve targets differently and `_connect_targets` answers the wrong question for
    one of them: it maps `--source <repo-id>` to every indexed repo, and `--source <dir>` to
    the directory name instead of the canonical id `kb index` files the graph under.

    `skip_unchanged` is off for `kb docs`, so that command stays the unconditional
    regenerate, and on for `kb index`, which runs on every commit through `kb hook`.

    `rebuilt` names the repos whose graph THIS run rewrote, and those are regenerated
    whatever their pages say. A document is rendered from a shard, so the commit alone is
    not the key: a parser change builds a different graph out of the same code, and the
    run that rebuilds the shard is the run whose pages are now wrong. `kb index --force`
    arrives here the same way, because every repo it re-indexes is rewritten.

    `fleet` and `unreadable` are out-params in the `unusable=` shape `_index_workspace`
    already uses. The whole-store page is NOT written here: only a caller that covered every
    indexed repo can make a whole-store claim, and `kb index` covers a subset by construction
    (repos that failed, were over budget, or were filtered out by `--repos` never reach it).
    """
    from ..docs.api import render_api_reference
    from ..docs.design import render_design_document
    from ..docs.draft import render_orientation
    from ..docs.fleet import FleetDep
    from ..docs.snippets import SnippetReader
    from ..visualize import repo_slug

    out_dir = store_dir.joinpath(*API_DIR)
    design_dir = store_dir.joinpath(*DESIGN_DIR)
    written = unchanged = skipped = missing = failed = 0
    limit = max_symbols or 500
    for repo_id, _path in targets:
        shard = read_shard(store_dir, repo_id)
        if shard is None:
            # A DIFFERENT event from an empty shard, so it gets a different line and a
            # different exit code. The store lists this repo and its shard could not be
            # read, which is a broken store rather than a repository without symbols.
            # Reporting both as "indexed to 0 symbols" states a cause that is false half
            # the time, and hides the half that needs fixing.
            missing += 1
            # Named for the fleet page too, not only counted here: a repo whose shard
            # failed to load is not a repo that declares nothing, and the fleet page
            # promises to tell those apart.
            if unreadable is not None:
                unreadable.append(repo_id)
            log(f"  {style.warn(repo_id)}: no reference — the store lists this repo but "
                f"its shard could not be read", inline=True)
            continue
        if not shard.nodes:
            # Counted, not silent, and NOT an error: a repository that indexed to no
            # symbols has no interface to document, which is a fact worth one line.
            skipped += 1
            log(f"  {style.warn(repo_id)}: no reference — the repository indexed to "
                f"0 symbols", inline=True)
            continue
        by_name = {n.id: n.name for n in shard.nodes if n.kind == "package"}
        # Accumulated BEFORE the freshness test, so a fresh repo still contributes its
        # dependencies. A `continue` placed above this would build a page headed "the whole
        # store" out of only the repos that moved, and `scoped` would still be False, so the
        # guard in `cmd_docs` could not see it.
        if fleet is not None:
            for e in shard.edges:
                if e.relation != "depends_on":
                    continue
                pkg = by_name.get(e.dst)
                if pkg is None:
                    continue
                a = e.attrs or {}
                fleet.append(FleetDep(pkg, repo_id, e.provenance.source_file,
                                      a.get("constraint") or "", a.get("group") or "runtime"))
        head = getattr(shard, "head_commit", None)
        # The kind each directory holds, paired with the directory, because that is what
        # the renderer stamped: `docs/api.py:170` writes kind=api and `docs/design.py:352`
        # writes kind=design. A wrong constant here reads every marker as foreign, so the
        # skip never fires and every index re-renders every page.
        stamped = ((out_dir, "api"), (design_dir, "design"))
        if (skip_unchanged and head and repo_id not in (rebuilt or ())
                and all(_stamped_commit(d.joinpath(repo_slug(repo_id) + ".md"),
                                        kind=kind, repo_id=repo_id) == head
                        for d, kind in stamped)):
            # Both documents, not either: one page rewritten and the other left behind is
            # the state a per-repo skip must not leave, and it is reachable whenever a
            # previous run failed between the two writes.
            unchanged += 1
            log(f"  {style.skip(repo_id)}: documents already describe {head[:8]}", inline=True)
            continue
        try:
            page = render_api_reference(shard, repo_id=repo_id, max_symbols=limit,
                                        snippets=SnippetReader(store, repo_id))
            if llm is not None:
                block = render_orientation(llm, page, repo_id=repo_id)
                if block:
                    page = block + page
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / (repo_slug(repo_id) + ".md")).write_text(page, encoding="utf-8")
            design_dir.mkdir(parents=True, exist_ok=True)
            (design_dir / (repo_slug(repo_id) + ".md")).write_text(
                render_design_document(shard, repo_id=repo_id), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - one repo must not abort the run
            # `kb index` calls this on every commit through `kb hook`, so a renderer that
            # trips over one repository must not take the other 479 down with it.
            failed += 1
            log(f"  {style.fail(repo_id)}: documents failed: {e}", inline=True)
            continue
        written += 1
    return DocsCounts(written=written, unchanged=unchanged, skipped=skipped,
                      missing=missing, failed=failed)


def cmd_docs(args) -> int:
    """Write the API reference for each indexed repository."""
    from ..docs.fleet import render_fleet_design

    # OPT-IN prose tier. Absent --llm this stays None and the run is byte-for-byte what it
    # always was: no model, no network, every line traceable to the graph.
    llm = None
    if getattr(args, "llm", None):
        from ..config import apply_llm_overrides
        from ..llm import build_llm
        cfg = kb_config(args)
        apply_llm_overrides(cfg, provider=args.llm, model=getattr(args, "llm_model", None))
        llm = build_llm(cfg.llm)
        if llm is None:
            log("--llm was requested but no model could be built; writing the "
                "deterministic documents only.")

    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "docs"):
        store.close()
        return 1
    try:
        targets = _connect_targets(args, store)
        if not targets:
            wanted = [a for a in (getattr(args, "args", None) or []) if a]
            if wanted:
                log(f"No indexed repo matches {', '.join(wanted)} — check the exact repo id "
                    "(see `contextlake kb lint`).")
                return 1
            log("No indexed repos (run `contextlake kb index` first)")
            return 0

        out_dir = store_dir.joinpath(*API_DIR)
        # Accumulated from the shards the loop already reads, so the fleet page costs no
        # extra I/O. Only written when the run covered EVERY indexed repo -- see below.
        fleet: list = []
        unreadable: list = []
        scoped = bool([a for a in (getattr(args, "args", None) or []) if a])
        # `skip_unchanged` stays off here on purpose: `kb docs` is the unconditional
        # regenerate, which is what a reader reaches for when a page looks wrong. The
        # freshness skip belongs to `kb index`, which runs on every commit.
        counts = generate_docs(store, store_dir, targets, llm=llm,
                               max_symbols=getattr(args, "max_symbols", None),
                               fleet=fleet, unreadable=unreadable)
        written, skipped, missing = counts.written, counts.skipped, counts.missing
        # A fleet page built from a SUBSET would be a false claim: "3 of 15 packages are
        # shared" is only true of the whole store, and a reader has no way to tell the page
        # was scoped. So it is written only for a full run, and a scoped run says why not.
        wrote_fleet = False
        if written and not scoped:
            fleet_dir = store_dir.joinpath(*FLEET_DIR)
            fleet_dir.mkdir(parents=True, exist_ok=True)
            # The stamp's members are read from the repos table, the same source the cluster
            # page's fingerprint uses. A repo with no recorded head contributes `unknown`
            # rather than being dropped: dropping it would make a store with an unindexed
            # member fingerprint identically to one without that member at all.
            members = []
            for repo_id, _p in targets:
                row = store.get_repo(repo_id)
                members.append((
                    repo_id,
                    getattr(row, "head_commit", None) if row is not None else None,
                    store.get_repo_parser_version(repo_id),
                ))
            (fleet_dir / "design.md").write_text(
                render_fleet_design(fleet, repos=[r for r, _p in targets],
                                    unreadable=unreadable, members=members),
                encoding="utf-8")
            wrote_fleet = True
        elif scoped:
            log("  fleet page skipped: this run was scoped to named repos, and a fleet "
                "view of part of the store would report shares and disagreements that are "
                "not true of the whole", inline=True)

        glyph = style.warn() if (skipped or missing or counts.failed) else style.ok()
        log(f"{glyph} API reference and design notes: {written} of each written"
            # Named because it ran. A summary that lists two outputs while three were
            # written under-reports the work, which is the same defect as reporting a
            # partial run as complete, pointed the other way.
            + (", plus one fleet page" if wrote_fleet else "")
            + (f", {skipped} skipped (nothing indexed)" if skipped else "")
            + (f", {counts.failed} failed" if counts.failed else "")
            + (f", {missing} unreadable" if missing else "")
            + f" → {out_dir.parent}")
        # Non-zero whenever a shard could not be read, even PARTIALLY: one unreadable repo out
        # of ten is a store to repair, and an exit code of 0 there tells a script the run was
        # clean. An empty shard is not a failure, so it alone only reaches this when nothing
        # at all could be written.
        return 1 if missing or counts.failed or (written == 0 and skipped) else 0
    finally:
        store.close()
