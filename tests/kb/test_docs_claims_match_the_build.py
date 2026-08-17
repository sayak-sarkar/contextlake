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


def test_the_stated_tool_counts_match_a_server_built_without_embeddings():
    """Both halves of "N registered unconditionally, M with embeddings".

    The pair is the trap: the first number is what a default install offers and the second is
    the first plus exactly two. Stating them independently let one drift while the other
    looked plausible, which is what happened.
    """
    unconditional = _unconditional_tool_names()
    text = _text("docs/explained.md")
    m = re.search(r"(\d+) tools are registered unconditionally", text)
    assert m, "docs/explained.md no longer states the unconditional tool count"
    assert int(m.group(1)) == len(unconditional), (
        f"docs claim {m.group(1)} unconditional tools; a server built without embeddings "
        f"offers {len(unconditional)}: {sorted(unconditional)}")

    with_embeddings = re.search(r"bring it to\s*\n?(\d+)", text)
    assert with_embeddings, "docs no longer state the with-embeddings total"
    assert int(with_embeddings.group(1)) == len(unconditional) + 2, (
        f"docs claim {with_embeddings.group(1)} with embeddings; it should be "
        f"{len(unconditional)} + 2 (semantic_search, hybrid_search)")
    # And those two really are the conditional pair, not something else that happens to fit.
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
