# Results

Produced by `run.py` on 2026-08-16, contextlake 7.9.0 against `@colbymchenry/codegraph@1.5.0`,
on one Linux machine. Read `README.md` first: the unit matters, and this is a coverage measure,
not a quality one. The raw output is in `results/*.json`.

## All four trees

`rel` is `distinct_relationships`, the only column meant for comparison.

| Tree | Language | contextlake nodes | comparator nodes | contextlake rel | comparator rel |
| --- | --- | ---: | ---: | ---: | ---: |
| `fmt` | C++ | **7,730** | 7,695 | **15,282** | 14,724 |
| `curl` | C | **18,659** | 16,521 | 38,540 | **40,793** |
| `flask` | Python | 2,207 | **2,705** | **4,815** | 4,692 |
| `express` | JavaScript | 783 | **1,084** | 766 | **1,240** |

**contextlake leads on both measures on one tree of four, on one of the two measures on two
more, and on neither on the JavaScript tree.** Four of the eight cells. That is the honest
summary and it is why this file exists: a comparison showing only `fmt` would be true and
useless.

## What changed since the first run, and why

The first run of this harness (2026-08-16, contextlake 7.7.0) is what produced the finding
below, and acting on it moved two of these trees. Both figures are from the same harness at the
same pinned commits, so they are directly comparable:

| Tree | nodes before | nodes after | rel before | rel after |
| --- | ---: | ---: | ---: | ---: |
| `express` | 320 | **783** | 303 | **766** |
| `flask` | 1,959 | **2,207** | 4,567 | **4,815** |
| `curl` | 18,531 | 18,659 | 38,412 | 38,540 |
| `fmt` | 7,721 | 7,730 | 15,273 | 15,282 |

The `express` and `flask` movement is the intended effect. The small `curl` and `fmt` movement
is a side effect worth naming rather than glossing: both trees carry Python helper scripts, and
those now contribute module-level names too.

**flask crossed over.** It was behind on both measures and now leads on relationships while
still trailing on nodes.

## The finding that produced the change

On the first run, `express` showed contextlake at 320 nodes to the comparator's 1,084. It was
not that contextlake read fewer files: it produced 142 file nodes to the comparator's 141, so
both walked the same tree. The difference was which *kinds* of symbol each one emitted, and one
category accounted for most of it.

contextlake emits five symbol kinds that a tree-sitter definition query cannot express (data
members, macros, typedefs, enum constants, file-scope variables), and that extraction was
implemented for C and C++ only, by an explicit language check. It was a deliberate scope, and
this benchmark is what put a number on what the scope cost elsewhere. Extending it to JavaScript
and Python is the change measured above.

### express, by node kind, after the change

| Node kind | contextlake | comparator |
| --- | ---: | ---: |
| variable / global_variable | 463 | 461 + 2 constants |
| route / endpoint | 47 | 266 |
| function | 86 | 214 |
| file | 142 | 141 |
| package | 45 | 0 |

The variable category is now at parity. **The remaining 301-node gap is two specific things**,
and the arithmetic closes exactly: routes (219) and functions (128), less the 45 `package` nodes
only contextlake emits and one file.

- **Routes.** The comparator emits 266 route nodes to contextlake's 47 endpoints on a repo whose
  entire purpose is routing.
- **Functions.** 214 to 86. contextlake's definition query recognises declared functions and
  methods; a JavaScript codebase of this vintage is largely function expressions and arrow
  functions passed as arguments.

Neither is fixed here. They are named because a gap you can decompose is worth more than a gap
you can only report.

## What this does not show

Nothing here measures whether an edge points at the right target, or whether a node is worth
having. The change above added 463 nodes to `express` and every one of them is a real
module-level binding, but a `const` holding a `require()` alias is a weaker answer to "what is
in this codebase" than a function is. Both tools could be equally confident and equally wrong and
these counts would not tell you. A precision comparison needs a labelled answer set this harness
does not have.

Neither does it cover what is not counted in nodes and edges: call-site provenance on every edge,
citation verification, the offline guarantee, the absence of telemetry, and the cross-repository
fleet layer have no counterpart column here. Those are stated separately, with source citations,
rather than smuggled into a graph-size number.

## Reproducing

```bash
python benchmarks/head-to-head/run.py --all
```

If your numbers differ from these, the numbers you produced are the ones that count. Both tools
move; this file records one dated run, not a standing fact.

## See also

- [The harness, and what it measures](README.md)
- [What contextlake saves](../../docs/benchmarks.md)
