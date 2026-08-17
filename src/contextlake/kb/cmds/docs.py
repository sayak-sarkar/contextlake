"""`contextlake kb docs` -- generated documentation, distinct from the wiki.

The wiki answers "what is this repository" in one page. This writes narrower documents that
answer one question each, and today that is the API reference: the public surface with its
real call sites. No model is involved in any of it.

Separate from `kb wiki` on purpose rather than as another section of it. A reference is
looked things up in, a wiki page is read start to finish, and one document that tries to be
both serves neither.
"""

from __future__ import annotations

from ... import style
from ...logging_setup import log
from ..store.shards import read_shard
from ._common import _connect_targets, _guard_store, _open_store

# The store subdirectory each output writes into. One directory per KIND of document, so a
# repository's API reference and its future design document cannot collide on a filename.
API_DIR = ("docs", "api")


def cmd_docs(args) -> int:
    """Write the API reference for each indexed repository."""
    from ..docs.api import render_api_reference
    from ..visualize import repo_slug

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
        written = skipped = missing = 0
        limit = getattr(args, "max_symbols", None) or 500
        for repo_id, _path in targets:
            shard = read_shard(store_dir, repo_id)
            if shard is None:
                # A DIFFERENT event from an empty shard, so it gets a different line and a
                # different exit code. The store lists this repo and its shard could not be
                # read, which is a broken store rather than a repository without symbols.
                # Reporting both as "indexed to 0 symbols" states a cause that is false half
                # the time, and hides the half that needs fixing.
                missing += 1
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
            page = render_api_reference(shard, repo_id=repo_id, max_symbols=limit)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / (repo_slug(repo_id) + ".md")).write_text(page, encoding="utf-8")
            written += 1
        glyph = style.warn() if (skipped or missing) else style.ok()
        log(f"{glyph} API reference: {written} written"
            + (f", {skipped} skipped (nothing indexed)" if skipped else "")
            + (f", {missing} unreadable" if missing else "")
            + f" → {out_dir}")
        # Non-zero whenever a shard could not be read, even PARTIALLY: one unreadable repo out
        # of ten is a store to repair, and an exit code of 0 there tells a script the run was
        # clean. An empty shard is not a failure, so it alone only reaches this when nothing
        # at all could be written.
        return 1 if missing or (written == 0 and skipped) else 0
    finally:
        store.close()
