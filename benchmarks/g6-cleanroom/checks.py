"""What a clean-room run has to show, separated from the running of it.

Same split as the derivation harness next door, for the same reason: the part that decides
whether the gate passed can then be tested without installing anything.

Three states. `unverifiable` is not a softer failure, it is a different fact: the step did
not run, so the thing it would have proved is unproven. It counts against the run, because
the question this gate asks is whether a clean machine can do the whole thing, and a step
that never executed has not answered it.
"""

from __future__ import annotations

VERIFIED, BROKEN, UNVERIFIABLE = "verified", "broken", "unverifiable"

Result = tuple[str, str]

#: The six output types the charter names. Kept here rather than inline so the runner and
#: the report cannot drift about what "all six" means.
OUTPUTS = ("code graph", "wiki", "generated docs", "diagrams", "vector search", "fleet view")


def installed_version(reported: str | None, expected: str) -> Result:
    """The version the INSTALLED command reports, not the one the repository holds.

    The charter singles this out because an editable install has masked a version mismatch
    twice: the working tree and the published artefact were different builds, and every
    check that read the tree agreed with itself.
    """
    if reported is None:
        return UNVERIFIABLE, "the installed command did not report a version"
    got = reported.strip().split()[-1]
    if got == expected:
        return VERIFIED, f"contextlake {got}, installed from the index"
    return BROKEN, f"the installed command reports {got!r}, the release is {expected!r}"


def produced_outputs(produced: dict[str, bool]) -> Result:
    """All six, named individually so a partial run cannot read as a whole one."""
    missing = [name for name in OUTPUTS if not produced.get(name)]
    unknown = [name for name in OUTPUTS if name not in produced]
    if unknown:
        return UNVERIFIABLE, f"never checked: {', '.join(unknown)}"
    if missing:
        return BROKEN, f"not produced: {', '.join(missing)}"
    return VERIFIED, f"all {len(OUTPUTS)}: {', '.join(OUTPUTS)}"


def reindex_is_quiet(first: int | None, second: int | None) -> Result:
    """A second index over an unchanged tree must not rebuild it.

    The charter names this shape because it has broken before and the failure is invisible:
    a re-index that silently redoes everything still ends with a correct store, so nothing
    downstream complains while every run costs full price.

    `first` must be POSITIVE. A first version accepted zero, and with the output this
    actually parses that meant both runs read as zero and the check reported "second rebuilt
    nothing" for a pair where neither number had been measured at all. A check that passes
    when its own measurement failed is worse than no check.
    """
    if first is None or second is None:
        return UNVERIFIABLE, "one of the two index runs did not report a rebuild count"
    if first <= 0:
        return UNVERIFIABLE, (f"the first index reported {first} repositories rebuilt, so "
                              f"there is no baseline to compare against; the fixture indexes "
                              f"one repository, so this means the count was not read")
    if second > 0:
        return BROKEN, (f"the second index rebuilt {second} repository(ies) over an "
                        f"unchanged tree; the first built {first}")
    return VERIFIED, f"first run rebuilt {first}, second rebuilt nothing"


def offline_run(refused: bool | None, exit_code: int | None, command: str = "") -> Result:
    """`--offline` has to REFUSE a command that would talk to the network.

    A first version ran `kb lint` under poisoned proxies and called a zero exit proof. But
    `kb lint` reads the local store and local git heads and says so in its own docstring: it
    has no network path, so nothing was being tested. The check now runs a command the CLI
    explicitly lists as network-bound and requires the guard to stop it.
    """
    if refused is None or exit_code is None:
        return UNVERIFIABLE, f"the offline run of {command or 'the guarded command'} did not "\
                             f"complete"
    if not refused:
        return BROKEN, (f"`--offline {command}` was not refused (exit {exit_code}); the flag "
                        f"is a preference rather than a promise")
    return VERIFIED, f"`--offline {command}` refused, exit {exit_code}"


def repo_without_manifest(exit_code: int | None, symbol_found: bool | None) -> Result:
    """A tree with no manifest is a repository, not an error.

    Named by the charter because dependency reading is the newest code path and the one
    most likely to assume a manifest exists.
    """
    if exit_code is None or symbol_found is None:
        return UNVERIFIABLE, "the no-manifest case did not run"
    if exit_code != 0:
        return BROKEN, f"indexing a tree with no manifest exited {exit_code}"
    if not symbol_found:
        # A repository ROW is not a graph. A first version counted rows and passed because
        # the previous repository had already made the count two.
        return BROKEN, ("a tree with no manifest produced no symbol of its own in the "
                        "graph, so the row it added is empty")
    return VERIFIED, "indexed, its own symbol is in the graph, and no manifest to read"


def summarise(rows: list[tuple[str, str, str, str]]) -> tuple[bool, str]:
    """`(every check verified on every interpreter, one-line summary)`.

    Rows are `(python, check, status, detail)`. The interpreter is part of the identity of
    a result: "it passed" without saying where is the claim this gate exists to refuse.
    """
    broken = [r for r in rows if r[2] == BROKEN]
    unknown = [r for r in rows if r[2] == UNVERIFIABLE]
    verified = [r for r in rows if r[2] == VERIFIED]
    if not rows:
        # A gate that passes when nothing ran is the purest form of the defect this whole
        # file is about. An earlier test blessed exactly this.
        return False, "no check ran, so the clean room proved nothing"
    parts = [f"{len(verified)}/{len(rows)} verified"]
    if broken:
        parts.append("BROKEN: " + ", ".join(f"{p}:{c}" for p, c, _s, _d in broken))
    if unknown:
        parts.append("not tested: " + ", ".join(f"{p}:{c}" for p, c, _s, _d in unknown))
    return (not broken and not unknown), "; ".join(parts)
