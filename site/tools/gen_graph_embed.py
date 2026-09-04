#!/usr/bin/env python3
"""Regenerate site/graph-embed.html: the real interactive graph shown in 'See it'.

The landing page embeds an actual contextlake graph rather than a screenshot, exported
from contextlake's OWN codebase so the sample is public by construction and no other
repository's names can leak into a published asset.

This script exists because the first export (2026-07-24) was produced by hand and the
invocation was never written down, so the file could not be rebuilt when the renderer
changed. It has to be rebuildable: a graph page is a *static artifact*, and a renderer
fix does not reach one already on disk. That is not hypothetical -- the stored-XSS fix
in 6.2.0 left this file stale until it was regenerated, while the advisory was telling
users to regenerate theirs.

Run it after any change to the graph renderer or the brand palette:

    python3 site/tools/gen_graph_embed.py

Nothing runs it for you. `build_docs.py` does not write this page, and `deploy.sh` skips
it in both directions: `.gitignore` un-ignores it so it is tracked and copied, and the
prune loop names it as an exception so a deploy never retires it. A renderer change
therefore ships to the docs pages and leaves this one on whatever it was last built
from -- which is how the embedded copy fell 194 lines behind `static/app.js` (missing
the keyboard zoom controls, the folded-leaves count and the wiki link).

The index is built in a throwaway store under a temp directory, never the caller's real
one. Note the config key is `[kb] store_dir`; a `[store]` table is silently ignored and
the whole run lands in the default store instead (see kb/config.py's own warning).
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
OUT = REPO / "site" / "graph-embed.html"

# The seed and caps that shape the embedded view. `repo_subgraph` sits in the middle of
# the graph-building code, so a 2-hop neighbourhood around it shows the subsystem the
# page is actually demonstrating. 140 nodes is what the original export shipped and is
# about what stays legible in a 600px iframe.
SEED = "repo_subgraph"
HOPS = "2"
MAX_NODES = "140"


def _only_repo_id(store_dir: pathlib.Path) -> str:
    """The id `kb index` stored for this repository, read back from the store.

    `--repo` used to be the literal string "contextlake" and the run stopped working
    the day the indexer started storing remote-derived ids: the id on disk is
    `github.com/<owner>/contextlake`, `--repo` is matched against it exactly, and a
    scope that matches nothing takes the seed down with it -- the run failed on
    "Nothing in the graph matches 'repo_subgraph'", which names the seed and says
    nothing about the scope that hid it. Reading the id back cannot drift: whatever
    the indexer wrote is what the graph call asks for.

    The store is built one line above this call and holds this repository alone, so
    more than one id means the index step did something other than what it says.
    """
    from contextlake.kb.store.sqlite_store import SqliteStore

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        ids = sorted(r.id for r in store.list_repos())
    finally:
        store.close()
    if len(ids) != 1:
        raise SystemExit(
            f"  [gen_graph_embed] expected 1 indexed repository, found {len(ids)}: {ids}")
    return ids[0]


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cl-graph-embed-"))
    try:
        cfg = tmp / "kb.toml"
        store_dir = tmp / "store"
        cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n', encoding="utf-8")

        index_args = ["kb", "index", str(REPO), "--config", str(cfg)]
        r = subprocess.run([sys.executable, "-m", "contextlake", *index_args], cwd=REPO)
        if r.returncode != 0:
            print(f"  [gen_graph_embed] FAILED: {' '.join(index_args)}")
            return r.returncode

        graph_args = ["kb", "graph", "--search", SEED, "--repo", _only_repo_id(store_dir),
                      "--hops", HOPS, "--max-nodes", MAX_NODES,
                      "--format", "html", "--output", str(OUT), "--config", str(cfg)]
        r = subprocess.run([sys.executable, "-m", "contextlake", *graph_args], cwd=REPO)
        if r.returncode != 0:
            print(f"  [gen_graph_embed] FAILED: {' '.join(graph_args)}")
            return r.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    kb = OUT.stat().st_size / 1024
    print(f"  [gen_graph_embed] wrote {OUT.relative_to(REPO)} ({kb:.0f} KB)")
    # The page must stay offline: it is served from a static host and framed in an
    # iframe, so any external fetch would be both a privacy leak and a broken render.
    html = OUT.read_text(encoding="utf-8", errors="replace")
    for marker in ('src="http', "src='http", 'href="http', "href='http"):
        if marker in html:
            print(f"  [gen_graph_embed] WARNING: external resource load ({marker})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
