# Head-to-head: contextlake against a comparable local code-graph tool

This directory holds a comparison you can re-run. `docs/benchmarks.md` opens by retracting an
earlier set of numbers because no script, dataset or result file was ever committed with them, so
nobody, including us, could reproduce them. Everything needed to reproduce what is published here
is in this directory:

| File | What it is |
| --- | --- |
| `trees.json` | The public repositories, each pinned to a commit |
| `run.py` | The harness: clone, index with both tools, count both stores |
| `results/*.json` | One committed result file per tree, exactly as the harness wrote it |

```bash
python benchmarks/head-to-head/run.py --all        # every tree
python benchmarks/head-to-head/run.py --tree flask # one of them
```

It needs the network and `npm` (the comparator installs via `npx`), and it is deliberately not run
in CI: it takes minutes per tree and depends on two package registries staying up.

## The unit is the whole argument

The two tools do not count edges the same way, and this is where comparisons of this kind usually
go wrong. The comparator makes position part of edge identity, so a function called four times from
one place produces four edge rows. contextlake also records a source line per edge, so it too
stores more than one row for some pairs.

Measured on these trees rather than assumed: the row-to-pair ratio is **not** 1.0 on either side,
and it varies by relation. On `flask`, contextlake's `contains`, `inherits` and `exposes` edges sit
at exactly 1.000, while `calls` is 1.261 and `imports` 1.337; on `fmt`, `calls` reaches 1.462. This
is worth stating plainly because an earlier internal comparison recorded contextlake's ratio as
"exactly 1.000" and that does not hold here.

So neither tool's raw row count means what a reader would assume, and putting one beside the other
inflates the apparent difference in whichever direction the ratios happen to differ. The harness
therefore reports **both** numbers for **both** tools:

- `rows` -- raw edge rows in that tool's store
- `distinct_relationships` -- `COUNT(DISTINCT src, dst, kind)`, the same query shape on both sides
- `rows_per_relationship` -- the multiplier, which makes each tool's unit visible rather than
  something you have to know in advance

**Only `distinct_relationships` is meant for comparison.** The other two are published so you can
check that claim rather than take it.

## What this measures, and what it does not

It measures what each tool extracts from the same source at the same commit: how many nodes, and
how many distinct relationships. That is a coverage measure.

It is **not** a quality measure. More edges is not automatically better: an edge to the wrong
target is worse than no edge, and neither count here distinguishes them. Precision needs a labelled
answer set, which this harness does not have. Read these numbers as "how much of the tree did each
tool represent", nothing more.

Both tools are run with their optional tiers off, so neither is timed doing work the other cannot
do. Embeddings are disabled on the contextlake side; the comparator has no vector tier. The
comparator's telemetry, which is on by default in its own configuration, is explicitly disabled by
the harness.

## Why these trees

Four public repositories, four shapes, chosen before any of them was run:

| Tree | Language | Shape |
| --- | --- | --- |
| `fmt` | C++ | modern, header-and-template heavy, few macros |
| `curl` | C | mature and portable, heavy `#ifdef` and macro use |
| `flask` | Python | small, decorator-heavy |
| `express` | JavaScript | small, CommonJS |

The point of four shapes is that **the margin is tree-dependent**, and a single favourable tree
invites the obvious rebuttal. Every tree in `trees.json` is published with whatever it showed,
including the ones where contextlake does not lead. A run where a tool fails outright is recorded
as a result, not dropped: `run.py` exits non-zero and writes the failure into the result file.

## Reading a result file

```jsonc
{
  "tree": { "key": "...", "commit": "...", "shape": "..." },
  "runs": [
    {
      "tool": "contextlake",
      "version": "...",
      "ok": true,
      "seconds": 0.0,
      "nodes": { "total": 0 },
      "edges": { "rows": 0, "distinct_relationships": 0, "rows_per_relationship": 1.0 }
    }
  ]
}
```

`seconds` is wall clock for the index command only, on one machine, and is the least portable
number here. Treat it as an order of magnitude.

## If you get different numbers

That is the point of committing the harness. Both tools change; a newer version of either will move
these figures. Re-run it, and if the result contradicts what this project publishes, the result
wins.
