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


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cl-graph-embed-"))
    try:
        cfg = tmp / "kb.toml"
        cfg.write_text(f'[kb]\nstore_dir = "{tmp / "store"}"\n', encoding="utf-8")

        for args in (
            ["kb", "index", str(REPO), "--config", str(cfg)],
            ["kb", "graph", "--search", SEED, "--repo", "contextlake",
             "--hops", HOPS, "--max-nodes", MAX_NODES,
             "--format", "html", "--output", str(OUT), "--config", str(cfg)],
        ):
            r = subprocess.run([sys.executable, "-m", "contextlake", *args], cwd=REPO)
            if r.returncode != 0:
                print(f"  [gen_graph_embed] FAILED: {' '.join(args)}")
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
