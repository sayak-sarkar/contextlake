"""The API reference: a repository's public surface, with its real call sites.

What makes this worth generating rather than writing is the last part. Every entry carries
the file and line of each place the symbol is actually called, read from the graph's
per-occurrence `calls` edges. A hand-written reference says what a function is for; this one
says where the codebase itself uses it, and a reader can open any of those lines.

Two numbers accompany each symbol and they are NOT the same, which is why both appear:

- **call sites** -- how many places call it. One row per occurrence in source.
- **callers** -- how many distinct definitions those sites belong to.

`model.PER_SITE_RELATIONS` states the rule this follows: a row count answers "how many call
sites", and rendering it as "callers" is a number that is confidently wrong rather than
merely missing. A function called fifty times from one loop has fifty sites and one caller,
and a reader deciding whether it is safe to change needs to know which.

The caller count is narrower than "distinct edge sources", and measurably so: on one public
Python tree 26 of the `calls` edge sources are `file` nodes rather than definitions. Counting
those as callers is the same confident-wrong number in a second disguise, so they are counted
and labelled separately -- see `_split_sites`.
"""

from __future__ import annotations

from collections import defaultdict

from ..kinds import KIND_REGISTRY
from ..mdwrite import code, table

# A call's source counts as a caller when it is a definition rather than a container. Read off
# the registry's own `group` rather than listed here, because the question "is this a thing
# that can call something" is already answered there, and a second list would drift from it.
#
# Measured on two public trees, this distinction is not academic: using the DOCUMENTED kinds
# for it instead discarded 270 test-function callers on one C++ tree, reporting a symbol with
# 12 real call sites as having 0 callers.
CONTAINER_KINDS = frozenset(
    k for k, spec in KIND_REGISTRY.items() if spec.group == "Containers"
)

# The kinds that make up an interface. A file, a module or a package is not a symbol
# somebody calls, and an entry point is how the program STARTS rather than something its
# own code depends on -- the structural wiki gives entry points their own section for the
# same reason.
API_KINDS = frozenset({
    "class", "function", "method", "interface", "struct", "enum", "typedef", "macro",
    "procedure",
})


def _call_sites(shard) -> dict:
    """``node id -> [(file, line, caller id), ...]`` for every recorded call, in file order.

    Read off the shard rather than queried per symbol. A store round-trip per symbol is how
    a documentation pass over a large repository becomes too slow to run, and the shard is
    already parsed by the time anything here is called.
    """
    out = defaultdict(list)
    for e in shard.edges:
        if e.relation != "calls":
            continue
        prov = e.provenance
        f = getattr(prov, "source_file", None) if prov else None
        line = getattr(prov, "source_line", None) if prov else None
        out[e.dst].append((f or "", line or 0, e.src))
    for sites in out.values():
        sites.sort(key=lambda s: (s[0], s[1]))
    return out


def scoped_name(node) -> str:
    """``node.name`` with its owning scope when the graph recorded one.

    Necessary rather than decorative. On one public C++ tree the reference held 64 pairs of
    IDENTICAL headings inside a single file section -- six `Get()`, six `flush()` -- because a
    header library overloads a short name across many types. `qualified_name` separates them
    (`detail.glibc_file.flush` against `ostream.flush`) and the bare name does not.

    Two things are deliberate. The `file_scope::` prefix is stripped: every non-C/C++ language
    carries one, and rendering a path inside a symbol heading is noise. And the scope is used
    only when it genuinely ENDS with the node's own name, so a `qualified_name` that disagrees
    with `name` falls back to `name` rather than producing a heading that contradicts the
    entry beneath it.
    """
    tail = (node.qualified_name or "").rsplit("::", 1)[-1].strip()
    if tail and tail != node.name and tail.endswith(node.name):
        return tail
    return node.name


def _is_caller(node) -> bool:
    """Whether a `calls` edge's source is a definition that can be named as the caller."""
    return node is not None and node.kind not in CONTAINER_KINDS


def _split_sites(sites, by_id) -> tuple[int, int]:
    """``(distinct callers, how many of the sites name no caller)``.

    The second number is a SUBSET of the site count, not an addition to it. It exists because
    a `calls` edge's source is not always a definition: it can be the enclosing FILE, which is
    what the store records when a call cannot be attributed to any symbol. Those are real call
    sites and stay listed, but a file is not a caller, so folding them into the caller count
    would overstate how many places would need reading before a change.
    """
    callers, unattributed = set(), 0
    for _f, _l, src in sites:
        if _is_caller(by_id.get(src)):
            callers.add(src)
        else:
            unattributed += 1
    return len(callers), unattributed


def _caller_cell(src, by_id) -> str:
    """How one call site's source reads in the table.

    A container source is labelled as one rather than rendered as a name: a filename in a
    column headed "Caller" is exactly the kind of plausible-looking wrong answer this document
    is meant not to produce.
    """
    node = by_id.get(src)
    if node is None:
        return "*recorded, but the defining node is not in this shard*"
    if not _is_caller(node):
        where = "file" if node.kind == "file" else f"{node.kind} level"
        return f"*{where}-level code, no enclosing definition recorded*"
    # `*(kind)*` rather than a `<br>` and a line break: these pages are read by agents as
    # plain text as well as rendered, and an HTML tag inside a cell reads as noise there.
    return f"{code(scoped_name(node))} *({node.kind})*"


def _by_file(nodes) -> dict:
    grouped = defaultdict(list)
    for n in nodes:
        grouped[n.file].append(n)
    for syms in grouped.values():
        syms.sort(key=lambda n: (n.line_start or 0, n.name or ""))
    return grouped


def render_api_reference(shard, *, repo_id: str, max_symbols: int = 500,
                         max_sites_shown: int = 5, snippets=None) -> str:
    """The reference as Markdown. Always states what it left out.

    Symbols are SELECTED by call-site count, then GROUPED by the file they live in, because a
    reader looking something up thinks in files. Those are two different orders and the
    document says so: an earlier draft claimed the page was "ordered by how many places call
    them" while rendering it by filename, which put the least-used symbols at the top under a
    sentence promising the opposite.

    ``max_symbols`` bounds the document. It is REPORTED whenever it binds, with the true
    total, since a reference that silently stops is worse than a short one: the reader
    cannot tell a symbol that is missing from a symbol that does not exist.

    ``snippets`` is an optional `docs.snippets.SnippetReader`. Given one, each call site also
    carries the line of source at it, which is what turns a pointer into an example. It quotes
    a line only where the file can be proved unchanged since indexing, and where it can quote
    nothing at all the page says why rather than simply having no examples.
    """
    sites = _call_sites(shard)
    by_id = {n.id: n for n in shard.nodes}
    symbols = [n for n in shard.nodes if n.kind in API_KINDS and n.file]
    total = len(symbols)
    symbols.sort(key=lambda n: (-len(sites.get(n.id, ())), n.file or "", n.name or ""))
    shown = symbols[:max_symbols]

    lines = [f"# {repo_id} API reference", ""]
    if not shown:
        # Two conditions reach here -- no documentable symbol at all (a repository of
        # configuration and build files is the ordinary case) and symbols with no file
        # recorded -- so this states the observation and not one presumed cause.
        lines += [f"Nothing to document: this repository has no indexed symbol of a "
                  f"documentable kind ({', '.join(sorted(API_KINDS))}) with a file recorded. "
                  f"For a repository of build or configuration files that is expected. "
                  f"Otherwise re-run `contextlake kb index`.", ""]
        return "\n".join(lines)

    lines += [
        f"{len(shown)} of {total} callable symbols. Selected by how many places call them, "
        f"then grouped by the file they are defined in, so the order below is by file and "
        f"not by call count. Every call site is a line in this repository, read from the "
        f"graph.", "",
    ]
    if total > len(shown):
        cut = len(sites.get(shown[-1].id, ()))
        dropped = symbols[max_symbols:]
        tied = sum(1 for n in dropped if len(sites.get(n.id, ())) == cut)
        note = f"**{len(dropped)} symbols are not listed.** "
        # Three genuinely different situations, and one sentence cannot describe all three
        # honestly. Measured on a public Python tree, only 365 of 1607 symbols had ANY call
        # site, so a 500-entry cap fell entirely inside the tie at zero: "the ones omitted are
        # those with the fewest call sites" implies a ranking that, there, does not exist.
        if tied == len(dropped):
            note += (f"The document is capped at {max_symbols} entries, and every symbol left "
                     f"out has exactly as many recorded call sites ({cut}) as entries that "
                     f"were kept, so which ones were dropped came down to their file and name "
                     f"rather than to any judgement about importance.")
        else:
            note += (f"The document is capped at {max_symbols} entries, and the ones omitted "
                     f"are those with the fewest recorded call sites.")
            if tied:
                note += (f" {tied} of them tie with entries that were kept, at {cut} call "
                         f"site(s) each; that part of the cut is by file and name, which is "
                         f"arbitrary rather than a judgement about importance.")
        note += " They exist in the graph and `contextlake kb query` will find them."
        lines += [note, ""]

    if snippets is not None and snippets.reason:
        # Stated up front, once. A reader who sees no quoted source anywhere would otherwise
        # have to guess whether this repository has no call sites or whether the generator
        # could not read them, and those are different facts.
        lines += [f"Call sites below are not quoted: {snippets.reason}.", ""]

    for path, syms in sorted(_by_file(shown).items(), key=lambda kv: kv[0] or ""):
        lines += [f"## `{path}`", ""]
        for n in syms:
            lines += _symbol_entry(n, sites.get(n.id, []), max_sites_shown, by_id, snippets)
    return "\n".join(lines).rstrip() + "\n"


def _symbol_entry(node, sites, max_sites_shown: int, by_id, snippets=None) -> list[str]:
    """One symbol: what it is, what it looks like, and where it is used."""
    sig = (node.attrs or {}).get("signature") or ""
    shown_name = scoped_name(node)
    heading = f"### `{shown_name}{sig}`" if sig else f"### `{shown_name}`"
    at = f"{node.kind}, defined at line {node.line_start}" if node.line_start else node.kind
    out = [heading, "", f"*{at}.*", ""]

    doc = ((node.attrs or {}).get("doc") or "").strip()
    if doc:
        out += [" ".join(doc.split())[:400], ""]

    if not sites:
        # Stated, not omitted. "Nothing calls this" is a real and useful finding -- it is how
        # dead code and a public API with no internal users look -- and an entry that simply
        # stops reads as though the lookup failed.
        out += ["No call site is recorded in this repository.", ""]
        return out

    callers, unattributed = _split_sites(sites, by_id)
    summary = f"**{len(sites)} call site(s)** across **{callers} caller(s)**"
    if unattributed:
        # "of which", never "and N more": the unattributed sites are part of the site count,
        # and phrasing them as additional made 12 sites with no named caller read as 24.
        summary += (f", {unattributed} of which name no enclosing definition")
    out += [summary + ":", ""]
    header = ["Caller", "File", "Line"]
    rows = [[_caller_cell(src, by_id), code(f), line]
            for f, line, src in sites[:max_sites_shown]]
    if snippets is not None and not snippets.reason:
        # The Source column appears only where a line could actually be quoted for at least
        # one site. A column of blanks would claim the feature ran and found nothing, which is
        # not what an unprovable file means.
        quoted = [snippets.line(f, line) for f, line, _s in sites[:max_sites_shown]]
        if any(quoted):
            header.append("Source")
            # strict: the two lists are built from the same slice of `sites`, so a length
            # mismatch is a bug in this function rather than a case to absorb. Silently
            # truncating would drop a Source cell and leave the row a column short.
            for row, text in zip(rows, quoted, strict=True):
                row.append(code(text) if text else "*changed since indexing*")
    out += table(header, rows)
    if len(sites) > max_sites_shown:
        out.append("")
        out.append(f"...and {len(sites) - max_sites_shown} more. "
                   f"`contextlake kb query \"{node.name}\"` lists every one.")
    out.append("")
    return out
