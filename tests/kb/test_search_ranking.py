"""`kb query <Symbol>` must rank `<Symbol>`'s own definition first.

Measured on a clean 2,086-node index of a small public Python library, `kb query Context`
returned 20 test-file hits and the real class ranked **32nd of 153**; `Command` ranked
44th of 191. The same ordering reaches an agent through the MCP `search_code` tool, so it
was not only a CLI complaint.

The cause is that FTS5's bare `rank` weights every indexed column equally and the default
tokenizer splits on `_`, so `test_context_meta` in `tests/test_context.py` matches
"context" in `name`, `qualified_name` AND `file` while the real `Context` matches twice.
Longer, noisier rows win.

Every ordering assertion here checks the ORDER, not merely that a hit came back: a test
that asserted the right node appears somewhere in the results would have passed against
the build this fixes, where it appeared 32nd.

Ordering alone is not enough either, which is why the last test in this file is
structural. See its docstring: the bm25 arity trap is invisible to all four ordering
tests, and the symptom recorded in the original finding turned out not to reproduce.
"""

from __future__ import annotations

import pytest

from contextlake.kb.model import Node, Repo
from contextlake.kb.store import sqlite_store
from contextlake.kb.store.sqlite_store import SqliteStore

# The shape that loses under bare `rank`: one real definition, one sibling that shares its
# prefix, and several test rows whose name AND file AND qualified name all repeat the term.
NODES = [
    Node(id="n_ctx", repo="r", kind="class", name="Context",
         qualified_name="pkg.core.Context", file="src/pkg/core.py",
         line_start=10, line_end=40, lang="python"),
    Node(id="n_ctxmeta", repo="r", kind="class", name="ContextMeta",
         qualified_name="pkg.core.ContextMeta", file="src/pkg/core.py",
         line_start=50, line_end=60, lang="python"),
    Node(id="n_t1", repo="r", kind="function", name="test_context_meta",
         qualified_name="tests.test_context.test_context_meta",
         file="tests/test_context.py", line_start=5, line_end=9, lang="python"),
    Node(id="n_t2", repo="r", kind="function", name="test_context_basic",
         qualified_name="tests.test_context.test_context_basic",
         file="tests/test_context.py", line_start=12, line_end=16, lang="python"),
    Node(id="n_t3", repo="r", kind="function", name="test_context_nested",
         qualified_name="tests.test_context.test_context_nested",
         file="tests/test_context.py", line_start=20, line_end=24, lang="python"),
    Node(id="n_helper", repo="r", kind="function", name="context_helper",
         qualified_name="pkg.context_util.context_helper",
         file="src/pkg/context_util.py", line_start=3, line_end=8, lang="python"),
]


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "index.sqlite")
    s.upsert_repo(Repo(id="r", path=str(tmp_path), host=None,
                       default_branch="main", head_commit="abc123"))
    s.upsert_nodes("r", NODES)
    yield s
    s.close()


def test_the_exact_definition_ranks_first(store):
    names = [n.name for n in store.search("Context", limit=10)]
    assert names, "the fixture returned nothing at all; the search never ran"
    assert names[0] == "Context", (
        f"the class the user named did not rank first; order was {names}")


def test_a_prefix_sibling_outranks_the_test_files(store):
    """`ContextMeta` is genuinely related; `test_context_meta` merely mentions the word.

    This is the assertion that separates the shipped fix from the weaker one considered
    first: an exact-name boost alone puts `Context` first but leaves this pair in bm25's
    hands, where the test row wins on the columns it repeats the term in.
    """
    names = [n.name for n in store.search("Context", limit=10)]
    assert names.index("ContextMeta") < names.index("test_context_meta"), (
        f"a test that mentions the term outranked a real sibling symbol; order was {names}")


def test_ranking_survives_a_kind_filter(store):
    """The boosts are appended to the same statement the filters build, so a filtered
    search must not fall back to the old ordering."""
    names = [n.name for n in store.search("Context", kind="class", limit=10)]
    assert names == ["Context", "ContextMeta"], f"filtered order was {names}"


def test_a_lowercase_query_still_finds_the_capitalised_definition(store):
    """Both boost terms are `lower()`ed on each side. Without that the exact-match term
    is dead for every query whose case does not match the symbol's, which is most of the
    way people actually type."""
    names = [n.name for n in store.search("context", limit=10)]
    assert names[0] == "Context", f"lowercase query did not rank the definition first: {names}"


def test_one_bm25_weight_per_fts_column(store):
    """The arity guard, and it exists because BEHAVIOUR does not reliably catch this.

    `bm25()` takes one weight per column including the UNINDEXED one, and too few does
    not raise: SQLite defaults the missing trailing columns to 1.0, shifting every weight
    one column left. Break-tested against the fixture above by dropping the leading 0.0,
    and all four ordering tests still PASSED -- the exact-name and prefix boosts settle
    that order before bm25 is consulted, so the corruption was invisible to them.

    The recorded finding this fix came from said the wrong arity returns "results
    identical to bare rank". Measured here, it does not: the shifted weights produce an
    order that differs from bare `rank` and happens to match the correct one on small
    fixtures. That makes it harder to notice, not easier, which is the argument for
    checking the arity directly instead of hoping a fixture is sensitive to it.
    """
    cols = [r[1] for r in store.conn.execute("PRAGMA table_info(node_fts)")]
    assert cols == list(sqlite_store.FTS_COLUMNS), (
        f"the FTS table's columns changed to {cols} but FTS_COLUMNS still says "
        f"{list(sqlite_store.FTS_COLUMNS)}; the weights below are now aimed at the "
        f"wrong columns")
    assert len(sqlite_store.FTS_WEIGHTS) == len(cols), (
        f"{len(sqlite_store.FTS_WEIGHTS)} bm25 weights for {len(cols)} columns; SQLite "
        f"will not complain, it will silently shift every weight one column left")
    assert sqlite_store.FTS_WEIGHTS[cols.index("node_id")] == 0.0, (
        "node_id is UNINDEXED and must carry weight 0.0")
    assert (sqlite_store.FTS_WEIGHTS[cols.index("name")]
            > sqlite_store.FTS_WEIGHTS[cols.index("file")]), (
        "name must outweigh file, or a path that repeats the term beats the symbol")
