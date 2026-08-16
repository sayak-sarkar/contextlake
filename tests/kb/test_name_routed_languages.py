"""Files routed to a grammar by NAME rather than by extension.

Every language before this one reached its grammar through `LANG_BY_EXT`, and a build
file has no extension to route on. `Makefile` was not "unsupported" in any visible way:
`_select_file` returned None for it and did not even increment the unsupported-extension
counter, because that counter is guarded by `if ext:`. The file was invisible twice over.

The two routing tables are separate on purpose and the tests below pin both halves of
that separation: an extension never reaches the name table, a name never reaches the
extension table, and the `languages` filter gates both by the same list.
"""

from __future__ import annotations

import pytest

from contextlake.kb import parse
from contextlake.kb.parse import (
    ALL_LANGS,
    LANG_BY_EXT,
    LANG_BY_NAME,
    index_repo_dir,
    is_indexable_name,
    lang_for,
    name_key,
    parse_source,
)

MAKEFILE = b""".PHONY: build test

build test:
\techo building

release: build
\techo releasing
"""


def _write(root, rel: str, body: bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)


# --- the routing key itself ---------------------------------------------------------


@pytest.mark.parametrize("fn,expected", [
    ("Makefile", "makefile"),
    ("makefile", "makefile"),
    ("GNUmakefile", "gnumakefile"),
    # A dotted spelling. `Makefile.am` and `Makefile.in` are automake's real files, and
    # a bare `fn.lower() in LANG_BY_NAME` misses both: the same class of error as an
    # unanchored match, only in the too-strict direction.
    ("Makefile.am", "makefile"),
    ("Makefile.in", "makefile"),
    # The deliberate near-miss: a prefix test would route this to make, and it is not a
    # makefile. This is the assertion that says the match is exact on a derived key.
    ("MyMakefile", "mymakefile"),
    ("Makefile2", "makefile2"),
    # A dotfile has no stem at all, so it can never collide with a table entry.
    (".gitignore", ""),
    # Directories in the path must not leak into the key.
    ("build/Makefile", "makefile"),
])
def test_name_key_is_the_lowercased_stem_before_the_first_dot(fn, expected):
    assert name_key(fn) == expected


@pytest.mark.parametrize("fn,ext,expected", [
    ("Makefile", "", "make"),
    ("GNUmakefile", "", "make"),
    ("makefile", "", "make"),
    ("Makefile.am", ".am", "make"),
    ("MyMakefile", "", None),
    ("LICENSE", "", None),
    ("README", "", None),
])
def test_lang_for_resolves_by_extension_then_by_name(fn, ext, expected):
    assert lang_for(fn, ext) == expected


def test_an_extension_wins_over_a_stem():
    """`Makefile.py` is a Python file whose stem happens to be `makefile`.

    Extension first is not an arbitrary tie-break: an extension is an explicit
    statement about a file's type and a stem is an inference from a convention.
    """
    assert lang_for("Makefile.py", ".py") == "python"


# --- the two tables stay separate and stay in the union -----------------------------


def test_every_name_table_key_is_a_key_name_key_can_actually_produce():
    """A row `name_key` can never return is dead, and dead in a way nothing else shows:
    the language keeps its grammar, its lettermark and its registered kinds, and simply
    never matches a file."""
    for name, _lang in LANG_BY_NAME.items():
        assert "." not in name, (
            f"{name!r} contains a dot, so `name_key` can never produce it and this row "
            f"is dead. Put an extension in LANG_BY_EXT instead.")
        assert name == name.lower(), f"{name!r} must be lowercased to ever match"


def test_all_langs_is_the_union_and_not_either_table_alone():
    """The second assertion is the one that matters, and it caught a real defect.

    `.mk` and `.mak` were in `LANG_BY_EXT` at first, which made `make` reachable BOTH
    ways. `ALL_LANGS` was then numerically identical to `set(LANG_BY_EXT.values())`, so
    every union written into the language count and the two lettermark guards would have
    passed unchanged if reverted to the extension table alone. The guards would have
    looked correct while proving nothing.

    So at least one language must stay reachable by name only. That keeps the union
    load-bearing, which is the whole reason those call sites were changed.
    """
    assert ALL_LANGS == set(LANG_BY_EXT.values()) | set(LANG_BY_NAME.values())
    name_only = set(LANG_BY_NAME.values()) - set(LANG_BY_EXT.values())
    assert name_only, (
        "no language is reachable by name alone, so every union over both routing "
        "tables is currently indistinguishable from reading LANG_BY_EXT by itself, and "
        "reverting one would fail no test")


# --- extraction ---------------------------------------------------------------------


def test_make_targets_become_nodes_one_per_name():
    nodes, _edges, _cites, _imports = parse_source("r", "Makefile", MAKEFILE, "make")
    got = {(n.kind, n.name) for n in nodes if n.kind != "file"}
    assert got == {("make_target", "build"), ("make_target", "test"),
                   ("make_target", "release")}


def test_a_special_target_is_not_a_target():
    """`.PHONY` is make's own directive, not a name anybody invokes.

    Emitting it would put a symbol in the graph that appears in no author's mental model
    of the project, which is the defect Elixir's `use`/`import` directives produced
    before the same kind of guard was added there.
    """
    nodes, _e, _c, _i = parse_source("r", "Makefile", MAKEFILE, "make")
    assert ".PHONY" not in {n.name for n in nodes}


def test_a_prerequisite_is_not_a_definition():
    """`release: build` names `build` twice in the file, once as a target and once as a
    prerequisite. Only the rule defines it, so exactly one node carries that name."""
    nodes, _e, _c, _i = parse_source("r", "Makefile", MAKEFILE, "make")
    assert len([n for n in nodes if n.name == "build"]) == 1


# --- the walker, end to end ---------------------------------------------------------


def test_a_makefile_is_indexed_from_a_real_tree(tmp_path):
    _write(tmp_path, "Makefile", MAKEFILE)
    _write(tmp_path, "app.py", b"def go():\n    return 1\n")
    shard = index_repo_dir(str(tmp_path), "r")

    files = {n.name for n in shard.nodes if n.kind == "file"}
    assert "Makefile" in files, "the build file was walked past without being indexed"
    assert "make_target" in {n.kind for n in shard.nodes}


def test_the_language_filter_gates_names_by_the_same_list_as_extensions(tmp_path):
    """Both directions, because a one-directional test passes when the name table is
    hardwired always-on: filtering to python would still show the Makefile, and only the
    make-side assertion would have caught it."""
    _write(tmp_path, "Makefile", MAKEFILE)
    _write(tmp_path, "app.py", b"def go():\n    return 1\n")

    py_only = index_repo_dir(str(tmp_path), "r", languages=["python"])
    assert "Makefile" not in {n.name for n in py_only.nodes if n.kind == "file"}
    assert "app.py" in {n.name for n in py_only.nodes if n.kind == "file"}

    make_only = index_repo_dir(str(tmp_path), "r", languages=["make"])
    assert "Makefile" in {n.name for n in make_only.nodes if n.kind == "file"}
    assert "app.py" not in {n.name for n in make_only.nodes if n.kind == "file"}


def test_a_name_routed_file_carries_its_language(tmp_path):
    """The lang field is what a repo node's lettermark and every per-language view read.

    This is also the KeyError guard: `SourceFile.lang` used to be `LANG_BY_EXT[ext]`,
    which raises on a file with no extension and on `Dockerfile.prod` alike.
    """
    _write(tmp_path, "Makefile", MAKEFILE)
    shard = index_repo_dir(str(tmp_path), "r")
    mk = [n for n in shard.nodes if n.name == "Makefile"]
    assert mk and mk[0].lang == "make", (
        f"expected lang 'make' on the file node, got {mk[0].lang!r}" if mk
        else "no Makefile node at all")


def test_is_indexable_name_agrees_with_the_walker(tmp_path):
    """The read half of a read/write pair. `is_indexable_name` is a name-only preview of
    the same decision, and it answered "no" for build files while the walker indexed
    them, until it was taught the name table too.
    """
    assert is_indexable_name("Makefile", "Makefile")
    assert is_indexable_name("Makefile.am", "build/Makefile.am")
    assert not is_indexable_name("LICENSE", "LICENSE")
    assert not is_indexable_name("MyMakefile", "MyMakefile")


def test_the_two_sides_of_the_pair_are_derived_from_one_function():
    """Stronger than agreeing on a handful of names: it asserts they cannot disagree.

    `_file_kind` takes `allowed_names` with NO default, so a caller that has not been
    taught about name routing is a TypeError rather than a silent "not indexable".
    """
    import inspect

    sig = inspect.signature(parse._file_kind)
    param = sig.parameters["allowed_names"]
    assert param.default is inspect.Parameter.empty, (
        "`allowed_names` has acquired a default. An unstated answer must be a "
        "construction error: with a default, `is_indexable_name` silently reports build "
        "files as unindexable while the walker indexes them, and no test fails.")
