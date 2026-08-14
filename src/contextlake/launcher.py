"""How to spell "run contextlake" inside a file contextlake writes for something else.

Every integration this tool installs -- the MCP server entries, the Claude Code
SessionStart hook, the git ``post-commit`` hook -- has to name a command that some
*other* program will run later, with an environment nobody controls. Getting that name
wrong fails in the worst available way: the host program runs the line, the shell reports
"command not found" into a stream nobody reads, and every status check still says the
integration is installed. Measured before this module existed: a `post-commit` hook ran,
the commit succeeded, the store head never moved, and `kb hook status` reported
``✓ present on 1/6 repo(s)``.

The project already argues this exact point about somebody else's command --
``cmds/doctor_fix.py`` uses ``sys.executable -m pip`` and says "never a bare ``pip`` off
PATH, which can belong to [another interpreter]". This module applies that rule to
contextlake's own name.

**Why there are two answers rather than one.** A bare ``contextlake`` is portable and
wrong when it is absent; an absolute interpreter path is always right on this machine and
useless on anyone else's. Which one is correct depends on who reads the file:

* **Machine-local files** (the git hook, a session hook in one checkout) are never shared,
  so correctness wins outright: always ``sys.executable -m contextlake``.
* **Shared, committed files** (``.mcp.json``, ``.vscode/mcp.json``) are cloned by
  teammates whose home directory is not this one. There a bare ``contextlake`` is
  preferred *when it actually resolves*, and the absolute path is the fallback -- because
  a launcher that works for one person beats a launcher that works for nobody. This is
  the same reasoning ``cmds/steer.py`` already applies to pinning ``--config``: it
  declines to write ``/home/<you>/...`` into a committed file when it can avoid it.
"""

from __future__ import annotations

import shutil
import sys

__all__ = ["launch_argv", "launch_command", "portable_launcher_available"]


def portable_launcher_available() -> bool:
    """Whether a bare ``contextlake`` resolves on this PATH."""
    return shutil.which("contextlake") is not None


def launch_argv(*, portable: bool = False) -> list[str]:
    """The argv prefix that invokes contextlake.

    ``portable=False`` (the default, and the right choice for anything machine-local)
    always returns the running interpreter, which cannot be shadowed or missing.
    ``portable=True`` prefers the bare console script for files other people will clone,
    and silently falls back to the interpreter when that script is not installed --
    a fallback rather than a failure, because the alternative is a file that runs nothing.
    """
    if portable and portable_launcher_available():
        return ["contextlake"]
    return [sys.executable, "-m", "contextlake"]


def launch_command(*, portable: bool = False) -> str:
    """:func:`launch_argv` as a shell-ready string.

    ``sys.executable`` is quoted because a Python installed under a path with a space in
    it is ordinary on macOS and Windows, and an unquoted one produces a hook that reports
    success while running a truncated command.
    """
    from shlex import quote

    return " ".join(quote(p) for p in launch_argv(portable=portable))
