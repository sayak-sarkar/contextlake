# Results

Produced by `run.py` on 2026-08-24, contextlake 8.6.1 against `@colbymchenry/codegraph@1.5.0`,
on one Linux machine. Read `README.md` first: the unit matters, and this is a coverage measure,
not a quality one. The raw output is in `results/*.json`.

## All four trees

`rel` is `distinct_relationships`, the only column meant for comparison. **Read the section
below it before comparing `rel` against the 2026-08-16 run: it is not the same measure.**

| Tree | Language | contextlake nodes | comparator nodes | contextlake rel | comparator rel |
| --- | --- | ---: | ---: | ---: | ---: |
| `fmt` | C++ | **7,752** | 7,695 | **17,036** | 14,724 |
| `curl` | C | **19,652** | 16,521 | **63,367** | 40,793 |
| `flask` | Python | 2,286 | **2,705** | **5,064** | 4,692 |
| `express` | JavaScript | 805 | **1,084** | 1,080 | **1,240** |

**contextlake leads on both measures on two trees of four, on one of the two measures on a
third, and on neither on the JavaScript tree.** Five of the eight cells. That is the honest
summary and it is why this file exists: a comparison showing only `fmt` would be true and
useless.

## The comparator reproduced exactly, and that is the load-bearing control

Every comparator figure above is **byte-identical** to the 2026-08-16 run: 16,521/40,793 on
`curl`, 1,084/1,240 on `express`, 2,705/4,692 on `flask`, 7,695/14,724 on `fmt`. Same pinned
version, same pinned tree commits, same numbers eight days later.

That licenses attributing every moved cell to contextlake, and it is worth publishing on its own:
the harness is deterministic.

## Why `rel` is NOT comparable to the 2026-08-16 run

`curl`'s relationship count rose 64% in eight days. Almost none of that is better analysis of the
same thing. **Version 7.14.0 added a relation that did not exist at 7.9.0**: `uses`, emitted when
a bare name reads a constant. `git show v7.9.0:src/contextlake/kb/parse.py` contains no such
relation, and the comparator at 1.5.0 has no counterpart to it.

The 7.14.0 changelog measured that relation at **+63% of all edges on a macro-heavy C++ tree**.
`curl` is macro-heavy C, and `curl` moved +64%. The delta is the new relation arriving where its
own release notes said it would.

Two further changes widened what counts as a node, both concentrated on `curl`:

| Release | Change | Effect on these trees |
| --- | --- | --- |
| 7.10.0 | Perl, Bash, CSS and HTML became parsed languages | `curl` gains 74 Perl and 29 shell files that 7.9.0 read as unsupported extensions |
| 7.11.0 | Name-based grammar routing; Makefile targets become nodes | `curl` gains 35 build files; `flask` gains 1 |

So: **against the comparator, on this dated run, the table above is a fair comparison.** Across
dates it is not a like-for-like delta, and no signed percentage against 2026-08-16 belongs in
this file. Both runs are published; compare them yourself knowing what changed underneath.

`seconds` is excluded from this page entirely for a separate reason. Wall-clock moved 1.8x to
3.4x on **both** tools between the two runs, uniformly, which is machine contention rather than
either tool changing. README.md already calls `seconds` the least meaningful column; on this run
it is not meaningful at all.

## What changed between 7.7.0 and 7.9.0

**A closed historical record. It is deliberately not extended with an 8.6.1 column.** A
7.7.0-to-8.6.1 row would span two harness revisions and fold three separate definition changes
into one unattributed number.

| Tree | nodes before | nodes after | rel before | rel after |
| --- | ---: | ---: | ---: | ---: |
| `express` | 320 | **783** | 303 | **766** |
| `flask` | 1,959 | **2,207** | 4,567 | **4,815** |
| `curl` | 18,531 | 18,659 | 38,412 | 38,540 |
| `fmt` | 7,721 | 7,730 | 15,273 | 15,282 |

The node and relationship columns of that table are comparable to each other: the only harness
change between those two runs (`0cd21a73`, deleting the store before each run) affected `seconds`
alone. The `express` and `flask` movement was the intended effect of the change described below.
The small `curl` and `fmt` movement was a side effect: both carry Python helper scripts, which
began contributing module-level names.

**flask crossed over** in that interval, and still leads on relationships while trailing on nodes.

## The finding that produced the change

On the 2026-08-16 first run, `express` showed contextlake at 320 nodes to the comparator's 1,084. It was
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
