#!/usr/bin/env python3
"""Bare-script launcher for contextlake.

Lets the tool run without installation via ``python3 contextlake.py <command>``.
It puts the ``src/`` layout on ``sys.path`` (ahead of the cwd) so that
``import contextlake`` resolves to the package under ``src/contextlake`` rather
than to this launcher file, then delegates to the package CLI.

For an installed copy, prefer the ``contextlake`` console command or
``python -m contextlake``.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _maybe_reexec_into_venv() -> None:
    """Re-exec under the project ``.venv`` when this interpreter can't run the
    knowledge layer.

    Running ``./contextlake.py`` uses whatever ``python3`` is on the shebang —
    often a bare system Python without the ``[kb]`` extra, so ``index`` / ``graph``
    / ``wiki`` (and the post-sync steps of ``bootstrap``) silently no-op. If the
    current interpreter lacks the extra's marker dependency (``pydantic``) but a
    sibling ``.venv`` Python exists, hand off to it so muscle-memory invocations
    just work. Guarded by an env sentinel so it can never loop, and a no-op when
    the current interpreter is already capable or no venv is present.
    """
    if os.environ.get("CONTEXTLAKE_NO_REEXEC"):
        return
    try:
        import pydantic  # noqa: F401  — marker dep of the [kb] extra
        return
    except ModuleNotFoundError:
        pass
    venv_dir = os.path.join(_HERE, ".venv")
    # Already running from the project venv? Re-exec wouldn't help. (Compare by
    # location, not the binary's realpath — a venv's python is often a symlink to
    # the very system python we're trying to escape, yet uses its own packages.)
    exe = os.path.abspath(sys.executable or "")
    if exe.startswith(os.path.abspath(venv_dir) + os.sep):
        return
    for rel in (("bin", "python"), ("Scripts", "python.exe")):  # POSIX, then Windows
        venv_py = os.path.join(venv_dir, *rel)
        if os.path.exists(venv_py):
            os.environ["CONTEXTLAKE_NO_REEXEC"] = "1"  # backstop against re-exec loops
            os.execv(venv_py, [venv_py, os.path.abspath(__file__), *sys.argv[1:]])


_maybe_reexec_into_venv()

# Put src/ ahead of everything (including this script's own directory) so that
# `import contextlake` resolves to the package, not to this same-named launcher.
# Insert unconditionally: an editable install may already have src/ further down
# sys.path, which must not let the repo-root launcher shadow the package.
_SRC = os.path.join(_HERE, "src")
sys.path.insert(0, _SRC)

from contextlake.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
