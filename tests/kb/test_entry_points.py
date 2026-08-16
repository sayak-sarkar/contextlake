"""How a program is STARTED, which a list of functions cannot answer.

The `entry_point` kind has two producers, and they are separate on purpose.

**A definition, re-kinded.** In most languages the entry point is an ordinary function
that something else makes special, so the kind is refined rather than a second node added
beside it, the same call the `test` kind already makes.

**A guard, or a manifest, with no definition to refine.** Python's
`if __name__ == "__main__":` is an `if_statement` and not a definition at all, and
`[project.scripts]` names a command that may point into another file entirely. Neither can
be a re-kind, so each produces its own node.

Every negative below is a language's SECOND condition doing its job. Without them, any
helper called `main` anywhere in a repository is advertised as a way to run the project,
which is a name nobody wrote as an entry point appearing as though somebody had.
"""

from __future__ import annotations

import pytest

from contextlake.kb.manifest import parse_manifest
from contextlake.kb.parse import parse_source


def _kinds(fn: str, src: bytes, lang: str) -> set[tuple[str, str]]:
    nodes, _e, _c, _i = parse_source("r", fn, src, lang)
    return {(n.kind, n.name) for n in nodes if n.kind != "file"}


def _entries(fn: str, src: bytes, lang: str) -> set[str]:
    return {n for k, n in _kinds(fn, src, lang) if k == "entry_point"}


# --- the positive case, one per language --------------------------------------------

POSITIVE = {
    "go": ("a.go", b"package main\n\nfunc main() {}\n", "main"),
    "rust": ("a.rs", b"fn main() {}\n", "main"),
    "c": ("a.c", b"int main(void) { return 0; }\n", "main"),
    "cpp": ("a.cpp", b"int main(int c, char** v) { return 0; }\n", "main"),
    "kotlin": ("a.kt", b"fun main() {}\n", "main"),
    "java": ("A.java", b"class A {\n  public static void main(String[] a) {}\n}\n", "main"),
    # C# capitalises it. The one language here that does, which is exactly the sort of
    # detail that silently halves coverage when it is assumed instead of checked.
    "csharp": ("A.cs", b"class A { static void Main(string[] a) {} }\n", "Main"),
    "python": ("a.py", b'if __name__ == "__main__":\n    pass\n', "__main__"),
}


@pytest.mark.parametrize("lang", sorted(POSITIVE))
def test_each_language_finds_its_entry_point(lang):
    fn, src, expected = POSITIVE[lang]
    assert _entries(fn, src, lang) == {expected}


def test_every_language_with_an_entry_name_is_covered_here():
    """Meta-assertion. A language added to the table in `parse.py` and not to this file
    would be untested, and untested here means untested anywhere: nothing else in the
    suite parses a `main`."""
    from contextlake.kb.parse import _ENTRY_NAMES

    assert set(_ENTRY_NAMES) <= set(POSITIVE), (
        f"languages claiming an entry point with no test: "
        f"{sorted(set(_ENTRY_NAMES) - set(POSITIVE))}")


# --- the negatives, which are the whole point ---------------------------------------


def test_a_go_main_in_another_package_is_not_an_entry_point():
    """The discriminating case, and the one most likely to be got wrong.

    `func main()` in a package that is not `main` is an ordinary function; Go will not
    build it as a command. The function looks IDENTICAL to the real thing, so nothing
    but the package clause tells them apart.
    """
    got = _kinds("util.go", b"package util\n\nfunc main() {}\n", "go")
    assert ("function", "main") in got
    assert not [n for k, n in got if k == "entry_point"]


@pytest.mark.parametrize("lang,fn,src", [
    # An instance method named `main` starts nothing in either language. Both spellings
    # of "not static" are here, and the SECOND is the one that tests the guard.
    #
    # A method with no modifier at all produces no modifier node, so a check as weak as
    # "has a modifier node" already rejects it. Removing the `static` test entirely
    # failed nothing until `public` appeared here: with `public void main`, a modifier
    # node exists and only reading its text tells the two apart. The first draft had the
    # bare form alone, so the guard looked robust while never being reached.
    ("java", "B.java", b"class B {\n  void main(String[] a) {}\n}\n"),
    ("java", "C.java", b"class C {\n  public void main(String[] a) {}\n}\n"),
    ("csharp", "B.cs", b"class B { void Main(string[] a) {} }\n"),
    ("csharp", "C.cs", b"class C { public void Main(string[] a) {} }\n"),
])
def test_an_instance_method_named_main_is_not_an_entry_point(lang, fn, src):
    got = _kinds(fn, src, lang)
    assert ("method", "main") in got or ("method", "Main") in got
    assert not [n for k, n in got if k == "entry_point"]


@pytest.mark.parametrize("lang,fn,src", [
    ("rust", "a.rs", b"fn outer() {\n    fn main() {}\n}\n"),
    ("go", "a.go", b"package main\n\nfunc outer() {\n    main := 1\n    _ = main\n}\n"),
])
def test_a_nested_main_is_not_an_entry_point(lang, fn, src):
    assert not _entries(fn, src, lang)


def test_a_function_merely_named_main_keeps_being_a_function():
    """The re-kind must be a refinement, not a rename applied on sight. A JavaScript
    `function main()` is not an entry point in any sense the runtime recognises, and
    JavaScript claims no entry name at all."""
    got = _kinds("a.js", b"function main() {}\n", "javascript")
    assert ("function", "main") in got


def test_a_lookalike_python_guard_is_not_an_entry_point():
    """`__name__` compared against anything else, or another name compared against
    `"__main__"`, is somebody testing something else."""
    assert not _entries("a.py", b'if __name__ == "__init__":\n    pass\n', "python")
    assert not _entries("a.py", b'if other == "__main__":\n    pass\n', "python")


def test_the_python_guard_is_found_written_the_other_way_round():
    """`if "__main__" == __name__:` is legal Python that no linter rewrites, and a
    one-sided check reads as complete until somebody writes it."""
    assert _entries("a.py", b'if "__main__" == __name__:\n    pass\n', "python") == {
        "__main__"}


# --- the manifest producer ----------------------------------------------------------


def test_pyproject_console_scripts_become_entry_points():
    nodes, _e = parse_manifest("r", "pyproject.toml", b"""[project]
name = "demo"
[project.scripts]
demo = "demo.cli:main"
demo-admin = "demo.admin:main"
[project.gui-scripts]
demo-ui = "demo.ui:main"
""")
    assert {n.name for n in nodes if n.kind == "entry_point"} == {
        "demo", "demo-admin", "demo-ui"}


def test_a_package_json_bin_string_installs_one_command_named_after_the_package():
    """The string spelling, which a dict-only reading drops in silence. It is the more
    common of the two in the wild, since a package that installs exactly one command
    writes it this way."""
    nodes, _e = parse_manifest("r", "package.json", b'{"name": "demo", "bin": "./cli.js"}')
    assert {n.name for n in nodes if n.kind == "entry_point"} == {"demo"}


def test_a_package_json_bin_object_installs_each_key():
    nodes, _e = parse_manifest(
        "r", "package.json", b'{"name": "d", "bin": {"aa": "x.js", "bb": "y.js"}}')
    assert {n.name for n in nodes if n.kind == "entry_point"} == {"aa", "bb"}


def test_npm_run_targets_are_not_entry_points():
    """`scripts` are `npm run` targets, a different fact from a command on your PATH.
    Reading them would bury a project's real entry point under `test` and `lint`."""
    nodes, _e = parse_manifest(
        "r", "package.json",
        b'{"name": "d", "scripts": {"test": "jest", "lint": "eslint"}}')
    assert not [n for n in nodes if n.kind == "entry_point"]


def test_a_manifest_entry_point_is_scoped_to_its_repo():
    """Unlike the `package` nodes beside them, which are fleet-wide on purpose. Two
    repositories that both install a command called `serve` install two different
    programs, and merging them would claim one thing has two definitions."""
    nodes, _e = parse_manifest(
        "r", "pyproject.toml", b'[project]\nname = "demo"\n[project.scripts]\nserve = "a:b"\n')
    entry = next(n for n in nodes if n.kind == "entry_point")
    package = next(n for n in nodes if n.kind == "package")
    assert entry.repo == "r"
    assert package.repo != "r", (
        "the package node stopped being fleet-wide; this test now proves nothing about "
        "the entry point being different")


def test_a_manifest_entry_point_is_reachable_from_its_file():
    """An orphan node answers no question. The audit that produced this project's
    connector work found whole categories of node with zero incident edges."""
    nodes, edges = parse_manifest(
        "r", "pyproject.toml", b'[project]\nname = "demo"\n[project.scripts]\nserve = "a:b"\n')
    entry = next(n for n in nodes if n.kind == "entry_point")
    assert [e for e in edges if e.dst == entry.id and e.relation == "contains"]


# --- the release actually reaching an existing store --------------------------------


def test_an_older_store_re_indexes_and_gains_its_entry_points(tmp_path, monkeypatch):
    """A new kind is worthless if no existing store ever produces it.

    `kb index` skips a repository whose HEAD has not moved, so a parser change reaches
    nobody unless the version stamp forces the re-parse. This drives the shipped command
    against an unchanged commit and asserts the KINDS moved, not merely that the version
    string did. A stale index that reports itself fresh is this project's worst failure
    shape and has happened here before.
    """
    import subprocess
    import types

    from contextlake.kb.cmds.index import cmd_index
    from contextlake.kb.state import mark_repo_indexed
    from contextlake.kb.store.shards import read_shard
    from contextlake.kb.store.sqlite_store import SqliteStore

    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)
    store_dir = tmp_path / "store"
    (home / ".contextlake" / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{store_dir}"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONTEXTLAKE_NO_LOCAL_CONFIG", "1")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "PATH": "/usr/bin:/bin"}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env,
                       capture_output=True)

    # A plain namespace, not a class body: a class body does not see the enclosing
    # function's locals, which is how the first draft of this test raised NameError.
    args = types.SimpleNamespace(source=str(repo), repo="demo", workspace=None,
                                 config=None, force=False, bundle=True, watch=False)

    assert cmd_index(args) == 0
    first = read_shard(store_dir, "demo")
    assert first is not None and "entry_point" in {n.kind for n in first.nodes}, (
        "the first index produced no entry point at all, so the staleness assertions "
        "below would prove nothing about this release reaching an existing store")

    # Now claim the store was built by the PREVIOUS parser, with HEAD unmoved.
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        mark_repo_indexed(store, "demo", head, "0")
    finally:
        store.close()

    assert cmd_index(args) == 0

    from contextlake.kb.parse import PARSER_VERSION
    from contextlake.kb.state import indexed_parser_version

    # The DB STAMP, not the shard file. The first index already wrote that file at the
    # current version, so its `parser_version` reads correct whether the second run
    # re-parsed or skipped entirely, and asserting on it passed against a build with the
    # staleness gate deleted. The stamp is the only value that moves here.
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        stamp = indexed_parser_version(store, store_dir, "demo")
    finally:
        store.close()
    assert stamp == PARSER_VERSION, (
        f"the repository was skipped as unchanged (stamp still {stamp!r}), so an "
        f"existing store would silently keep serving a graph this build does not "
        f"produce")
    after = read_shard(store_dir, "demo")
    assert after is not None and "entry_point" in {n.kind for n in after.nodes}
