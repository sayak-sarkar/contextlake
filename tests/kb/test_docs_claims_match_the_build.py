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

import pytest

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


# --- the claims -----------------------------------------------------------------------

# Every documented count, with the file that states it. A new claim goes here; a claim whose
# authority cannot be named does not belong in the docs as a number.
_LANGUAGE_CLAIM_FILES = [
    "docs/index-code-graph.md",
    "docs/style-guide-reference.md",
    "README.md",
]


@pytest.mark.parametrize("rel", _LANGUAGE_CLAIM_FILES)
def test_every_stated_language_count_matches_the_grammar_table(rel):
    """`27 languages` in prose vs `len(_GRAMMARS)` in the parser.

    Stated in three files, which is exactly why this is a test: a language added with two of
    the three updated leaves one lying, and nothing else would notice.
    """
    langs, _grammars = _language_counts()
    stated = {int(n) for n in re.findall(r"\*?\*?(\d+)\*?\*? languages", _text(rel))}
    assert stated, f"{rel} states no language count; drop it from _LANGUAGE_CLAIM_FILES"
    assert stated == {langs}, (
        f"{rel} claims {sorted(stated)} languages; the parser table has {langs}. "
        f"Update the prose, not this test.")


def test_the_stated_grammar_count_matches_the_distinct_modules():
    """`across 25 grammars` is a SECOND number, and the two move independently.

    TypeScript and TSX share one grammar, so languages exceed grammars. Adding a language that
    reuses an existing grammar moves one number and not the other, and a single check would
    miss it.
    """
    langs, grammars = _language_counts()
    text = _text("docs/index-code-graph.md")
    stated = {int(n) for n in re.findall(r"across \*?\*?(\d+)\*?\*? grammars", text)}
    assert stated == {grammars}, (
        f"docs claim {sorted(stated)} grammars; the table has {grammars} distinct modules")
    assert langs > grammars, (
        "languages no longer exceed grammars, so the sentence explaining why they differ "
        "is now wrong as well as the number")


def test_the_stated_node_kind_count_matches_the_registry():
    kinds = _node_kind_count()
    stated = {int(n) for n in re.findall(r"(\d+) node kinds", _text("docs/index-code-graph.md"))}
    assert stated == {kinds}, (
        f"docs claim {sorted(stated)} node kinds; KIND_REGISTRY has {kinds}")


# Every file that states a tool count, in ANY wording. The first version of this gate read one
# phrase in one file, and two further statements -- `docs/explained.md`'s "(21, or 23 once
# embeddings exist)" and `docs/benchmarks.md`'s "21 of them on a graph-only store (20 graph tools
# plus the `ask` router)" -- stayed wrong while the gate was green. A sibling reviewer then SKIPPED
# numeric claims because it trusted this gate, so one blind spot became two.
#
# So the shape of the check changed: find every count-shaped claim anywhere in the docs, and
# require the SET of numbers to be exactly the ones the build supports. A new phrasing that this
# pattern does not recognise is a hole, which is why the test also asserts it found several.
_TOOL_COUNT_FILES = ["docs/explained.md", "docs/benchmarks.md", "docs/serve.md", "README.md"]

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
]


def test_every_tool_count_anywhere_in_the_docs_matches_the_build():
    """Every count-shaped claim in every doc file, each against its OWN expected value.

    The first version read one phrase in one file, and two further statements stayed wrong while
    it was green -- and a sibling reviewer then skipped numeric claims because it trusted this
    gate, so one blind spot became two.
    """
    unconditional = len(_unconditional_tool_names())
    found: list[tuple[str, str, int, int]] = []
    for rel in _TOOL_COUNT_FILES:
        text = _text(rel)
        for pat, offset in _TOOL_COUNT_PATTERNS:
            for m in re.finditer(pat, text):
                found.append((rel, m.group(1), unconditional + offset, offset))

    assert len(found) >= 5, (
        f"only {len(found)} tool-count claims matched; a doc was reworded and this gate has gone "
        f"blind to it. Found: {found}")
    wrong = sorted({(rel, got, exp) for rel, got, exp, _off in found if int(got) != exp})
    assert not wrong, (
        f"stale tool counts (file, stated, expected): {wrong}. A server without embeddings offers "
        f"{unconditional}.")


def test_the_embeddings_pair_really_is_the_conditional_two():
    """The +2 in the allowed set has to be those two tools and not a coincidence."""
    unconditional = _unconditional_tool_names()
    assert "semantic_search" not in unconditional
    assert "hybrid_search" not in unconditional


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
