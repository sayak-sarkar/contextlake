# Derivation evidence for the six output types

Each of contextlake's six output types could be satisfied by a fixture, a cached sample, or
by reading a README. So "it appeared after indexing" proves nothing about where it came
from. This harness changes **one thing** in a pinned public tree, re-indexes, and asserts
the specific movement that change implies. **A page that does not move when its source
moves is not generated from it.**

The bars are in [`bars.json`](bars.json), written before the test rather than after it, each
naming a failure it would catch. Seven bars cover the six types: generated docs are split
into the API reference and the design notes, which move for different reasons.

## The probe

One new function called from five places, and one new runtime dependency.

The function exercises the graph, the API reference's call-site list, the diagram and vector
search in a single change. Its docstring shares **no keyword** with the query used to
retrieve it, so a hit is semantic rather than lexical — a substring matcher would rank it
nowhere, which is exactly what makes that bar meaningful.

The dependency exercises the design notes and the fleet page. The tree is chosen for having
**zero** runtime dependencies, so before and after are unambiguous.

## Running it

Not run in CI: it needs the network and minutes of indexing.

```bash
python benchmarks/g2-derivation/run.py
python benchmarks/g2-derivation/run.py --keep   # leave the work tree for inspection
```

Everything it writes lives under `--work-dir` (default: a playground directory outside the
repo) and `results/`. Every contextlake invocation carries an explicit `--config` pointing
inside the work directory, so a run cannot touch a store the operator already has.

## Reading the result

Three states, never two:

| | meaning |
| --- | --- |
| `ok` | the output moved in the way the bar requires |
| `FAIL` | it did not move, or moved wrongly — the bar caught something |
| `????` | the bar was **not tested**: a command failed, or an output was never produced |

`????` counts against the run. G2 asks whether a bar was *proven*, and a bar that could not
be tested has not been. A summary that treated it as a pass would report the gate closed on
evidence nobody gathered — the precise failure this whole release series has been removing
from the product's own commands.

The deciding half lives in [`checks.py`](checks.py), separated from the I/O so it can be
tested without a network: see `tests/test_g2_derivation_checks.py`, which breaks every
assertion to confirm each one fails for its own reason.

## Status

The harness and its bars are committed. `results/derivation.json` is written by a live run;
until one has been done against the pinned tree, **G2 is not closed** — a harness that has
never run is not evidence, and the earlier hand-run derivation matrix in the planning notes
is not repeatable, which is the specific gap this directory exists to fill.
