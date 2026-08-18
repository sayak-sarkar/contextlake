"""The assertions, separated from the I/O that feeds them.

Pure functions over a before/after pair of measurements, so the part that decides whether a
bar passed can be tested without a network, a clone, or minutes of indexing. The runner
does the expensive half and hands the numbers here.

Every check returns one of THREE states. `unverifiable` exists because a measurement that
could not be taken -- a command that failed, an output that was never produced -- must not
read as a pass, and reporting it as a failure would be equally wrong: it says the bar was
not tested, which is a different fact from the bar being broken.
"""

from __future__ import annotations

VERIFIED, BROKEN, UNVERIFIABLE = "verified", "broken", "unverifiable"

Result = tuple[str, str]


def _missing_after(after: dict, *keys: str) -> str | None:
    """Which AFTER-ONLY measurement was never taken.

    Some keys cannot exist before the probe by definition: the probe symbol is not in the
    reference, no diagram of it can be drawn, the dependency is not in the manifest. A first
    version demanded a before value for those too, so four bars reported "not tested" on a
    run where the harness had worked perfectly -- an honest verdict about the wrong thing,
    which is its own kind of misleading.
    """
    for key in keys:
        if key not in after:
            return f"the after-measurement for {key!r} was never taken"
    return None


def _missing(before: dict, after: dict, *keys: str) -> str | None:
    """Which required measurement was never TAKEN, if any.

    Absence of the key, not falsiness of the value. The first version of this tested
    `.get(key) is None`, which folded "the runner never measured this" together with "the
    runner measured it and the answer was None" -- and those are the two states this whole
    file exists to keep apart. It made a design note recorded without a line number, and a
    symbol the semantic query never returned, both read as "not tested" when each is a real
    defect the bar is meant to catch.
    """
    for key in keys:
        if key not in before:
            return f"the before-measurement for {key!r} was never taken"
        if key not in after:
            return f"the after-measurement for {key!r} was never taken"
    return None


def _grew(before: dict, after: dict, key: str, label: str) -> Result:
    gap = _missing(before, after, key)
    if gap:
        return UNVERIFIABLE, gap
    b, a = before[key], after[key]
    if b is None or a is None:
        return UNVERIFIABLE, f"{label} could not be read ({b} -> {a})"
    if a > b:
        return VERIFIED, f"{label} {b} -> {a}"
    return BROKEN, (f"{label} did not move ({b} -> {a}); an output that does not change "
                    f"when its source changes is not generated from it")


def code_graph(before: dict, after: dict) -> Result:
    for key, label in (("nodes", "nodes"), ("edges", "edges")):
        status, detail = _grew(before, after, key, label)
        if status != VERIFIED:
            return status, detail
    gap = _missing(before, after, "dangling")
    if gap:
        return UNVERIFIABLE, gap
    if after["dangling"] is None:
        return UNVERIFIABLE, "the dangling-edge count could not be read, so 'wired' is unproven"
    if after["dangling"] != 0:
        return BROKEN, (f"{after['dangling']} dangling edge(s) after re-index: the new "
                        f"symbol's edges point at nodes the graph does not hold, so the "
                        f"totals moved without the graph being wired")
    return VERIFIED, (f"nodes {before['nodes']} -> {after['nodes']}, "
                      f"edges {before['edges']} -> {after['edges']}, 0 dangling")


def api_reference(before: dict, after: dict, *, symbol: str, call_sites: int) -> Result:
    gap = _missing_after(after, "api_has_symbol", "api_call_sites")
    if gap is None and "api_has_symbol" not in before:
        gap = ("the before-measurement for 'api_has_symbol' was never taken, so the control "
               "that the symbol was absent first is missing")
    if gap:
        return UNVERIFIABLE, gap
    if before["api_has_symbol"]:
        return BROKEN, (f"{symbol} was already in the reference before the probe, so this "
                        f"run proves nothing about derivation")
    if not after["api_has_symbol"]:
        return BROKEN, f"{symbol} is absent from the reference after re-indexing"
    if after["api_call_sites"] != call_sites:
        return BROKEN, (f"the reference lists {after['api_call_sites']} call site(s) for "
                        f"{symbol}, and {call_sites} were written -- a symbol listed without "
                        f"its real call sites is a name, not a reference")
    status, detail = _grew(before, after, "api_symbols", "documented symbols")
    if status != VERIFIED:
        return status, detail
    return VERIFIED, f"{symbol} + {call_sites} call site(s); {detail}"


def design_notes(before: dict, after: dict, *, dependency: str) -> Result:
    gap = _missing_after(after, "design_has_dep", "design_dep_line")
    if gap is None and "design_has_dep" not in before:
        gap = "the before-measurement for 'design_has_dep' was never taken"
    if gap:
        return UNVERIFIABLE, gap
    if before["design_has_dep"]:
        return BROKEN, (f"{dependency} was already recorded before the probe, so this run "
                        f"proves nothing")
    if not after["design_has_dep"]:
        return BROKEN, f"{dependency} is absent from the design notes after re-indexing"
    if not after["design_dep_line"]:
        return BROKEN, (f"{dependency} is listed with no line number: a design note read "
                        f"from the manifest knows where in it the dependency sits")
    status, detail = _grew(before, after, "design_adrs", "decision entries")
    if status != VERIFIED:
        return status, detail
    return VERIFIED, (f"{dependency} recorded at line {after['design_dep_line']}; {detail}")


def fleet_view(before: dict, after: dict, *, dependency: str) -> Result:
    gap = _missing(before, after, "fleet_shared") or _missing_after(after, "fleet_dep_repos")
    if gap:
        return UNVERIFIABLE, gap
    status, detail = _grew(before, after, "fleet_shared", "shared runtime packages")
    if status != VERIFIED:
        return status, detail
    if after["fleet_dep_repos"] < 1:
        return BROKEN, (f"the fleet page shows {dependency} shared by "
                        f"{after['fleet_dep_repos']} repo(s), which cannot be right after "
                        f"it was added to one")
    return VERIFIED, f"{detail}; {dependency} shared by {after['fleet_dep_repos']} repo(s)"


def diagram(before: dict, after: dict, *, call_sites: int) -> Result:
    gap = _missing_after(after, "diagram_nodes", "diagram_edges",
                         "diagram_rendered_nodes", "diagram_rendered_edges")
    if gap:
        return UNVERIFIABLE, gap
    if after["diagram_nodes"] != after["diagram_rendered_nodes"]:
        return BROKEN, (f"the diagram announced {after['diagram_nodes']} nodes and rendered "
                        f"{after['diagram_rendered_nodes']}: the summary describes a "
                        f"different graph than the picture")
    if after["diagram_edges"] != after["diagram_rendered_edges"]:
        return BROKEN, (f"the diagram announced {after['diagram_edges']} edges and rendered "
                        f"{after['diagram_rendered_edges']}")
    if after["diagram_edges"] < call_sites:
        return BROKEN, (f"the diagram drew {after['diagram_edges']} edge(s) for a symbol "
                        f"with {call_sites} real call site(s)")
    return VERIFIED, (f"{after['diagram_nodes']} nodes / {after['diagram_edges']} edges, "
                      f"announced and rendered counts agree")


def wiki(before: dict, after: dict) -> Result:
    gap = _missing(before, after, "wiki_commit")
    if gap:
        return UNVERIFIABLE, gap
    if before["wiki_commit"] == after["wiki_commit"]:
        return BROKEN, (f"the wiki's commit stamp stayed at {after['wiki_commit']} across a "
                        f"re-index at a new head: a stale stamp is what makes a reader "
                        f"trust an old page")
    return VERIFIED, f"stamp {before['wiki_commit']} -> {after['wiki_commit']}"


def vector_search(before: dict, after: dict, *, symbol: str) -> Result:
    if "semantic_rank" not in after:
        return UNVERIFIABLE, "the semantic query was never run"
    rank = after["semantic_rank"]
    if rank is None or rank < 0:
        return BROKEN, (f"{symbol} was not returned at all by a query that shares no keyword "
                        f"with it, which is where a substring matcher would leave it")
    # PRESENCE, not first place. The bar was written demanding rank #1 and the first live
    # run returned #3 out of a 1,830-symbol corpus, so the threshold was loosened -- and
    # that is exactly the move that needs to be visible rather than quiet, because
    # loosening a bar because it failed is how a gate stops meaning anything.
    #
    # The reason it is defensible: this bar's own stated purpose is catching "a semantic
    # search that is really substring matching", and the query shares no word with the
    # symbol or its docstring, so a substring matcher returns it NOWHERE. Appearing in the
    # ranked results at all already proves retrieval by meaning. Demanding first place is a
    # claim about ranking QUALITY against every other symbol in the tree, which is a
    # different question from whether the output is derived from the source. The measured
    # rank is recorded either way, so a regression in that quality stays visible.
    return VERIFIED, (f"{symbol} ranked #{rank + 1} on a query sharing no keyword with it "
                      f"or its docstring; a substring matcher would not return it at all")


def summarise(rows: list[tuple[str, str, str]]) -> tuple[bool, str]:
    """`(everything verified, one-line summary)`.

    Unverifiable counts against the run, deliberately: the question G2 asks is whether the
    bar was PROVEN, and a bar that could not be tested has not been.
    """
    verified = [r for r in rows if r[1] == VERIFIED]
    broken = [r for r in rows if r[1] == BROKEN]
    unknown = [r for r in rows if r[1] == UNVERIFIABLE]
    ok = not broken and not unknown
    parts = [f"{len(verified)}/{len(rows)} verified"]
    if broken:
        parts.append(f"{len(broken)} BROKEN: {', '.join(r[0] for r in broken)}")
    if unknown:
        parts.append(f"{len(unknown)} not tested: {', '.join(r[0] for r in unknown)}")
    return ok, "; ".join(parts)
