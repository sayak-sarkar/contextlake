"""The structural wiki page: six sections, no model in the loop.

Every line here is read off the graph, the manifests and the checkout. Nothing is
inferred, nothing is phrased, and there is no API key involved, so this page exists for
every repository the moment it is indexed. That is the point rather than a fallback: a
generated page needs an LLM configured, and until now a user without one got nothing at
all from `kb wiki`.

It is also what the generated page is written FROM. The council rejected an earlier
generated wiki for thin grounding, and the cause was that the model saw a ~15-symbol
sample of a repository and wrote confidently about the whole of it. Handing it this
document instead means stage 2 is prose over stated facts rather than inference over a
sample.

**An empty section is omitted and named.** A small library has no entry points and no
decision records, and six headings two-thirds of which say "none found" is a worse page.
But an omission that says nothing is worse still: absent then reads identically whether
the repository has no entry points or the extractor missed them, and telling those apart
is most of what this project is for. So the omitted ones are listed once, at the end.
"""

from __future__ import annotations

# Section titles, in the order they appear. Kept as a table because two things read it:
# the renderer, and the closing line that names whatever was omitted, which must use the
# same words the headings would have used.
SECTION_TITLES = {
    "entry_points": "Entry points and how to run it",
    "architecture": "Architecture",
    "ownership": "Ownership and activity",
    "surface": "The public surface",
    "install": "Installation and usage",
    "contents": "What this repository contains",
}


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    """A Markdown table, or nothing at all when there are no rows.

    Returning nothing rather than a header with no body is what makes the empty-section
    rule work: a section is empty when its parts render to nothing, and a lone header row
    would make every section non-empty forever.
    """
    if not rows:
        return []
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    out += ["| " + " | ".join(c or "" for c in r) + " |" for r in rows]
    return out


def _md_cell(text: str | None) -> str:
    """One table cell, with the two characters that would break the table neutralised.

    A pipe splits a row and a newline ends it, and both arrive here from source: a C++
    signature carries `|` for bitwise-or, and a doc comment carries newlines. Escaping is
    not cosmetic, it is what keeps a symbol's own text from silently rewriting the table
    around it.
    """
    return " ".join(str(text or "").split()).replace("|", "\\|")


def _symbol_rows(rows: list[dict], *, limit: int) -> list[list[str]]:
    out = []
    for r in rows[:limit]:
        where = _md_cell(r.get("file"))
        out.append([f"`{_md_cell(r.get('name'))}`", _md_cell(r.get("kind")),
                    f"`{where}`" if where else "", _md_cell(r.get("signature"))])
    return out


# The kinds section 1 owns. Named once so section 4 can exclude them rather than repeating
# the list, which is how the two sections would drift into listing the same symbol twice.
_ENTRY_KINDS = ("entry_point", "endpoint", "route", "make_target", "dockerfile_stage")


def _entry_points(brief: dict) -> list[str]:
    """Section 1. How the program is started, and every other named way in.

    Entry points, service surfaces and build targets are one question asked of different
    kinds of project: a command, an HTTP route and a `make` target are all "the thing you
    invoke". They are listed together, with the kind saying which is which.
    """
    rows = [r for r in brief.get("top_symbols", []) if r.get("kind") in _ENTRY_KINDS]
    return _table(["Name", "Kind", "File", "Signature"], _symbol_rows(rows, limit=40))


def _architecture(brief: dict, modules: list[dict] | None) -> list[str]:
    """Section 2. The shape of the repository: its modules and their sizes."""
    rows = [[f"`{_md_cell(m.get('prefix'))}`", str(m.get("nodes") or "")]
            for m in (modules or [])[:30]]
    return _table(["Module", "Symbols"], rows)


def _ownership(owners: list[dict] | None) -> list[str]:
    """Section 3. Who knows this area, from the commit history.

    Deliberately NOT a productivity scoreboard: the share is what makes it useful for
    "who should I ask", and the raw commit counts beside it are what would turn it into
    one, so only the share and the last-active date are shown.
    """
    rows = [[_md_cell(o.get("name")), f"{round(100 * float(o.get('share') or 0))}%",
             _md_cell(o.get("last_active"))] for o in (owners or [])[:10]]
    return _table(["Contributor", "Share of recent work", "Last active"], rows)


def _surface(brief: dict) -> list[str]:
    """Section 4. The named surface, most-called first, with counts where there are any.

    Hubs come first because "what does everything here depend on" is the question a reader
    arriving at unfamiliar code asks, and the count is computed over the whole shard rather
    than a sample. But the section is NOT hubs-only, and that was the first draft's real
    defect: a class nobody calls yet has no caller count, so on a small repository the
    public surface listed almost nothing while the code plainly had a public surface. A
    symbol with no callers is a fact about the graph, not a reason to omit the symbol.

    So the ranked hubs are followed by the rest of the brief's top symbols, once each. A
    blank Callers cell means the graph records none, which is different from zero being
    unknown and is why the cell is blank rather than "0".
    """
    rows, seen = [], set()
    for r in brief.get("hubs", [])[:25]:
        name = _md_cell(r.get("name"))
        seen.add(name)
        where = _md_cell(r.get("file"))
        rows.append([f"`{name}`", _md_cell(r.get("kind")),
                     f"`{where}`" if where else "", str(r.get("count") or "")])
    for r in brief.get("top_symbols", []):
        name = _md_cell(r.get("name"))
        if name in seen or r.get("kind") in _ENTRY_KINDS:
            continue      # entry points have their own section; do not list them twice
        seen.add(name)
        where = _md_cell(r.get("file"))
        rows.append([f"`{name}`", _md_cell(r.get("kind")),
                     f"`{where}`" if where else "", ""])
        if len(rows) >= 40:
            break
    return _table(["Symbol", "Kind", "File", "Callers"], rows)


def _install(brief: dict) -> list[str]:
    """Section 5. How to install and run it, from the files that say so."""
    signals, counts, extras = _setup_parts(brief)
    out: list[str] = []
    if signals:
        out.append("Build and packaging files found: "
                   + ", ".join(f"`{_md_cell(s)}`" for s in signals[:12]) + ".")
    if extras:
        out.append("")
        out.append("Also present: " + ", ".join(f"`{_md_cell(e)}`" for e in extras[:12])
                   + ".")
    if counts:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
        out.append("")
        out += _table(["Tooling", "Files"],
                      [[f"`{_md_cell(k)}`", str(v)] for k, v in top])
    return out


def _setup_parts(brief: dict) -> tuple[list, dict, list]:
    """`setup_signals` unpacked defensively.

    It is a 3-tuple today and is built in two different places (shard-only and
    live-checkout). Unpacking it positionally here would make this renderer break on a
    shape change somewhere else entirely, which is the coupling that makes a derived
    surface fragile.
    """
    raw = brief.get("setup_signals") or brief.get("setup_from_shard") or ()
    parts = list(raw) + [None, None, None]
    signals = parts[0] if isinstance(parts[0], list) else []
    counts = parts[1] if isinstance(parts[1], dict) else {}
    extras = parts[2] if isinstance(parts[2], list) else []
    return signals, counts, extras


def _contents(brief: dict, deps: dict | None) -> list[str]:
    """Section 6. What is in here, and where it sits in the fleet.

    The dependency half is the part no single-repo tool can produce, which is why it is
    here rather than left to the graph view.
    """
    out: list[str] = []
    langs = brief.get("langs") or {}
    if langs:
        out += _table(["Language", "Symbols"],
                      [[_md_cell(k), str(v)] for k, v in
                       sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))[:12]])
    kinds = brief.get("kinds") or {}
    if kinds:
        out.append("")
        out += _table(["Node kind", "Count"],
                      [[f"`{_md_cell(k)}`", str(v)] for k, v in
                       sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:12]])
    out += _dependency_lines(deps)
    # `any(out)` and not `if out`: the section builds blank-line separators as it goes,
    # so a list of nothing but separators is still an empty section. Filtering them out
    # instead is what the first draft did, and it ran two Markdown tables together with
    # no blank line between them, which merges them into one broken table.
    return out if any(out) else []


def _dependency_lines(deps: dict | None) -> list[str]:
    """The cross-repo half of section 6, both directions.

    Always labelled as describing the WHOLE repository. On a module page every other
    section is scoped to the module, and cross-repo dependency edges cannot be: they come
    from the package two-hop, which is repo-level by construction. Rendering them under a
    neutral heading would let a module page read as though this module depended on those
    repositories.
    """
    if not deps:
        return []
    out: list[str] = []
    for key, title in (("depends_on", "This repository depends on"),
                       ("depended_on_by", "Repositories that depend on this one")):
        names = deps.get(key) or []
        if not names:
            continue
        out.append("")
        out.append(f"**{title}** (whole repository, not scoped to any module): "
                   + ", ".join(f"`{_md_cell(n)}`" for n in names[:20]) + ".")
    return out


# The marker that says which KIND of page a file holds. It has to be machine-readable and
# stable, because the whole "one wiki per repository" rule depends on being able to tell a
# structural page from a generated one on disk: the structural stage runs on every `kb
# wiki`, and without this it would overwrite an accepted prose page with tables every time.
STRUCTURAL_MARKER = "Built from the knowledge graph with no language model."


def is_structural_page(text: str) -> bool:
    """Whether ``text`` is a structural page rather than generated prose.

    Read from the page itself rather than tracked beside it. A sidecar would be a second
    thing to keep in step with the file, and the case that matters is a file somebody
    copied, restored from a backup, or wrote by hand.
    """
    return STRUCTURAL_MARKER in (text or "")


def render_structural_page(
    brief: dict, *, repo_id: str, path_prefix: str | None = None,
    modules: list[dict] | None = None, owners: list[dict] | None = None,
    dependencies: dict | None = None,
) -> str:
    """The whole page as Markdown, sections with nothing in them omitted and named.

    ``owners`` is passed in rather than computed: it needs a live checkout and, under
    `[kb] anonymize`, pseudonyms. Both are the caller's business, and taking it as an
    argument keeps this function a pure rendering of facts it was handed, which is what
    makes it testable without a git repository.
    """
    body: dict[str, list[str]] = {
        "entry_points": _entry_points(brief),
        "architecture": _architecture(brief, modules),
        "ownership": _ownership(owners),
        "surface": _surface(brief),
        "install": _install(brief),
        "contents": _contents(brief, dependencies),
    }

    if path_prefix:
        head = [f"# {repo_id} — {path_prefix}", "",
                f"*This page covers only the `{path_prefix}` module of `{repo_id}`, "
                f"not the repository as a whole.*"]
    else:
        head = [f"# {repo_id}"]

    lines = [*head, ""]
    for key, section in body.items():
        if not section:
            continue
        lines += [f"## {SECTION_TITLES[key]}", "", *section, ""]

    omitted = [SECTION_TITLES[k] for k, v in body.items() if not v]
    if omitted:
        lines += [
            "## Sections omitted", "",
            "Nothing was found for: " + ", ".join(omitted.copy()) + ". "
            "These are omitted rather than shown empty, and named rather than omitted "
            "silently, so an absence here means the graph holds nothing of that kind "
            "for this scope and not that a section was forgotten.", "",
        ]
    head_commit = brief.get("head")
    lines += [
        "---", "",
        f"*{STRUCTURAL_MARKER} Every line above is read from the graph, the manifests "
        f"and the checkout"
        + (f", at commit `{head_commit}`" if head_commit else "")
        + (f" (parser {brief['parser_version']})" if brief.get("parser_version") else "")
        + ". Nothing here was written by a model, so nothing here needs reviewing for "
        "accuracy against something else.*",
    ]
    return "\n".join(lines).rstrip() + "\n"


# Providers that answer from this machine. Everything else sends the prompt over a network
# to somebody else's computer, which is the distinction that matters for identities.
# `cli` is here because it spawns a local agent binary; whether THAT binary then calls out
# is its own configuration and not something this module can see or should claim to.
LOCAL_PROVIDERS = frozenset({"ollama", "builtin", "cli"})


def owners_leave_this_machine(provider: str | None, anonymize: str) -> bool:
    """Whether a wiki run would send real contributor names to somebody else's computer.

    The structural page carries the ownership section and is now the generation prompt, so
    a remote provider receives whatever that section holds. Under `anonymize = "always"` it
    holds pseudonyms and nothing identifying travels; under the default it holds real names.

    Reported by the caller rather than prevented here: the setting is the operator's answer
    and this function does not overrule it. But a NEW data flow that the default turns on
    must not be silent, so the run says it once. Silent by construction for local
    providers, which is most runs, so it never becomes boilerplate.
    """
    name = (provider or "").strip().lower()
    if not name or name == "auto":
        # Nothing configured, or not yet resolved. Claiming a data flow here would be a
        # false alarm on the commonest case of all: no LLM set up, so nothing is sent.
        return False
    return name not in LOCAL_PROVIDERS and anonymize != "always"


def repo_dependencies(store, repo_id: str) -> dict:
    """Both directions of ``repo_id``'s cross-repo dependency edges, by repo id.

    Reads `arch.resolve.repo_dependency_edges`, the package two-hop
    (``publishes ⨝ depends_on``), which is the only cross-repo signal this project
    treats as trustworthy: the raw cross-repo ``imports`` join is dominated by
    fleet-wide `module` nodes and would render hundreds of thousands of phantom edges.

    BOTH directions on purpose. "What depends on me" is the question no single-repo tool
    can answer at all, and returning only the outbound half would quietly drop the half
    that is the differentiator.
    """
    from ..arch.resolve import repo_dependency_edges

    out: dict[str, list[str]] = {"depends_on": [], "depended_on_by": []}
    for e in repo_dependency_edges(store):
        if e.get("src") == repo_id and e.get("dst") != repo_id:
            out["depends_on"].append(e["dst"])
        elif e.get("dst") == repo_id and e.get("src") != repo_id:
            out["depended_on_by"].append(e["src"])
    # Sorted and de-duplicated: the two-hop can yield one pair per shared package, and a
    # page listing `team/core` four times is reporting package count as dependency count.
    return {k: sorted(set(v)) for k, v in out.items()}


def repo_owners(store, repo_id: str, *, path_prefix: str | None = None,
                anonymize: bool = False, limit: int = 10) -> list[dict]:
    """Recency-weighted contributors for a repo or one of its sub-paths, or ``[]``.

    Returns ``[]`` rather than raising when the checkout is gone: a repository can be
    indexed and then moved, and a wiki page that fails to generate because somebody
    relocated a directory would be a worse outcome than one whose ownership section is
    honestly absent. The omitted-sections line then names it, so the absence is visible.

    ``anonymize`` uses the SAME pseudonym function the dashboard uses, so one person
    reads as one "Contributor a1b2" across both surfaces.
    """
    from ..ownership import anon_author, compute_owners

    repo = store.get_repo(repo_id)
    path = getattr(repo, "path", None) if repo else None
    if not path:
        return []
    try:
        owners = compute_owners(path, subpath=path_prefix, limit=max(1, min(limit, 50)))
    except Exception:  # noqa: BLE001 - a missing/odd checkout must not fail the page
        return []
    return [{"name": anon_author(o.name, o.email) if anonymize else o.name,
             "share": o.share, "last_active": o.last_active} for o in owners]
