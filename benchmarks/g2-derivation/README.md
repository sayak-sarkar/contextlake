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
retrieve it, so a hit is semantic rather than lexical: a substring matcher would rank it
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
| `FAIL` | it did not move, or moved wrongly, and the bar caught it |
| `????` | the bar was **not tested**: a command failed, or an output was never produced |

`????` counts against the run. G2 asks whether a bar was *proven*, and a bar that could not
be tested has not been. A summary that treated it as a pass would report the gate closed on
evidence nobody gathered. That is the precise failure this whole release series has been
removing from the product's own commands.

The deciding half lives in [`checks.py`](checks.py), separated from the I/O so it can be
tested without a network: see `tests/test_g2_derivation_checks.py`, which breaks every
assertion to confirm each one fails for its own reason.

## Status

**All seven bars verified against the pinned tree**, recorded in
[`results/derivation.json`](results/derivation.json). Re-run it and diff that file.

Two things about how it got there are worth keeping, because both were caught rather than
avoided. The harness was first written without being able to run it, and its measurement
patterns were guesses: five of seven bars could never have passed and three would have
reported the product broken. And the pinned commit it carried did not exist in that
repository at all, written from memory while the network was down. Measurement code that has
never run against its real subject is a hypothesis, not a harness.

## See also

- [The head-to-head harness](../head-to-head/README.md)
- [What contextlake saves](../../docs/benchmarks.md)
