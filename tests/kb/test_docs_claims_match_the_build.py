"""Every number the docs state about the build, checked against the build.

This is charter gate G4, written as a test rather than a script on purpose: a script gets run
once and then rots, and the whole point is that these claims stay true after 1.0.

**It exists because the claims were measured drifting, twice in one day.** `docs/explained.md`
said "21 tools are registered unconditionally... bring it to 23". Counting from the built server
gave 22 with a tool added that morning, which means 21 had been right only momentarily and 23
dated from an earlier era. Nobody had noticed, because reading a number tells you nothing about
whether it is true.

The rule this enforces: **a number in the docs must have exactly one authority in the code, and
this file names it.** A claim with no authority cannot be checked and should not be a number.

Adding a language, a node kind, an edge relation or an MCP tool will fail a test here. That is
the intent: the failure names the doc line to update, so the docs cannot silently fall behind.

Lives in `tests/kb/` rather than the core tier because every authority it reads -- the grammar
table, the kind registry, the built MCP server, the CLI dispatch table -- is inside
`contextlake.kb`, which is an optional extra. Filed in `tests/` first, and the repo's own
tier guard caught it: the core CI job installs no `[kb]` extra and would have gone red on four
matrix entries while passing locally, where the extra is always present.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _text(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# --- the authorities ------------------------------------------------------------------

def _language_counts() -> tuple[int, int]:
    """`(languages, distinct grammar modules)` from the parser's own table."""
    from contextlake.kb import parse as P
    return len(P._GRAMMARS), len({spec[0] for spec in P._GRAMMARS.values()})


def _node_kind_count() -> int:
    from contextlake.kb.kinds import KIND_REGISTRY
    return len(KIND_REGISTRY)


def _unconditional_tool_names() -> set[str]:
    """Tools a server registers with NO embeddings configured.

    Built rather than counted from source, because the count that matters is what a client
    is actually offered. `semantic_search` and `hybrid_search` register only when an embedder
    and a vector store both exist, so a source-level count would overstate the default.
    """
    from contextlake.kb.server import build_server
    from contextlake.kb.store.sqlite_store import SqliteStore
    with tempfile.TemporaryDirectory() as d:
        store = SqliteStore(Path(d) / "k.sqlite")
        try:
            tools = asyncio.run(build_server(store).list_tools())
            return {t.name for t in tools}
        finally:
            store.close()


def _tool_names_with_embeddings() -> set[str]:
    """Tools a server registers WITH an embedder and a vector store present.

    Stubs rather than a real embedder: registration is gated on both objects merely being
    non-None, so a stub answers the question the count actually asks -- how many tools does
    a client see -- without loading a model into a test run.
    """
    from contextlake.kb.embeddings.store import VectorStore
    from contextlake.kb.server import build_server
    from contextlake.kb.store.sqlite_store import SqliteStore

    class _StubEmbedder:
        name = "stub"

        def embed(self, texts):
            return [[0.0, 0.0] for _ in texts]

    with tempfile.TemporaryDirectory() as d:
        store = SqliteStore(Path(d) / "k.sqlite")
        vs = VectorStore(Path(d) / "v.sqlite")
        try:
            server = build_server(store, embedder=_StubEmbedder(), vector_store=vs)
            return {t.name for t in asyncio.run(server.list_tools())}
        finally:
            vs.close()
            store.close()


# --- the claims -----------------------------------------------------------------------

# EVERY prose file, discovered rather than listed. Each family below used to name the files it
# knew stated its number, which makes the gate exactly as complete as somebody's memory: move a
# sentence into a new page, or write a new page that repeats a count, and the claim is checked
# nowhere while every test stays green. The same shape as the defect this whole gate exists to
# catch, one level up -- a check that reports "all claims verified" when it verified the ones it
# happened to know about.
#
# CHANGELOG.md is excluded on purpose and it is the only exclusion: it is a historical record, so
# "22 tools are registered" under an old version heading is TRUE of that version and must not be
# rewritten when the build moves on.
_HISTORICAL = {"CHANGELOG.md"}


def _doc_files() -> list[str]:
    """Every file that carries prose a reader can see, including the site GENERATOR.

    `site/build_docs.py` holds the published page subtitles, meta descriptions, OpenGraph
    text and JSON-LD as Python string literals -- prose by any reasonable definition, and
    the most widely READ prose the project has, since it is what a search engine and a
    social preview quote. A first version of this glob took `.md` only, and a reviewer
    found "across 14 languages" sitting live on the published page against a build of 27,
    in the one file a markdown glob can never reach. Scanning the generator rather than its
    output keeps a single authority: the HTML and `llms.txt` are built from it.
    """
    root = REPO_ROOT
    patterns = ("docs/**/*.md", "*.md", "site/*.md", "site/*.py")
    found: list[str] = []
    for pat in patterns:
        found += [str(p.relative_to(root)) for p in sorted(root.glob(pat))]
    return [f for f in dict.fromkeys(found) if f not in _HISTORICAL]


def _claims(pattern: str) -> list[tuple[str, int]]:
    """Every `(file, number)` a count-shaped pattern matches, across every prose file."""
    out: list[tuple[str, int]] = []
    for rel in _doc_files():
        for m in re.finditer(pattern, _text(rel)):
            out.append((rel, int(m.group(1))))
    return out


def test_every_stated_language_count_matches_the_grammar_table():
    """`27 languages` in prose vs `len(_GRAMMARS)` in the parser, wherever it is written.

    A language added with two of three pages updated leaves one lying, and nothing else would
    notice. Searched across every page rather than a named few, so a count that moves to a new
    page moves into the gate with it.
    """
    langs, _grammars = _language_counts()
    claims = _claims(r"\*?\*?(\d+)\*?\*? languages")
    assert len(claims) >= 3, (
        f"only {len(claims)} language-count claims found; the prose was reworded and this gate "
        f"has gone blind to it. Found: {claims}")
    wrong = sorted({(rel, n) for rel, n in claims if n != langs})
    assert not wrong, (
        f"stale language counts (file, stated): {wrong}; the parser table has {langs}. "
        f"Update the prose, not this test.")


def test_the_stated_grammar_count_matches_the_distinct_modules():
    """`across 25 grammars` is a SECOND number, and the two move independently.

    TypeScript and TSX share one grammar, so languages exceed grammars. Adding a language that
    reuses an existing grammar moves one number and not the other, and a single check would
    miss it.
    """
    langs, grammars = _language_counts()
    claims = _claims(r"across \*?\*?(\d+)\*?\*? grammars")
    assert claims, "no page states a grammar count any more; the pattern has gone blind"
    wrong = sorted({(rel, n) for rel, n in claims if n != grammars})
    assert not wrong, (
        f"stale grammar counts (file, stated): {wrong}; the table has {grammars} distinct "
        f"modules")
    assert langs > grammars, (
        "languages no longer exceed grammars, so the sentence explaining why they differ "
        "is now wrong as well as the number")


def _embeddable_kind_count() -> int:
    from contextlake.kb.embeddings.index import EMBEDDABLE_KINDS
    return len(EMBEDDABLE_KINDS)


def test_the_stated_embeddable_kind_count_matches_the_set():
    """A THIRD kind-shaped number, independent of the other two.

    Added after `docs/embedding-reference.md` was found claiming 17 against a set of 19:
    `schema_element` and `schema_type` joined the set and the sentence naming the total was
    never touched. Neither the node-kind gate nor the language gate could see it, because 19
    is not 52 and not 27 -- a number can be stale in its own dimension while every other
    dimension checks out.
    """
    embeddable = _embeddable_kind_count()
    claims = _claims(r"\*?\*?(\d+) embeddable kinds")
    assert claims, "no page states an embeddable-kind count any more; the pattern has gone blind"
    assert embeddable < _node_kind_count(), (
        "embeddable kinds should be a strict subset of all node kinds; the source moved")
    wrong = sorted({(rel, n) for rel, n in claims if n != embeddable})
    assert not wrong, (
        f"stale embeddable-kind counts (file, stated): {wrong}; EMBEDDABLE_KINDS holds "
        f"{embeddable}")


def test_the_stated_node_kind_count_matches_the_registry():
    kinds = _node_kind_count()
    claims = _claims(r"(\d+) node kinds")
    assert claims, "no page states a node-kind count any more; the pattern has gone blind"
    wrong = sorted({(rel, n) for rel, n in claims if n != kinds})
    assert not wrong, (
        f"stale node-kind counts (file, stated): {wrong}; KIND_REGISTRY has {kinds}")


# Every file that states a tool count, in ANY wording. The first version of this gate read one
# phrase in one file, and two further statements -- `docs/explained.md`'s "(21, or 23 once
# embeddings exist)" and `docs/benchmarks.md`'s "21 of them on a graph-only store (20 graph tools
# plus the `ask` router)" -- stayed wrong while the gate was green. A sibling reviewer then SKIPPED
# numeric claims because it trusted this gate, so one blind spot became two.
#
# So the shape of the check changed: find every count-shaped claim anywhere in the docs, and
# require the SET of numbers to be exactly the ones the build supports. A new phrasing that this
# pattern does not recognise is a hole, which is why the test also asserts it found several.

# Each pattern declares WHAT ITS NUMBER SHOULD BE, rather than every pattern sharing one set of
# acceptable values. A shared set was the first attempt and it left a hole: allowing
# `unconditional - 1` anywhere (legitimate only in the sentence that separates the `ask` router
# from the graph tools) made a stale "21 tools are registered" read as correct. Break-tested: with
# the shared set, corrupting that sentence PASSED.
#
# `offset` is added to the unconditional count to get the expected value.
_TOOL_COUNT_PATTERNS = [
    (r"\*?\*?(\d+)\*?\*? tools are registered", 0),
    # benchmarks.md was reworded for plain language on 2026-08-25 and these two patterns went
    # silent, which is the `silent` assertion below doing its job. Updated in the same commit as
    # the rewording, per the rule in TIER1-RESULTS.md.
    (r"\*\*(\d+) on a graph-only store\*\*", 0),
    (r"which is (\d+) graph tools plus", -1),
    (r"bring it to\s*\n?\*?\*?(\d+)", +2),
    (r"serves the other \*?\*?(\d+)", 0),
    (r"tool schemas \((\d+), or \d+ once", 0),
    (r"tool schemas \(\d+, or (\d+) once", +2),

    # The only place a count legitimately excludes the router, because the sentence adds it back.

    (r"\*\*(\d+) once embeddings exist\*\*", +2),
    # The roadmap counts the tools BESIDE the router, so both of its numbers sit one below the
    # unconditional total, which includes `ask`. Added after widening the file set found a stale
    # language count on that page and the tool sentence beside it turned out to be stale too --
    # in a wording no pattern here recognised. The file set and the pattern set are two separate
    # blind spots and closing one does not close the other.
    (r"router plus (\d+) underlying tools", +1),
    (r"(\d+) of them always present", -1),
    # docs/cli-reference.md's `kb keys` section, which reproduces a measured
    # tools/list answer proving a scope-flagged key is not restricted. The count
    # is the unconditional total, so offset 0. Added when the guard caught the
    # claim shipping with no pattern beside it, which is the guard working.
    (r"answered with all (\d+) registered tools", 0),
]


def test_every_tool_count_anywhere_in_the_docs_matches_the_build():
    """Every count-shaped claim in every doc file, each against its OWN expected value.

    The first version read one phrase in one file, and two further statements stayed wrong while
    it was green -- and a sibling reviewer then skipped numeric claims because it trusted this
    gate, so one blind spot became two.
    """
    unconditional = len(_unconditional_tool_names())
    found: list[tuple[str, str, int, int]] = []
    silent: list[str] = []
    for pat, offset in _TOOL_COUNT_PATTERNS:
        hits = _claims(pat)
        if not hits:
            silent.append(pat)
        for rel, n in hits:
            found.append((rel, str(n), unconditional + offset, offset))

    # PER PATTERN, not a total. A floor of "at least five matched" was the first version and
    # it does not do the job: with ten live claims, rewording the one sentence a pattern
    # covers drops the total to nine, the floor still passes, and that claim is checked
    # nowhere. A pattern that matches nothing is either dead or blind, and both need a human.
    assert not silent, (
        f"{len(silent)} tool-count pattern(s) matched nothing, so whatever they covered is now "
        f"unchecked -- the prose was reworded, or the claim was deleted and the pattern should "
        f"go with it: {silent}")
    wrong = sorted({(rel, got, exp) for rel, got, exp, _off in found if int(got) != exp})
    assert not wrong, (
        f"stale tool counts (file, stated, expected): {wrong}. A server without embeddings offers "
        f"{unconditional}.")


# A number sitting next to the word "tool", in any wording at all. The pattern table above is
# phrase-specific by design -- each entry declares its own expected value -- and that is exactly
# what makes it blind to a sentence nobody has written yet. Break-tested: a new page saying "The
# server exposes 99 tools." PASSED the table, because no pattern claimed it.
#
# The language, grammar and node-kind families do not have this hole: their patterns are already
# wording-agnostic (`(\d+) languages`). Only the tool family trades generality for per-phrase
# expected values, so only the tool family needs the inverse check.
#
# 20 characters of slack absorbs "23 graph tools" and "25 registered tools" without reaching the
# next sentence.
_TOOL_ADJACENT = re.compile(r"\b(\d+)\b(?=[^.\n]{0,20}?\btools?\b)")


def test_no_tool_count_escapes_the_pattern_table():
    """Every number written beside the word "tool" must be claimed by a pattern above.

    Without this, the gate above verifies the claims it happens to know the wording of and
    reports success, which is the defect it exists to catch one level up. A rewrite authors
    hundreds of new sentences; a rule saying "add a pattern when you add a claim" is an
    intention, and an intention is what let `releasing.md` ship a wrong command for a cycle.
    """
    uncovered: list[tuple[str, str]] = []
    for rel in _doc_files():
        text = _text(rel)
        covered = set()
        for pat, _offset in _TOOL_COUNT_PATTERNS:
            for m in re.finditer(pat, text):
                covered.add(m.span(1))
        for m in _TOOL_ADJACENT.finditer(text):
            if m.span(1) in covered:
                continue
            line = text[: m.start()].count("\n") + 1
            uncovered.append((f"{rel}:{line}", text[max(0, m.start() - 40):m.end() + 30]))
    assert not uncovered, (
        f"{len(uncovered)} tool-count claim(s) match no pattern in _TOOL_COUNT_PATTERNS, so "
        f"nothing checks them against the build. Add a pattern beside the claim, in the same "
        f"commit: {uncovered}")


def test_the_embeddings_pair_really_is_the_conditional_two():
    """The `+2` offsets have to be a measured CARDINALITY, not an assumption.

    Asserting only that the two named tools are absent from the unconditional set says
    nothing about how many conditional tools there are. Add a third one tomorrow and every
    `+2` in the pattern table silently starts enforcing a number that is one too low, on a
    gate whose whole job is catching numbers that drifted. So the conditional set is
    measured by difference and its size asserted.
    """
    unconditional = _unconditional_tool_names()
    conditional = _tool_names_with_embeddings() - unconditional
    assert conditional == {"semantic_search", "hybrid_search"}, (
        f"the conditional tools are {sorted(conditional)}; every `+2` offset in "
        f"_TOOL_COUNT_PATTERNS assumes exactly two, so both must be updated together")


def test_every_documented_cli_verb_exists():
    """A verb in the reference table that the dispatcher does not know is a broken promise.

    Cheaper to check than it looks: the reference states verbs as `kb <name>` in a table, and
    the dispatcher is a dict. Writing this forced the table out of a function-local literal
    into `cmds.VERBS`, because while it was inline nothing outside `dispatch` could name the
    verbs and the question was unanswerable except by parsing the file as text.

    Note `VERBS` and not the eager handler dict: `source` is dispatched lazily to keep tomlkit
    off every other command's import path, so the dict alone under-reports by exactly one and
    would look complete.
    """
    from contextlake.kb.cmds import VERBS
    known = set(VERBS)
    documented = set(re.findall(r"^\| `kb ([a-z-]+)`", _text("docs/cli-reference.md"), re.M))
    assert documented, "the CLI reference no longer lists any `kb <verb>` rows"
    missing = sorted(documented - known)
    assert not missing, f"documented but not dispatchable: {missing}"


# --- the compatibility promise -----------------------------------------------------
#
# A promise is a claim like any other, and this one is the kind that rots quietly: nobody
# re-reads a versioning section, and its statements are about behaviour that lives in code
# somewhere else. So the sentences that CAN be checked against the build are checked here,
# not only that the section exists.

_PROMISE_SURFACES = ("CLI verbs and flags", "Store layout", "MCP tool contracts",
                     "Config keys")

def _stated_promise_release(section: str) -> tuple[int, ...] | None:
    """The release the README says the promise binds from, as a version tuple."""
    m = re.search(r"From (\d+)\.(\d+)\.(\d+),", section)
    return tuple(int(g) for g in m.groups()) if m else None


def _current_version() -> tuple[int, ...]:
    from contextlake import __version__
    return tuple(int(part) for part in __version__.split(".")[:3])


def test_the_readme_states_the_compatibility_promise():
    text = _text("README.md")
    assert "## Versioning and compatibility" in text, (
        "the promise has to live where a user reads it, which is the README")
    assert "Semantic Versioning" in text
    for surface in _PROMISE_SURFACES:
        assert surface in text, f"the promise does not say what it means for {surface!r}"


def test_the_promise_is_honest_about_not_being_in_force_yet():
    """The MCP response shape changed in a MINOR release two versions ago.

    A README that read as though the promise already bound would be contradicted by the
    project's own changelog, which is worse than having no section at all.
    """
    text = _text("README.md")
    section = text.split("## Versioning and compatibility", 1)[1].split("\n## ", 1)[0]
    # The prose has to agree with WHERE THE VERSION ACTUALLY IS, which is a comparison
    # rather than a pinned phrase. A literal pin was the first attempt and it broke on the
    # release it was written for: the milestone was called "1.0" while the version sat at
    # 7.x, then became 8.0.0, and a test holding a string cannot notice either move. The
    # question that stays true is simpler -- has the release the promise names arrived?
    stated = _stated_promise_release(section)
    assert stated is not None, "the promise does not name the release it binds from"
    current = _current_version()
    in_force = current >= stated
    if in_force:
        assert "not yet in force" not in section and "Until then" not in section, (
            f"the promise binds from {stated} and this build is {current}, so the section "
            f"must not still describe it as pending")
    else:
        assert "CHANGELOG" in section, (
            "while the promise is pending, a break has to be findable somewhere")
    # And the changelog has to actually carry one. Asserting only that the README POINTS at
    # it would pass on a build whose changelog documented no break at all, which is the
    # promise pointing at an empty room.
    changelog = _text("CHANGELOG.md")
    assert "### Changed" in changelog, "no release records a behaviour change to point at"
    assert "needs `.nodes`" in changelog or "bare list" in changelog, (
        "the most recent pre-1.0 break -- two MCP tools moving off a bare list -- is not "
        "documented, so the README's pointer leads nowhere")


def test_the_parser_version_carve_out_matches_what_doctor_actually_does():
    """The README says a stale parser is an advisory rather than a fault. That is a claim
    about code in another file, and the two drifting apart is exactly how a promise starts
    lying. `doctor` passes the tri-state `None` (⚠) for this check; `False` would be a ✗ and
    would fail the verdict, which is the behaviour the README explicitly rules out."""
    import inspect

    from contextlake.kb.cmds import doctor

    src = inspect.getsource(doctor.cmd_doctor)
    stale = [line for line in src.splitlines()
             if "shards up to date with the current parser" in line]
    assert stale, "the stale-shard check has been renamed; the README claim needs re-reading"
    # The line that reports a STALE shard, not the one that reports a fresh one. Both carry
    # the label, so `any(... "None" ...)` passed with the branches swapped -- a guard
    # satisfied by the wrong half of the pair it was written to distinguish.
    reported_stale = [line for line in stale if "detail" in line]
    assert reported_stale, "the stale branch no longer passes a detail; re-read this check"
    assert all("None" in line for line in reported_stale), (
        f"the README says a parser bump is an advisory, but doctor reports it as a fault: "
        f"{reported_stale}")
    section = _text("README.md").split("## Versioning and compatibility", 1)[1]
    assert "advisory" in section, "the README no longer states the carve-out this checks"


# --- the demo fleet -----------------------------------------------------------------
#
# A repo id in a doc command is a claim like any count: "type this and it works". Five
# `kb graph --repo acme/app` commands and one `--name process_reading` shipped against a
# bundled fleet that holds neither, so every one of them failed for a reader who typed it.
# The CLI epilog had already moved to `demo/app`; the page had not, so the docs contradicted
# the CLI.
#
# The authority is the SHIPPED FIXTURE, read through the same `_fixture_path()` the dashboard
# uses. Reading it any other way means a fixture that moves blinds this gate instead of
# failing it.


def _fleet():
    """`(repo ids, symbol names, (repos, nodes, edges))` from the bundled demo fixture."""
    import json

    from contextlake.kb.dashboard.site import _fixture_path

    raw = json.loads(_fixture_path().read_text(encoding="utf-8"))
    shards = raw["shards"] if isinstance(raw, dict) and "shards" in raw else [raw]
    repos = {s["repo"] for s in shards}
    names = {n["name"] for s in shards for n in s["nodes"]}
    triple = (len(shards), sum(len(s["nodes"]) for s in shards),
              sum(len(s["edges"]) for s in shards))
    return repos, names, triple


# Fences that render a picture rather than something a reader runs.
_DIAGRAM_FENCES = {"mermaid"}


def _unwrap(snippet: str) -> str:
    """One command on one logical line: shell continuations joined, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"\\\s*\n", " ", snippet)).strip()


def _commands(text: str) -> list[tuple[str, int]]:
    """Every runnable-looking line in a fenced block or inline-code span, with its offset.

    Unwrapping is what makes this readable at all. A markdown command wraps mid-flag:
    `--repo` ends a line and its value starts the next, inside ONE inline-code span. A
    line-anchored search over `visualizing-the-graph.md` found `acme/app` on three of the
    five lines carrying it and found no `process_reading` at all. Both instances that hid
    from two earlier sweeps were wrapped ones. So fences and code spans are extracted and
    unwrapped first, rather than the file being read a line at a time.
    """
    out: list[tuple[str, int]] = []
    prose: list[tuple[str, int]] = []
    pos = 0
    for m in re.finditer(r"```([^\n]*)\n(.*?)```", text, re.S):
        prose.append((text[pos:m.start()], pos))
        pos = m.end()
        # A ```mermaid fence is a DIAGRAM. Its node labels quote flag names
        # (`--node / --name / --search`) beside the words `contextlake kb graph`, which
        # reads as a command with `/` for a value. Skipping the fence keeps the value
        # check strict instead of loosening it to tolerate a non-command.
        if m.group(1).strip().lower() in _DIAGRAM_FENCES:
            continue
        body = re.sub(r"\\\s*\n", " ", m.group(2))
        # Per LINE, not per block: a block holding both `kb index --repo team/widgets`
        # (an id being assigned) and a `kb graph` line would otherwise read as one command
        # and hand the assigned id to the reading-verb check.
        off = m.start(2)
        for line in body.split("\n"):
            out.append((_unwrap(line), off))
            off += len(line) + 1
    prose.append((text[pos:], pos))
    for chunk, base in prose:
        for m in re.finditer(r"`([^`]+)`", chunk):
            out.append((_unwrap(m.group(1)), base + m.start(1)))
    return [(c, o) for c, o in out if c]


# Only the verbs whose `--repo` is a FILTER over ids already in the store. `kb index
# --source ./widgets --repo team/widgets` ASSIGNS an id (the repo does not exist yet, and
# must not), and `gh attestation verify --repo sayak-sarkar/contextlake` is not contextlake
# at all. A guard that could not tell those apart would demand the fleet contain them.
_READING_VERB = re.compile(r"contextlake kb (?:graph|query|ask)\b")
# `--repo REPO`, `--repo R`, `--repo <repo>`: a placeholder standing for a value the reader
# supplies, not an id being promised.
_PLACEHOLDER = re.compile(r"^([A-Z_]+|.*<.*)$")


def _seeds(pattern: str) -> list[tuple[str, str, int]]:
    """`(file, value, line)` for a flag on every reading command, across every doc file."""
    found: list[tuple[str, str, int]] = []
    for rel in _doc_files():
        text = _text(rel)
        for cmd, off in _commands(text):
            if not _READING_VERB.search(cmd):
                continue
            for m in re.finditer(pattern, cmd):
                value = m.group(1)
                if _PLACEHOLDER.match(value):
                    continue
                found.append((rel, value, text[:off].count("\n") + 1))
    return found


def test_every_repo_a_docs_command_filters_on_is_in_the_demo_fleet():
    """`kb graph --repo <id>` names an id the reader must already have.

    Break-tested in both directions, and on a WRAPPED instance specifically: restoring
    `--repo\\n  acme/app` (the value on the following line) has to go red, or the gate has
    the same blind spot as the sweeps that let this ship.
    """
    repos, _names, _triple = _fleet()
    seeds = _seeds(r"--repo[= ]([^\s]+)")
    assert seeds, "no doc command filters by repo any more; this gate has gone blind"
    wrong = sorted({(rel, value, line) for rel, value, line in seeds if value not in repos})
    assert not wrong, (
        f"doc commands name repos the bundled fleet does not hold (file, id, line): {wrong}. "
        f"The fixture ships {sorted(repos)}. Every one of these fails for a reader who types "
        f"it. Update the docs, not this test.")


def test_every_symbol_a_docs_command_seeds_from_is_in_the_demo_fleet():
    """`kb graph --name <symbol>` is the same promise one level down.

    Its own test rather than a branch of the one above: `--name process_reading` was wrong
    while every `--repo` on the page was right, so a single combined check would have been
    satisfied by fixing only the repos.
    """
    _repos, names, _triple = _fleet()
    seeds = _seeds(r"--name[= ]([^\s]+)")
    assert seeds, "no doc command seeds by name any more; this gate has gone blind"
    wrong = sorted({(rel, value, line) for rel, value, line in seeds if value not in names})
    assert not wrong, (
        f"doc commands seed from symbols the bundled fleet does not hold (file, name, line): "
        f"{wrong}. `kb graph --name` on any of these exits with 'Nothing named ... is in the "
        f"graph'.")


# --- the fleet triple ---------------------------------------------------------------
#
# `4 repos, 29 nodes, 28 edges` sat in two style guides as a WRITING PATTERN, teaching the
# old fleet's numbers to whoever writes the next page, and the built site and its search
# index carried them. A word-shaped sweep cannot see a count, which is why two rounds of
# name sweeps left them standing.

_TRIPLE = re.compile(r"(\d+) repos?, (\d+) nodes?, (\d+) edges?")
# A `kb index --workspace` transcript counts REAL SOURCE TREES, not the fixture, so its
# triple has a different authority and gets its own test below. Excluding it here rather
# than listing the file keeps the split by authority instead of by filename.
_WORKSPACE_RUN = re.compile(r"```.*?\n(.*?kb index --workspace.*?)```", re.S)


def test_every_stated_fleet_triple_matches_the_bundled_fixture():
    fleet_repos, _names, triple = _fleet()
    assert len(fleet_repos) == triple[0]
    found: list[tuple[str, tuple[int, int, int]]] = []
    for rel in _doc_files():
        text = _text(rel)
        spans = [m.span(1) for m in _WORKSPACE_RUN.finditer(text)]
        for m in _TRIPLE.finditer(text):
            if any(lo <= m.start() < hi for lo, hi in spans):
                continue
            found.append((f"{rel}:{text[:m.start()].count(chr(10)) + 1}",
                          tuple(int(g) for g in m.groups())))
    assert found, "no page states the fleet triple any more; this gate has gone blind"
    wrong = sorted({(where, got) for where, got in found if got != triple})
    assert not wrong, (
        f"stale fleet triples (where, stated): {wrong}; the bundled fixture holds {triple}")


def test_a_workspace_index_transcript_sums_its_own_per_repo_lines():
    """`Workspace indexed: N repos, X nodes, Y edges` is arithmetic, so it is checkable.

    `index.py` builds `ws_nodes`/`ws_edges` by summing `store.repo_counts(repo_id)` over the
    repos the run discovered, and prints those same per-repo counts on the lines above. So a
    transcript whose summary does not equal its own lines is a transcript no run could have
    produced. One shipped saying 66 over lines adding to 76.

    This gate is why fixing that number is derivation rather than invention: the fixture
    cannot supply it (these are real source trees), but the code's own arithmetic can.
    """
    per_repo = re.compile(r"^\s*\S*\s*[\w./-]+: (\d+) nodes?, (\d+) edges?\s*$", re.M)
    summary = re.compile(r"Workspace indexed: (\d+) repos?, (\d+) nodes?, (\d+) edges?")
    checked = 0
    wrong: list[str] = []
    for rel in _doc_files():
        text = _text(rel)
        for block in _WORKSPACE_RUN.finditer(text):
            body = block.group(1)
            s = summary.search(body)
            if not s:
                continue
            lines = per_repo.findall(body)
            if not lines:
                continue
            checked += 1
            repos, nodes, edges = (int(g) for g in s.groups())
            want = (len(lines), sum(int(n) for n, _ in lines), sum(int(e) for _, e in lines))
            if (repos, nodes, edges) != want:
                wrong.append(f"{rel}: summary says {(repos, nodes, edges)}, its own "
                             f"{len(lines)} per-repo lines add to {want}")
    assert checked, (
        "no `kb index --workspace` transcript is checkable any more; the console block was "
        "reworded and this gate has gone blind")
    assert not wrong, (
        "a workspace-index transcript contradicts itself, so no real run produced it: "
        + "; ".join(wrong))


# --- the worked eval example -------------------------------------------------------
#
# `searching-semantically.md` shows a golden-query file a reader copies. It shipped with a
# second query expecting a node the shipped fixture does not hold, so the example scored
# P@k=0.50 against the very fixture it is written for. Measured: 0.50 before, 1.00 after.
#
# The other gates here read a number out of prose and compare it. This one RUNS the example,
# because the claim it makes is not a number at all -- it is "copy this and it works", and
# only running it can check that.


def test_the_worked_golden_query_example_scores_on_the_shipped_fixture():
    """Every query in the documented golden set must find what it says it will.

    A worked example that half fails teaches the reader that a red row is normal, which is
    the opposite of what `kb eval` is for. Break-tested by restoring the shipped pair
    (`{"query": "ingest", "expected": ["ingest"], "match": "name", "kind": "function"}`):
    it goes red on P@k, not on a parse error.

    Runs against `examples/fixtures/sample-graph.json` through the real FTS retriever, so it
    cannot pass by agreeing with a number written next to it.
    """
    import json
    import tempfile

    from contextlake.kb.eval import evaluate, load_golden, make_fts_retriever
    from contextlake.kb.model import Repo
    from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
    from contextlake.kb.store.sqlite_store import SqliteStore

    page = _text("docs/searching-semantically.md")
    blocks = [b for b in re.findall(r"```json\n(.*?)```", page, re.S) if '"queries"' in b]
    assert len(blocks) == 1, (
        f"expected one golden-query example on the page, found {len(blocks)}; the section "
        f"was restructured and this gate no longer knows which block to run")
    golden_doc = json.loads(blocks[0])
    assert golden_doc["queries"], "the documented golden set is empty"

    fixture = json.loads(
        (REPO_ROOT / "examples" / "fixtures" / "sample-graph.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "golden.json").write_text(blocks[0], encoding="utf-8")
        store = SqliteStore(tmp / "index.sqlite")
        try:
            shard = GraphShard.model_validate(fixture)
            store.upsert_repo(Repo(id=shard.repo, path=str(tmp), head_commit=shard.head_commit))
            write_shard(tmp, shard)
            reindex_shard(store, tmp, shard.repo)
            report = evaluate(store, load_golden(tmp / "golden.json"), k=10,
                              retriever=make_fts_retriever(store))
        finally:
            store.close()

    missed = [row["query"] for row in report["per_query"] if row["precision@k"] == 0.0]
    assert not missed, (
        f"the documented golden-query example does not score against the fixture it is "
        f"written for: {missed} find nothing, so the page's P@k reads "
        f"{report['precision@k']:.2f}. A reader who copies it sees a failing row and "
        f"learns that a red row is normal.")
    assert report["precision@k"] == 1.0, (
        f"the example scores P@k={report['precision@k']:.2f}; every documented query "
        f"should find what it claims")


# A repo id does not always arrive on a flag. `kb docs <id>` and `kb wiki <id>` take it
# POSITIONALLY, and the flag-shaped check above cannot see that form at all -- neither
# could the grep that found the five `--repo acme/app` lines. `generating-documentation.md`
# was showing `kb docs team/api team/worker`, which answers "No indexed repo matches
# team/api, team/worker" against the bundled fleet.
_POSITIONAL_IDS = re.compile(
    r"contextlake kb (?:docs|wiki)((?:\s+[a-z0-9_.-]+/[a-z0-9_./-]+)+)")


def test_every_repo_a_docs_command_names_positionally_is_in_the_demo_fleet():
    """The same promise as the `--repo` gate, in the shape that has no flag to match on.

    Its own test rather than a branch of the flag one, for the reason the `--name` split
    exists: a combined check is satisfied by fixing whichever half happens to be wrong.

    `--namespace acme/stations` is not caught here and should not be: the scan stops at the
    first flag, and a namespace is a repo-id PREFIX over the reader's own store rather than
    an id that has to resolve.
    """
    repos, _names, _triple = _fleet()
    found: list[tuple[str, str, int]] = []
    for rel in _doc_files():
        text = _text(rel)
        for cmd, off in _commands(text):
            for m in _POSITIONAL_IDS.finditer(cmd):
                line = text[:off].count("\n") + 1
                found += [(rel, v, line) for v in m.group(1).split()]
    assert found, "no doc command names a repo positionally any more; this gate has gone blind"
    wrong = sorted({(rel, value, line) for rel, value, line in found if value not in repos})
    assert not wrong, (
        f"doc commands name repos positionally that the bundled fleet does not hold "
        f"(file, id, line): {wrong}. `kb docs` on any of these answers 'No indexed repo "
        f"matches ...'. The fixture ships {sorted(repos)}.")
