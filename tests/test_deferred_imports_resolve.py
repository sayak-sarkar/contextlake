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
import importlib.util
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


def _spec(dotted: str):
    """``find_spec`` that answers "no" instead of raising.

    Asking for a submodule of a plain module (``contextlake.cli.foo``) raises
    AttributeError rather than returning None, because a module has no
    ``__path__`` to search. For this test's purposes that is simply "not a
    module", so it is folded into the same answer.
    """
    try:
        return importlib.util.find_spec(dotted)
    except (AttributeError, ModuleNotFoundError, ImportError, ValueError):
        return None


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
        if _spec(target) is None:
            bad.append(f"{path.name}:{lineno}: no module {target!r}")
            continue
        if _spec(f"{target}.{name}") is not None:
            continue
        mod = importlib.import_module(target)
        if not hasattr(mod, name):
            bad.append(f"{path.name}:{lineno}: {target!r} has no {name!r}")
    assert not bad, "unresolvable relative import(s):\n" + "\n".join(bad)
