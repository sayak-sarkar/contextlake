#!/usr/bin/env python3
"""Run contextlake and a comparator over the same pinned public trees, on the same unit.

Why this file exists at all: ``docs/benchmarks.md`` opens by retracting a set of numbers
because no script, dataset or result file was ever committed with them, so nobody could
re-run them. Any comparison this project publishes has to arrive with the thing that
produced it. This is that thing.

**The unit is the whole argument.** The two tools count edges differently. The comparator
makes position part of edge identity, so a function called four times from one place is
four edge rows. contextlake stores one row per (src, dst, relation) with a weight. An
earlier comparison put one tool's per-site count beside the other's per-pair count and
reported a headline that was mostly an artefact of that choice. This script therefore
reports BOTH numbers for both tools -- raw rows and distinct relationships -- and the
distinct count is the only one meant for comparison.

Not run in CI: it needs the network, npm, and minutes per tree. It is meant to be run by
hand, by anyone, and to produce a file they can diff against the committed one.

    python benchmarks/head-to-head/run.py --all
    python benchmarks/head-to-head/run.py --tree flask

Nothing here reads or writes anything outside ``--work-dir`` and ``results/``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
COMPARATOR = "@colbymchenry/codegraph@1.5.0"


def _ident(name: str) -> str:
    """A SQL identifier this file is allowed to interpolate.

    Table and column names cannot be bound as parameters, so they are interpolated --
    and the two schemas genuinely differ (`src`/`dst`/`relation` against
    `source`/`target`/`kind`), so a single hardcoded query cannot serve both. This makes
    the interpolation safe by construction rather than by assertion: anything that is
    not a bare identifier never reaches a query string.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"not a SQL identifier: {name!r}")
    return name


def sh(argv, cwd=None, env=None, timeout=3600):
    """Run a command, returning (returncode, stdout+stderr, seconds)."""
    started = time.monotonic()
    p = subprocess.run(argv, cwd=cwd and str(cwd), env=env,  # noqa: S603 - argv list, no shell
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or ""), round(time.monotonic() - started, 2)


def clone(tree, into: Path):
    """Check the tree out at its pinned commit. A pinned commit is what makes a published
    number re-derivable a year later; a branch name is not."""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    sh(["git", "init", "-q", "."], cwd=into)
    sh(["git", "remote", "add", "origin", tree["url"]], cwd=into)
    rc, out, _ = sh(["git", "fetch", "-q", "--depth", "1", "origin", tree["commit"]], cwd=into)
    if rc:
        raise RuntimeError(f"fetch failed for {tree['key']}:\n{out[-1500:]}")
    rc, out, _ = sh(["git", "checkout", "-q", "FETCH_HEAD"], cwd=into)
    if rc:
        raise RuntimeError(f"checkout failed for {tree['key']}:\n{out[-1500:]}")


def count_sqlite(db: Path, table: str, cols: tuple[str, str, str]) -> dict:
    """Raw rows and distinct (src, dst, kind) triples for one tool's edge table."""
    if not db.is_file():
        return {"error": f"no store at {db.name}"}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        a, b, c = (_ident(x) for x in cols)
        table = _ident(table)
        q_rows = f"select count(*) from {table}"  # noqa: S608 - identifiers via _ident
        q_dist = f"select count(*) from (select distinct {a},{b},{c} from {table})"  # noqa: S608
        rows = con.execute(q_rows).fetchone()[0]
        distinct = con.execute(q_dist).fetchone()[0]
        return {"rows": rows, "distinct_relationships": distinct,
                "rows_per_relationship": round(rows / distinct, 3) if distinct else None}
    finally:
        con.close()


def count_nodes(db: Path, table: str = "nodes") -> dict:
    if not db.is_file():
        return {"error": f"no store at {db.name}"}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        total = con.execute(f"select count(*) from {_ident(table)}").fetchone()[0]  # noqa: S608
        return {"total": total}
    finally:
        con.close()


def run_contextlake(repo: Path, work: Path) -> dict:
    home = work / "cl-home"
    (home / ".contextlake").mkdir(parents=True, exist_ok=True)
    store = work / "cl-store"
    # Embeddings off on BOTH sides of the comparison: the comparator has no vector tier,
    # so timing one tool with it on would be measuring a feature the other does not have.
    (home / ".contextlake" / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{store}"\n[embeddings]\nenabled = false\n', encoding="utf-8")
    env = dict(os.environ, HOME=str(home), NO_COLOR="1")
    rc_v, ver, _ = sh([sys.executable, "-m", "contextlake", "--version"], env=env)
    rc, out, secs = sh([sys.executable, "-m", "contextlake", "kb", "index", str(repo)], env=env)
    db = store / "index.sqlite"
    return {
        "tool": "contextlake",
        "version": ver.strip() if not rc_v else "unknown",
        "ok": rc == 0,
        "seconds": secs,
        "log_tail": out[-800:] if rc else "",
        "nodes": count_nodes(db),
        "edges": count_sqlite(db, "edges", ("src", "dst", "relation")),
    }


def run_comparator(repo: Path, work: Path) -> dict:
    # Telemetry off explicitly. The comparator's own default is on, which is a difference
    # worth stating in the write-up rather than silently benefiting from here.
    env = dict(os.environ, CODEGRAPH_TELEMETRY="0", DO_NOT_TRACK="1", NO_COLOR="1",
               npm_config_cache=str(work / "npm-cache"))
    rc_v, ver, _ = sh(["npx", "--yes", COMPARATOR, "version"], cwd=repo, env=env)
    rc, out, secs = sh(["npx", "--yes", COMPARATOR, "init", str(repo)], cwd=repo, env=env)
    db = repo / ".codegraph" / "codegraph.db"
    return {
        "tool": COMPARATOR,
        "version": ver.strip().splitlines()[-1] if not rc_v and ver.strip() else "unknown",
        "ok": rc == 0,
        "seconds": secs,
        "log_tail": out[-800:] if rc else "",
        "nodes": count_nodes(db),
        "edges": count_sqlite(db, "edges", ("source", "target", "kind")),
    }


def run_tree(tree: dict, work_dir: Path) -> dict:
    work = work_dir / tree["key"]
    repo = work / "src"
    print(f"\n=== {tree['key']} ({tree['language']}: {tree['shape']}) ===", flush=True)
    clone(tree, repo)

    # A tool that fails on a tree is a RESULT, not a reason to drop the tree. Publishing
    # only the trees where both tools succeeded is the same selection this whole file
    # exists to avoid.
    out = {"tree": tree, "runs": []}
    for fn in (run_contextlake, run_comparator):
        try:
            r = fn(repo, work)
        except Exception as exc:  # noqa: BLE001 - a crash is a publishable outcome
            r = {"tool": fn.__name__, "ok": False, "error": repr(exc)}
        print(f"  {r.get('tool')}: ok={r.get('ok')} "
              f"{r.get('seconds')}s nodes={r.get('nodes', {}).get('total')} "
              f"edges={r.get('edges', {})}", flush=True)
        out["runs"].append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tree", action="append", help="tree key (repeatable); default all")
    ap.add_argument("--all", action="store_true", help="run every tree in trees.json")
    ap.add_argument("--work-dir", default=None,
                    help="scratch checkout root (default: a sibling 'work' dir, gitignored)")
    args = ap.parse_args()

    trees = json.loads((HERE / "trees.json").read_text(encoding="utf-8"))["trees"]
    if args.tree:
        want = set(args.tree)
        trees = [t for t in trees if t["key"] in want]
        missing = want - {t["key"] for t in trees}
        if missing:
            print(f"unknown tree(s): {sorted(missing)}", file=sys.stderr)
            return 2
    elif not args.all:
        ap.print_help()
        return 2

    work_dir = Path(args.work_dir) if args.work_dir else HERE / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    failures = 0
    for tree in trees:
        res = run_tree(tree, work_dir)
        (RESULTS / f"{tree['key']}.json").write_text(
            json.dumps(res, indent=2) + "\n", encoding="utf-8")
        failures += sum(1 for r in res["runs"] if not r.get("ok"))

    # Non-zero when any run failed, matching the convention the rest of the CLI follows:
    # an operation that could not observe its input says so in its exit code too.
    print(f"\nwrote {len(trees)} result file(s) to {RESULTS}"
          + (f"; {failures} run(s) failed (recorded, not hidden)" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
