"""No module under ``schedule/`` may import ``contextlake.kb`` at module level.

``schedule/`` is core tier: it runs on an install with no ``[kb]`` extra. ``cmds.py``
reaches the knowledge layer through a lazy ``__import__`` inside a function, which is
the permitted shape. A module-level import would break ``contextlake schedule``
entirely on a core-only install.

``tests/test_core_tier_has_no_kb_imports.py`` sweeps TEST files. Nothing looked at
``src/`` until now, and Plan 2 splits ``cmds.py`` into five modules, which multiplies
the surface where this can go wrong.
"""
from __future__ import annotations

import ast
import pathlib

PKG = (pathlib.Path(__file__).resolve().parents[1]
       / "src" / "contextlake" / "schedule")
KB_ROOT = "contextlake.kb"


def _module_level_kb_imports(path: pathlib.Path) -> list[str]:
    """Only ``tree.body``, deliberately, not ``ast.walk``.

    An import nested inside a function is the permitted escape and the shape
    ``cmds.py`` already uses. Walking the whole tree would flag it and make the
    lazy import impossible, which is the opposite of the rule.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            if name == KB_ROOT or name.startswith(KB_ROOT + "."):
                try:
                    where = path.relative_to(PKG.parent)
                except ValueError:      # a path outside the package, e.g. a tmp file
                    where = path.name
                found.append(f"{where}:{node.lineno}: {name}")
    return found


def test_no_schedule_module_imports_the_knowledge_layer_at_module_level():
    offenders: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        offenders.extend(_module_level_kb_imports(path))

    assert not offenders, (
        "schedule/ is core tier and runs with no [kb] extra installed. Import the "
        "knowledge layer lazily, inside the function that needs it:\n  "
        + "\n  ".join(offenders))


def test_the_check_reads_module_level_only_and_allows_a_lazy_import(tmp_path):
    """Proves the check can FAIL, and that it does not fail on the permitted shape.

    Without this, a check that returned [] unconditionally would pass the test
    above forever and guard nothing.
    """
    offending = tmp_path / "bad.py"
    offending.write_text("import contextlake.kb.cmds.index\n", encoding="utf-8")
    assert _module_level_kb_imports(offending)

    lazy = tmp_path / "good.py"
    lazy.write_text("def f():\n    from contextlake.kb import cmds\n    return cmds\n",
                    encoding="utf-8")
    assert _module_level_kb_imports(lazy) == []
