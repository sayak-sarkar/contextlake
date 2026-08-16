"""Imports that only exist on newer interpreters must carry their backport fallback.

`tomllib` entered the standard library in 3.11, and this project supports 3.10, where the
`tomli` backport is a declared dependency. A bare `import tomllib` therefore works on every
interpreter a developer is likely to be running and fails on exactly one CI leg.

That is what happened: a new test imported it bare, passed locally on 3.14, passed three of
the four knowledge-layer jobs, and failed py3.10 alone with `ModuleNotFoundError`. Every
other module in the repo already used the two-line guarded form, so the house pattern was
right there and the check that would have caught the exception was not.

This scans source rather than importing anything, so it gives the same answer on every
interpreter, including the ones where the bare import happens to work.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# module name -> the version that made it stdlib, and the backport to fall back to.
# Add a row when the project starts importing another such module; the point is that the
# rule is a table, not one special case about toml.
NEEDS_FALLBACK = {
    "tomllib": "tomli",
}


def _python_files() -> list[Path]:
    """Every Python file the project OWNS, from git rather than from a glob.

    A glob over `benchmarks/` walks into `benchmarks/head-to-head/work/`, where the
    harness clones third-party repositories. Those are gitignored, are not ours, and one
    of them ships a vendored `docopt.py` that would be reported here as a violation of a
    rule this project cannot apply to it.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "*.py"],
        capture_output=True, text=True, check=True).stdout.split()
    paths = [REPO / rel for rel in out]
    assert paths, "git listed no Python files; this scan would pass vacuously"
    return paths


def _guarded_imports(tree: ast.AST) -> set[str]:
    """Modules imported inside a `try:` whose handler catches ImportError.

    `ModuleNotFoundError` is a subclass, so catching either one is a real guard. A bare
    `except:` counts too: ugly, but it does keep the interpreter running.
    """
    guarded: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        catches_import = any(
            h.type is None
            or (isinstance(h.type, ast.Name)
                and h.type.id in ("ImportError", "ModuleNotFoundError"))
            for h in node.handlers
        )
        if not catches_import:
            continue
        for stmt in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(stmt, ast.Import):
                guarded |= {a.name.split(".")[0] for a in stmt.names}
            elif isinstance(stmt, ast.ImportFrom) and stmt.module:
                guarded.add(stmt.module.split(".")[0])
    return guarded


def _all_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("module,backport", sorted(NEEDS_FALLBACK.items()))
def test_a_newer_stdlib_module_is_never_imported_bare(module, backport):
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if module in _all_imports(tree) and module not in _guarded_imports(tree):
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        f"`import {module}` with no fallback in: {offenders}. It is stdlib only on newer "
        f"interpreters, so this passes everywhere except the oldest supported version. "
        f"Use the form the rest of the repo uses:\n"
        f"    try:\n        import {module}\n"
        f"    except ModuleNotFoundError:\n        import {backport} as {module}")


def test_the_scan_finds_the_imports_it_is_looking_for():
    """Proves the scan is not vacuous.

    Without this, a rename of `NEEDS_FALLBACK`'s key, a broken glob, or an AST walk that
    misses `import x` inside a function body would leave the test above passing against
    an empty offender list on a repo full of violations.
    """
    seen = set()
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        seen |= _all_imports(tree) & set(NEEDS_FALLBACK)
    assert seen == set(NEEDS_FALLBACK), (
        f"the scan found {sorted(seen)} but the table lists {sorted(NEEDS_FALLBACK)}; "
        "either the module is no longer imported anywhere (drop the row) or the scan "
        "stopped seeing it (fix the scan, do not drop the row)")


def test_a_bare_import_is_actually_detected(tmp_path):
    """Break-test in permanent form: the detector must report a file it should reject.

    Written because the guarded and unguarded forms differ only by indentation and a
    surrounding `try`, and an AST walk that descends into handlers as well as bodies would
    call every bare import guarded and never fail on anything.
    """
    bare = ast.parse("import tomllib\n")
    assert "tomllib" in _all_imports(bare)
    assert "tomllib" not in _guarded_imports(bare)

    guarded = ast.parse(
        "try:\n    import tomllib\nexcept ModuleNotFoundError:\n"
        "    import tomli as tomllib\n")
    assert "tomllib" in _guarded_imports(guarded)

    # A `try` that catches something unrelated is NOT a guard, and must not read as one.
    wrong_handler = ast.parse(
        "try:\n    import tomllib\nexcept ValueError:\n    pass\n")
    assert "tomllib" not in _guarded_imports(wrong_handler)
