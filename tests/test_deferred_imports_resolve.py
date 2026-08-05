"""Guard: every function-local relative import actually resolves.

`contextlake kb graph --serve` crashed on every invocation, in the released
5.1.1 wheel, with ``ImportError: cannot import name 'style' from
'contextlake.kb'``. Two sites in ``kb/visualize/serve.py`` wrote ``from ..
import style`` where ``style`` lives one level higher, so the correct form was
``from ... import style``.

Nothing caught it because the import sits *inside* a function. This module
imports cleanly, the package imports cleanly, and every test that never calls
that particular function passes. The defect only surfaces when a user runs the
command, which is the worst possible place to find it.

Deferred imports are used deliberately here (heavy or optional dependencies are
kept off the startup path), so the answer is not to hoist them. The answer is to
check that each one names a module that exists, which is a static property and
does not require calling anything.

This walks the AST rather than importing, so it costs nothing and cannot be
fooled by a module that happens to be imported already.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "contextlake"


def _module_name(path: Path) -> str:
    """Dotted name for a file inside the package, e.g. kb/visualize/serve.py."""
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _relative_imports(tree: ast.AST, module: str, is_package: bool):
    """Yield (lineno, target, name) for each relative `from . import x`.

    Only relative imports are checked: an absolute import of a third-party
    package may legitimately be absent, since that is exactly what the optional
    extras are, whereas a relative one names something this repository ships.

    The dot arithmetic differs for a package and a plain module, which is the
    detail that makes this class of bug easy to write. Inside ``a/b/__init__.py``
    the module *is* the package ``a.b``, so one dot means ``a.b``. Inside
    ``a/b/c.py`` one dot means the containing package ``a.b``, not ``a.b.c``.
    """
    parts = module.split(".")
    anchor = parts if is_package else parts[:-1]
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        up = node.level - 1
        base = anchor[: len(anchor) - up] if up else anchor
        if not base:
            continue  # climbed past the top-level package; not something to assert on
        target = ".".join(base + ([node.module] if node.module else []))
        for alias in node.names:
            yield node.lineno, target, alias.name


def _module_file(dotted: str) -> Path | None:
    """Resolve a dotted first-party name to the file on disk, or None.

    Deliberately filesystem-only. An earlier version used ``importlib.find_spec``,
    which *imports parent packages* as a side effect: on the `core` CI job, where
    the optional knowledge-layer extra is absent, importing a parent raised and
    the guard reported "no module" for modules that plainly exist. That is a false
    positive, and a guard that cries wolf about a missing extra gets muted.

    A wrong relative import is a static property of the source tree, so the check
    is static too. It also means this test costs nothing and cannot be perturbed
    by whatever happens to be installed.
    """
    parts = dotted.split(".")
    if not parts or parts[0] != SRC.name:
        return None
    rest = parts[1:]
    base = SRC.joinpath(*rest)
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _defines(path: Path, name: str) -> bool:
    """Does this module bind ``name`` at module level, determined statically?

    Collects every binding rather than special-casing statement types. An earlier
    version enumerated def / class / import / assignment and still missed
    ``TEXT, JSON = "text", "json"``, because the assignment target there is a
    Tuple of Names rather than a Name. Walking for Name nodes in a Store context
    catches tuple and list unpacking, ``for`` targets, ``with ... as`` and the
    walrus operator without needing to predict which of them the source uses.

    Read from the AST, never by importing: the `core` CI job installs without the
    knowledge-layer extra, so importing those modules raises there and would turn
    this guard into a false alarm about a missing optional dependency.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name.split(".")[0]) == name for a in node.names):
                return True
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id == name:
                return True
    return False


PY_FILES = sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: str(p.relative_to(SRC)))
def test_relative_imports_name_something_that_exists(path):
    module = _module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = []
    for lineno, target, name in _relative_imports(tree, module, path.name == "__init__.py"):
        # Either `target` is a module and `name` is one of its attributes, or
        # `target.name` is itself a submodule. Both are legal; neither existing
        # is the bug.
        target_file = _module_file(target)
        if target_file is None:
            bad.append(f"{path.name}:{lineno}: no module {target!r}")
            continue
        if _module_file(f"{target}.{name}") is not None:
            continue  # `name` is itself a submodule
        if not _defines(target_file, name):
            bad.append(f"{path.name}:{lineno}: {target!r} has no {name!r}")
    assert not bad, "unresolvable relative import(s):\n" + "\n".join(bad)
