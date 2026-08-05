"""Tests outside ``tests/kb/`` must run without the ``[kb]`` extra installed.

CI splits the suite in two: a ``core`` job installs only the base package and runs
``pytest --ignore=tests/kb``, and a ``knowledge-layer`` job installs ``[kb]`` and
runs everything. A test that lives outside ``tests/kb/`` but imports
``contextlake.kb`` at module or call level passes locally, where a developer
almost always has the extra installed, and fails on all four core matrix entries
with ``ModuleNotFoundError: No module named 'mcp'``.

That is exactly what happened: a concurrency test was filed next to its sibling
flag tests in ``tests/test_cli_parser.py`` rather than with the server tests, and
took the whole core matrix red. Nothing local caught it, because locally the extra
is there. This check is the local equivalent of the core job.

The permitted escapes are the two the existing code already uses: guard the import
with ``pytest.importorskip`` (the test then skips without the extra), or wrap it in
``try/except ImportError`` (see ``tests/conftest.py``).
"""

from __future__ import annotations

import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent
KB_ROOT = "contextlake.kb"


def _guarded_lines(tree: ast.AST) -> set[int]:
    """Line numbers inside a ``try`` block or an ``importorskip`` call.

    Anything in this set is allowed to touch ``contextlake.kb``: it either skips
    cleanly or is caught, which is what the core job needs.
    """
    safe: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                if hasattr(child, "lineno"):
                    safe.add(child.lineno)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "importorskip":
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        safe.add(child.lineno)
    return safe


def _unguarded_kb_imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    safe = _guarded_lines(tree)
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if node.lineno in safe:
            continue
        for name in names:
            if name == KB_ROOT or name.startswith(KB_ROOT + "."):
                bad.append(f"{path.name}:{node.lineno}: imports {name}")
    return bad


def test_no_core_tier_test_imports_the_knowledge_layer_unguarded():
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("test_*.py")):
        # tests/kb/ is the knowledge-layer job's territory and is free to import it.
        if "kb" in path.relative_to(TESTS).parts[:-1]:
            continue
        offenders.extend(_unguarded_kb_imports(path))

    assert not offenders, (
        "these tests run in CI's core job, which has no [kb] extra, so importing "
        "contextlake.kb there fails with ModuleNotFoundError on every Python "
        "version:\n  " + "\n  ".join(offenders) + "\n"
        "Move the test under tests/kb/, or guard the import with "
        "pytest.importorskip / try-except ImportError."
    )
