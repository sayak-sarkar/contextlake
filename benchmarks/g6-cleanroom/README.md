# Clean-room install

Can a machine with no contextlake config and no store install this from the index and do the
whole thing? Verified from the **published artefact**, never the working tree, because an
editable install has masked a version mismatch twice: the tree and the released build were
different, and every check that read the tree agreed with itself.

One happy path is not a clean room. This runs on the **minimum supported interpreter and the
newest**, and includes the shapes that have actually broken before: a second index over an
unchanged tree, an `--offline` run, and a repository with no manifest at all.

```bash
python benchmarks/g6-cleanroom/run.py --version 7.30.0
python benchmarks/g6-cleanroom/run.py --version 7.30.0 --pythons 3.10,3.13 --keep
```

Each interpreter gets its own `HOME`, so "no config" is a fact rather than an assumption,
and every invocation carries an explicit `--config` inside that home.

## What each check proves

| check | catches |
| --- | --- |
| installed version | the released build differing from the tree, which an editable install hides |
| six outputs | a partial run reading as a whole one, so each type is named individually |
| re-index is quiet | a second index silently rebuilding an unchanged tree, which ends with a correct store and costs full price every run |
| offline run | `--offline` being a preference rather than a promise |
| no-manifest repo | dependency reading assuming a manifest exists |

The offline check poisons every proxy variable, so any outbound call fails loudly and a
command that still exits 0 did not make one. That is stronger than reading the flag's own
logging, which only reports what the code believes about itself.

Three states, never two. A check that did not run is `????` and counts against the gate: the
question is whether a clean machine *can* do this, and a step that never executed has not
answered it.

## Status

**10 of 10 verified** on Python 3.10 and 3.13 against the published 7.30.0 wheel, recorded in
[`results/cleanroom.json`](results/cleanroom.json).

That number is only worth reading because of what a review found in the version that first
reported it. Six checks were wrong, and the harness passed anyway:

1. **`init` failing was converted into a pass.** It wrote its own config and returned
   success, so the gate could be green while the shipped `init` was broken. `init` working on
   a clean machine is most of what this gate is for.
2. **"Re-index is quiet" read zero for both runs.** The regex matched a line `kb index` never
   prints, and the fallback matched the FIRST run's own `0 unchanged`, so the check reported
   "second rebuilt nothing" for a pair where nothing had been measured. A positive first
   count is now required, because the fixture indexes one repository.
3. **The offline check tested nothing.** It ran `kb lint`, which reads the local store and
   says offline in its own docstring, under poisoned proxies. It now runs `mirror fetch`,
   which the CLI lists as network-bound, and requires the guard to refuse it. That needed a
   configured group: without one, config validation refuses first and the guard is never
   consulted, and the earlier version read that refusal as the guard speaking.
4. **"Vector search" accepted a full-text fallback.** `kb query` degrades silently when the
   embedder is unavailable, so non-empty results proved nothing about the retriever named.
5. **The no-manifest check only proved a second repository ROW existed**, which the
   previously indexed tree had already made true. It now looks for that tree's own symbol.
6. **A run where nothing executed passed.** `summarise([])` returned success, and a test
   blessed it.

Every one is the defect class this project has spent a release series removing: a surface
reporting a result it did not measure. A harness is not exempt from it, and a harness that
grades a gate is the worst place for it to hide.

## See also

- [The head-to-head harness](../head-to-head/README.md)
- [What contextlake saves](../../docs/benchmarks.md)
