# Results

Produced by `run.py` on 2026-08-16, contextlake 7.7.0 against `@colbymchenry/codegraph@1.5.0`, on
one Linux machine. Read `README.md` first: the unit matters, and this is a coverage measure, not a
quality one. The raw output is in `results/*.json`.

## All four trees

`rel` is `distinct_relationships`, the only column meant for comparison.

| Tree | Language | contextlake nodes | comparator nodes | contextlake rel | comparator rel |
| --- | --- | ---: | ---: | ---: | ---: |
| `fmt` | C++ | **7,721** | 7,695 | **15,273** | 14,724 |
| `curl` | C | **18,531** | 16,521 | 38,412 | **40,793** |
| `flask` | Python | 1,959 | **2,705** | 4,567 | **4,692** |
| `express` | JavaScript | 320 | **1,084** | 303 | **1,240** |

**contextlake leads on one tree of four.** It is ahead on both measures on `fmt`, ahead on nodes
but behind on relationships on `curl`, behind on both on `flask`, and behind by more than 3x on
`express`. That is the honest summary and it is the reason this file exists: a comparison that only
showed `fmt` would be true and useless.

## What the numbers say

**The margin is tree-dependent, and the direction changes.** This was predicted before the run and
is now measured. Any single-tree claim in either direction is unsafe.

**The `express` gap is the largest and has a specific cause.** It is not that contextlake read
fewer files: contextlake produced 142 file nodes to the comparator's 141, so both walked the same
tree. The difference is which *kinds* of symbol each one emits.

| Node kind | contextlake | comparator |
| --- | ---: | ---: |
| variable | 0 | 461 |
| constant | 0 | 2 |
| route / endpoint | 47 | 266 |
| function | 86 | 214 |
| file | 142 | 141 |
| package | 45 | 0 |

Roughly 460 of the 764-node gap is a single missing category: contextlake emits no node for a
variable or a constant, in any language. That is a known gap with existing work planned against it,
and this run is the first public, reproducible measurement of what it costs.

**contextlake emits 45 `package` nodes the comparator emits none of**, which is the dependency
layer. It is a real difference in the other direction and it is not captured by a node total.

**Timing is not a differentiator at this scale.** contextlake indexed every tree faster (0.56s to
4.98s against 1.33s to 5.49s), but these are seconds on small trees and the number is machine and
cache dependent. It is not worth an argument.

## What this does not show

Nothing here measures whether an edge points at the right target. Both tools could be equally
confident and equally wrong, and these counts would not tell you. A precision comparison needs a
labelled answer set that this harness does not have; until that exists, treat every figure above as
coverage only.

Neither does it cover the things that are not counted in nodes and edges: contextlake's
call-site provenance on every edge, its citation verification, its offline guarantee, its absence
of telemetry, and its cross-repository fleet layer have no counterpart column in this table. They
are stated separately, with source citations, rather than smuggled into a graph-size number.

## Reproducing

```bash
python benchmarks/head-to-head/run.py --all
```

If your numbers differ from these, the numbers you produced are the ones that count. Both tools
move; this file records one dated run, not a standing fact.
