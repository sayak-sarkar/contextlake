"""The design document: what this repository's own files record about how it was built.

Distinct from the API reference next door, which answers "what can I call". This answers
"what was chosen", and the honest answer is narrower than the name "design document"
suggests, so the page says so in its first paragraph rather than in a footnote.

**Nothing here is a decision record.** A decision record states what was chosen, what was
rejected, and why. A graph holds none of that. What it holds is two kinds of evidence, and
the difference between them is the organising idea of this module:

- **Recorded**: a manifest dependency. Somebody wrote ``blinker>=1.9.0`` in a file on
  purpose. The choice, its constraint and its line are facts, not inferences, and they carry
  ``EXTRACTED`` confidence for exactly that reason.
- **Inferred**: a constant read in many places. That is evidence the value is load-bearing.
  It is NOT evidence that anybody decided anything, and the page must not blur the two.

Measured before any of this was written (`planning/measurement-adr-evidence-2026-08-17.md`):
on a mature library, seven constants survive a defensible evidence rule and roughly four of
them point at something a human would call a decision. The other three are typing
constructs. That ratio is why this module states measurements and refuses to characterise
them: it has no way to tell the four from the three, and a generated sentence calling a
type variable "a core architectural decision" is worse than no sentence.

The same rule already governs the wiki's gotchas prompt, which may state a caller count and
may not explain what the count means. This inherits it.
"""

from __future__ import annotations

from collections import defaultdict

from ..mdwrite import code, table
from ..model import Confidence
from .stamp import stamp

# A token, not only a sentence. Whoever reads this file receives bytes rather than a rendered
# page, and "these are proposals nobody ratified" stated only in a paragraph is a sentence a
# summariser can drop; a marker is something a reader can match on. An HTML comment because
# it is invisible in every Markdown renderer and present in every byte stream, which is
# exactly the pair of properties this needs.
#
# This was once justified by NOT claiming "the MCP server serves this", because at the time
# it did not: the server exposed only the store's `wiki/` directory. That has since stopped
# being true -- `get_generated_doc` serves `docs/api/` and `docs/design/`, and
# `get_fleet_doc` serves `docs/fleet/` -- so the comment is corrected rather than left
# stating a checked fact that expired. The marker earned its place before any of that, which
# is the part worth keeping: it was not added because something reads it.
STATUS_MARKER = (
    "<!-- contextlake:document=design status=proposed-never-ratified "
    "evidence=derived-from-code -->"
)

# Ordered, because a reader wants what the software cannot start without before what a
# contributor installs. Anything else (an extra, a PEP 735 group) sorts after these by name.
_GROUP_ORDER = {"runtime": 0, "peer": 1, "dev": 2}

_GROUP_TITLES = {
    "runtime": "Required at runtime",
    "peer": "Expected to be provided by the host project",
    "dev": "Development and test only",
}

# Which groups become a numbered entry rather than only a table row. A runtime or peer
# dependency is a commitment the software cannot run without, so it is a decision in the
# ordinary sense. A dev dependency is a contributor's convenience and an optional extra is
# something a user opts into: real facts, recorded in the tables, but promoting them to
# numbered decisions would flatten a distinction the graph took work to record.
_ADR_GROUPS = frozenset({"runtime", "peer"})

# What a constant has to clear before the page will print it. Each threshold is one the
# measurement showed does real work; none is a round number picked for looking careful.
MIN_USE_SITES = 3
MIN_USE_FILES = 2

# Numbered entries are bounded because an application repository's root manifest can carry
# over a hundred runtime dependencies (112 in one measured tree). The tables below the
# entries stay complete either way, so the cap costs a reader nothing they cannot recover.
DEFAULT_MAX_ADRS = 50

# Per MANIFEST, and set from the measured distribution rather than from taste. Counting
# dependencies in four public application repositories gave 69, 112, 173 and 230 in a single
# manifest, so any cap in the dozens would truncate the ordinary case. This is a safety bound
# against a generated manifest with thousands of entries, not a readability one: a dependency
# list IS a reference, and a truncated reference cannot be told from a short one.
DEFAULT_MAX_DEPS_PER_MANIFEST = 500
DEFAULT_MAX_VALUES = 25

# A declaration is captured at up to 200 characters, which is right for storage and far too
# wide for a table cell: one real declaration whose right-hand side opened a help string
# filled its row with prose. Cut for DISPLAY only, and visibly, so the cell never implies
# the value ends where the text stops.
MAX_DECLARATION_SHOWN = 72
_ELLIPSIS = " ..."


def _shown_declaration(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) <= MAX_DECLARATION_SHOWN:
        return text
    return text[:MAX_DECLARATION_SHOWN - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


_CONSTANT_KINDS = frozenset({"global_variable", "enum_constant", "macro"})


def _group_title(group: str) -> str:
    if group in _GROUP_TITLES:
        return _GROUP_TITLES[group]
    kind, _, name = group.partition(":")
    if kind == "optional" and name:
        return f"Optional extra `{name}`"
    if kind == "group" and name:
        return f"Dependency group `{name}`"
    return group or "Ungrouped"


def _dependencies(shard) -> dict[str, list[tuple]]:
    """`{manifest path: [(package, constraint, group, line), ...]}`.

    Keyed by MANIFEST, not by group, because a manifest is a project. A repository that
    ships example applications or is a monorepo has several, and merging them produces
    nonsense: on one public tree the merged list said the project depended on ITSELF three
    times, once per bundled example, presented beside its real dependencies as equals.

    Only ``EXTRACTED`` edges, compared against the enum rather than the string it happens
    to serialise as. That is not fussiness: writing this measurement the first time with a
    lowercase literal made the filter match nothing, so every candidate "passed" and the
    survivor count came out twice its real value while looking entirely plausible.
    """
    by_id = {n.id: n for n in shard.nodes}
    out: dict[str, list[tuple]] = defaultdict(list)
    for e in shard.edges:
        if e.relation != "depends_on" or e.confidence is not Confidence.EXTRACTED:
            continue
        pkg = by_id.get(e.dst)
        if pkg is None:
            continue
        attrs = e.attrs or {}
        out[e.provenance.source_file].append(
            (pkg.name, attrs.get("constraint") or "", attrs.get("group") or "runtime",
             e.provenance.source_line))
    for rows in out.values():
        rows.sort(key=lambda r: (_GROUP_ORDER.get(r[2], 9), r[2], (r[0] or "").lower()))
    return out


def _manifest_order(path: str) -> tuple:
    """Shallowest first, so a repository's own manifest precedes any nested project's."""
    return (path.count("/"), path.lower())


def _adr_section(shard, max_adrs: int) -> list[str]:
    """Numbered entries, one per commitment the repository's OWN manifest records.

    Only the recorded evidence class earns this shape. An ADR states a decision, its context
    and its consequences; the graph can fill the first and none of the rest, so an entry says
    exactly that and leaves the rest visibly absent rather than filled with a generated
    guess. The inferred class -- a constant read in many places -- gets no entry at all,
    because on a measured tree three of seven such candidates are typing constructs, and
    numbering a type variable as a proposed architectural decision is the invention this
    whole page is written to avoid.

    Scoped to the shallowest manifest's runtime and peer groups: a nested project's
    dependencies are that project's decisions, and a dev dependency is a contributor's
    convenience. Everything excluded here is still recorded in full in the tables below.
    """
    manifests = _dependencies(shard)
    if not manifests:
        return []
    root = sorted(manifests, key=_manifest_order)[0]
    rows = [r for r in manifests[root] if r[2] in _ADR_GROUPS]
    if not rows:
        return []
    # By name. Nothing in the graph ranks one dependency above another, and ordering by
    # anything else here would be a claim about importance that no edge supports.
    rows = sorted(rows, key=lambda r: (r[0] or "").lower())
    shown = rows[:max_adrs]

    declared = len(manifests[root])
    lines = [
        "## Proposed decision records",
        "",
        # Both numbers, and what separates them. `rows` is already filtered to runtime and
        # peer, so printing it alone beside a table listing ALL of this manifest's
        # dependencies leaves an unexplained discrepancy on a page whose pitch is that its
        # numbers can be trusted. Same shape as a coverage line that counts what fitted
        # rather than what qualified: the number is not wrong, the sentence around it is.
        f"{len(rows)} of the {declared} dependencies `{root}` declares are required at "
        f"runtime or expected from the host project, and each gets an entry below. The rest "
        f"are development or opt-in: recorded in the tables further down, but not treated as "
        f"commitments. Ordered by name, because nothing in the graph ranks one above another.",
        "",
        "**Every entry is proposed and none was ratified by anybody.** Each states a choice "
        "the repository records and leaves the reasoning visibly absent, because a reason is "
        "not something a manifest contains.",
        "",
        "The numbers are positions in this generated document, not stable identifiers. "
        "Regenerating after a dependency is added renumbers everything below it, so cite an "
        "entry by its heading rather than by its number.",
        "",
    ]
    if len(shown) < len(rows):
        lines += [f"The first {len(shown)} are written out; the remaining "
                  f"{len(rows) - len(shown)} are in the table below.", ""]
    for i, (name, constraint, group, line) in enumerate(shown, start=1):
        at = f"`{root}:{line}`" if line else f"`{root}`"
        pinned = f" at {code(constraint)}" if constraint else ""
        lines += [
            f"### ADR-{i:03d}: Depend on {code(name)}{pinned}",
            "",
            "**Status:** proposed, never ratified.",
            "",
            f"**Decision.** {at} declares {code(name)}"
            + (f" with the constraint {code(constraint)}" if constraint
               else " with no version constraint")
            + f", {_group_title(group).lower()}.",
            "",
            "**Context.** *Nobody wrote this down. The repository records the choice and not "
            "the reason, so what was weighed against it is not recoverable from the code.*",
            "",
        ]
    return lines


def _dependency_section(shard, max_per_manifest: int) -> list[str]:
    manifests = _dependencies(shard)
    total = sum(len(v) for v in manifests.values())
    lines = ["## Recorded choices: dependencies", ""]
    if not total:
        # Stated as an observation with its two causes, because an empty list here reads as
        # "this project depends on nothing" when the far likelier truth is that its
        # dependencies live somewhere this does not read yet. A public application once
        # reported zero for exactly that reason, and nothing on the page would have said so.
        lines += ["No dependency is recorded in this repository's manifests. Either it "
                  "declares none, or it declares them somewhere not yet read: the manifests "
                  "understood are `pyproject.toml` (including PEP 735 `[dependency-groups]`), "
                  "`package.json`, `*.csproj` and `pom.xml`. A lock file, a requirements "
                  "file, or a Makefile is not one of them.", ""]
        return lines
    pinned = sum(1 for rows in manifests.values() for r in rows if r[1])
    paths = sorted(manifests, key=_manifest_order)
    lines += [
        f"{total} recorded across {len(paths)} manifest(s); {pinned} carry a version "
        f"constraint. Each row is a line somebody wrote on purpose, so this is the one part "
        f"of the page that is a record rather than an inference. What none of it says is "
        f"WHY: whatever alternatives were weighed left no trace in the repository.",
        "",
    ]
    if len(paths) > 1:
        # Said explicitly, because the first table is the repository's own dependencies and
        # the ones under it may belong to bundled examples or sub-projects that depend on
        # this very repository. Merged, that list claims the project depends on itself.
        lines += [f"One table per manifest: `{paths[0]}` first, then any nested project, "
                  f"since each manifest is a separate project with its own dependencies.",
                  ""]
    for path in paths:
        rows = manifests[path]
        shown = rows[:max_per_manifest]
        lines += [f"### `{path}`", ""]
        lines += table(
            ["Package", "Constraint", "Group", "Line"],
            [(code(name), code(constraint) if constraint else "*unpinned*",
              _group_title(group), str(line) if line else "")
             for name, constraint, group, line in shown])
        if len(rows) > len(shown):
            lines += ["", f"{len(rows) - len(shown)} more declared in `{path}` and not "
                          f"listed here. That file is the complete list."]
        lines.append("")
    return lines


def _use_sites(shard) -> dict[str, list]:
    """`{constant id: [edge, ...]}` for `uses` edges that are not AMBIGUOUS.

    An AMBIGUOUS edge means the name resolved to several definitions, and the graph
    attributes the SAME site to every one of them. Measured on a real tree: one name defined
    three times carried an identical 41 sites on each node, so summing reports 123 uses of
    41. Dropping those edges is what makes a printed count true rather than plausible.
    """
    out: dict[str, list] = defaultdict(list)
    for e in shard.edges:
        if e.relation == "uses" and e.confidence is not Confidence.AMBIGUOUS:
            out[e.dst].append(e)
    return out


def _values_section(shard, max_values: int) -> list[str]:
    by_use = _use_sites(shard)
    constants = [n for n in shard.nodes if n.kind in _CONSTANT_KINDS]
    candidates = []
    for n in constants:
        sites = by_use.get(n.id, ())
        files = {e.provenance.source_file for e in sites}
        declaration = (n.attrs or {}).get("declaration")
        if len(sites) >= MIN_USE_SITES and len(files) >= MIN_USE_FILES and declaration:
            candidates.append((n, sites, files, declaration))
    candidates.sort(key=lambda c: (-len(c[1]), -len(c[2]), c[0].name or ""))
    shown = candidates[:max_values]

    lines = ["## Load-bearing values", ""]
    if not constants:
        lines += ["This repository has no indexed constant.", ""]
        return lines
    # The coverage line is not optional. The filters above drop candidates SILENTLY, and a
    # short list with no denominator reads as "there is little here" rather than as "most of
    # it did not clear the bar". Both numbers, always, even when they are equal.
    #
    # It counts QUALIFIERS, not rows. Saying "25 of 134 cleared the bar" when 40 cleared it
    # and 25 fit is not an imprecise number, it is a false one, and it is exactly the defect
    # this page exists to avoid: a surface reporting a partial result as a complete one. The
    # cap gets its own sentence below rather than being folded into this count.
    lines += [
        f"{len(candidates)} of {len(constants)} constants carry evidence strong enough to "
        f"print: read in at least {MIN_USE_SITES} places across at least {MIN_USE_FILES} "
        f"files, with a declaration recorded, and with no reading that the graph marked "
        f"ambiguous. The rest are read less, read in one file, or share a name with another "
        f"definition so that no count of them would be true.",
        "",
    ]
    if not candidates:
        lines += ["None cleared it, which is ordinary for a small repository or one whose "
                  "configuration lives outside the code.", ""]
        return lines
    if len(shown) < len(candidates):
        lines += [f"The {len(shown)} read in the most places are listed; "
                  f"{len(candidates) - len(shown)} more qualified and are not shown.", ""]
    lines += [
        "**A count is not a reason.** Each row says a value is read in many places, which is "
        "evidence it is load-bearing and no evidence at all about why it holds the value it "
        "holds. Nothing here was characterised, ranked by importance, or explained.",
        "",
    ]
    lines += table(
        ["Value", "As written", "Read at", "In files"],
        [(code(n.name), code(_shown_declaration(declaration)),
          str(len(sites)), str(len(files)))
         for n, sites, files, declaration in shown])
    lines.append("")
    return lines


def render_design_document(shard, *, repo_id: str,
                           max_deps_per_manifest: int = DEFAULT_MAX_DEPS_PER_MANIFEST,
                           max_values: int = DEFAULT_MAX_VALUES,
                           max_adrs: int = DEFAULT_MAX_ADRS) -> str:
    """The design document as Markdown. Every claim is a measurement or it is absent."""
    lines = [
        f"# {repo_id} design notes",
        "",
        STATUS_MARKER,
        "",
        *stamp("design", repo_id, getattr(shard, "head_commit", None)),
        "**Nobody wrote this page.** It was derived from this repository's own files by "
        "reading its code graph, so everything below is a question to confirm rather than a "
        "decision that was taken and recorded. No entry has been ratified by anybody, and "
        "several of them may turn out to describe an accident rather than a choice.",
        "",
        "Two kinds of evidence appear, and they are not equally strong. A dependency is "
        "**recorded**: somebody wrote it in a manifest on purpose. A load-bearing value is "
        "**inferred** from how often it is read. Neither kind records a reason, because a "
        "reason is not something source code contains.",
        "",
    ]
    lines += _adr_section(shard, max_adrs)
    lines += _dependency_section(shard, max_deps_per_manifest)
    lines += _values_section(shard, max_values)
    return "\n".join(lines).rstrip() + "\n"
