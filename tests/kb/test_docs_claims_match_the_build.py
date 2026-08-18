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
    (r"bring it to\s*\n?\*?\*?(\d+)", +2),
    (r"serves the other \*?\*?(\d+)", 0),
    (r"tool schemas \((\d+), or \d+ once", 0),
    (r"tool schemas \(\d+, or (\d+) once", +2),
    (r"\*\*(\d+) of them on a\s*\n?graph-only store\*\*", 0),
    # The only place a count legitimately excludes the router, because the sentence adds it back.
    (r"\((\d+) graph tools plus", -1),
    (r"\*\*(\d+) once embeddings exist\*\*", +2),
    # The roadmap counts the tools BESIDE the router, so both of its numbers sit one below the
    # unconditional total, which includes `ask`. Added after widening the file set found a stale
    # language count on that page and the tool sentence beside it turned out to be stale too --
    # in a wording no pattern here recognised. The file set and the pattern set are two separate
    # blind spots and closing one does not close the other.
    (r"router plus (\d+) underlying tools", +1),
    (r"(\d+) of them always present", -1),
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
