"""Every command that writes the store must take the store's single-writer lock.

`_guard_store`'s own docstring promises "two writers never interleave on one store".
That promise was false for four of the seven writing commands: `ingest`, `connect`,
`enrich` and `forget` opened the store and wrote rows -- and shard files -- without ever
asking for the lock. Proved by running it: with a live lock held, `kb index` refused,
`kb ingest` wrote, and `kb forget` **deleted**.

The realistic trigger is not two humans racing. It is contextlake's own detached
background index, which the session hook spawns and which holds the lock for the whole
run -- so any `ingest`/`forget` a user starts in that window writes straight through it.

**Why this is a parity test rather than four separate ones.** The defect was not that
somebody wrote a bad guard; it was that four commands were never considered. A test that
checks the four known offenders would pass forever while the eighth writer is added
unguarded. So this enumerates the *writers* and asserts each is guarded, and the list of
writers is derived from the code rather than typed here -- the same lesson as
"enumerate every producer of the result type, not every caller of the funnel".
"""

from __future__ import annotations

import ast
import pathlib

CMDS = pathlib.Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb" / "cmds"

# Commands that write to the store. Each one either upserts rows, writes shards, or
# deletes. Read-only verbs (query, lint, owners, refresh, doctor, graph, dashboard...)
# deliberately do NOT take the lock: blocking a read behind a long index would make the
# tool unusable during its own refresh.
WRITERS = {
    "index": "index",
    "embed": "embed",
    "wiki": "wiki",
    "ingest": "ingest",
    "connect": "connect",
    "enrich": "enrich",
    "forget": "forget",
}


def _guard_labels(path: pathlib.Path) -> list[str]:
    """The label passed to every `_guard_store(store_dir, "<label>")` call in a module.

    Parsed rather than grepped so a call inside a comment or a docstring cannot satisfy
    the assertion -- this test exists because something looked present and was not.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_guard_store"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)):
            found.append(node.args[1].value)
    return found


def test_every_store_writing_command_takes_the_single_writer_lock():
    """THE LOAD-BEARING ASSERTION. Fails for ingest/connect/enrich/forget before the fix."""
    missing = []
    for module, label in sorted(WRITERS.items()):
        path = CMDS / f"{module}.py"
        assert path.is_file(), f"{module}.py moved; update WRITERS"
        labels = _guard_labels(path)
        if label not in labels:
            missing.append(f"{module}.py (guards found: {labels or 'none'})")
    assert not missing, (
        "these commands write the store without taking its single-writer lock, so a "
        "concurrent writer -- including contextlake's own detached background index -- "
        "can interleave with them:\n  " + "\n  ".join(missing))


def test_the_guard_runs_before_any_work_and_closes_the_store_when_refused():
    """A guard that fires after the writes, or that leaks the handle on refusal, would
    satisfy the test above while fixing nothing. Pins both: `_guard_store` is called in
    the same statement block as `_open_store`, and the refusal path closes and returns."""
    for module, label in sorted(WRITERS.items()):
        src = (CMDS / f"{module}.py").read_text(encoding="utf-8")
        i_open = src.find("_open_store(args)")
        i_guard = src.find(f'_guard_store(store_dir, "{label}")')
        assert 0 < i_open < i_guard, f"{module}: the guard does not follow _open_store"
        between = src[i_open:i_guard]
        assert "upsert" not in between and "write_shard" not in between, (
            f"{module}: the store is written before the lock is taken")
        after = src[i_guard:i_guard + 200]
        assert "store.close()" in after and "return 1" in after, (
            f"{module}: the refusal path must close the store and fail, not fall through")


def test_the_writer_list_has_not_silently_shrunk():
    """The near-miss. If somebody deletes a row from WRITERS, the sweep above still
    passes -- vacuously. Pin the count so a removal has to be deliberate."""
    assert len(WRITERS) == 7, (
        f"WRITERS holds {len(WRITERS)} entries; it should hold 7. Adding a store-writing "
        "command means adding it here too, and removing one means saying why.")
