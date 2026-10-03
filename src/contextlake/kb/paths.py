"""Path containment, in one place.

Two surfaces read a file whose path came from somewhere untrusted: the dashboard resolves a
wiki page from a repo id in a query string, and the documentation generator resolves a call
site from a path recorded in the graph. Both must answer the same question -- is this inside
the directory I meant -- and getting it wrong means serving or quoting a file from elsewhere on
the machine.

It lived in `dashboard/data.py` as a private helper first, and the second implementation
written for the docs generator repeated the check while MISSING the `ValueError` case, which is
how a shared security check earns its own module rather than a copy.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from ..logging_setup import log


def within(base: Path, candidate: Path) -> bool:
    """True if ``candidate`` resolves to a path inside ``base``.

    Resolved on both sides and compared as paths, never as strings: a prefix test accepts
    `/repo-backup` for a base of `/repo`. Resolving also follows symlinks before the
    comparison, so a link pointing out of the tree is caught rather than trusted.

    Fails closed: an unresolvable path is not inside anything. ``ValueError`` is caught
    alongside ``OSError`` because ``resolve()`` raises it -- not ``OSError`` -- for an embedded
    NUL byte, which a query string can carry as ``%00`` and a stored path can carry directly.
    """
    try:
        return candidate.resolve().is_relative_to(base.resolve())
    # RuntimeError too: on Python 3.10, 3.11 and 3.12, ``resolve()`` raises it -- not
    # OSError -- for a symlink LOOP ("Symlink loop from ..."); 3.13 stopped. A clone is
    # untrusted and a loop is one commit away, so this raised out of every caller on the
    # three older versions while passing on the newer ones the code was written on. A
    # loop cannot be resolved, so by this function's own rule it is not inside anything.
    except (OSError, ValueError, RuntimeError):
        return False


def read_repo_file(base: Path, names: Iterable[str]) -> tuple[str, str] | None:
    """``(name, text)`` of the first of ``names`` that is a regular file INSIDE ``base``.

    ``base`` is a mirrored clone, so everything under it is untrusted: anyone who can commit
    to the repository can commit a symlink. The four readers of a clone's own files
    (``get_readme`` over MCP, the wiki generator's README excerpt, the dashboard's README
    panel and the wiki steering file) each did ``f = base / name; if f.is_file():
    f.read_text()`` and nothing else. ``is_file`` and ``read_text`` both FOLLOW symlinks, so
    a ``README.md`` linking to ``/proc/self/environ`` returned the server's environment to
    a key scoped to one repository, and a link to any readable file did the same on the wiki
    and dashboard paths. The wiki path then wrote the target into pages and LLM prompts.

    A candidate whose resolved path is not inside the resolved ``base`` is skipped, not
    raised on: the next name is still tried, so a hostile ``README.md`` does not hide a real
    ``README.rst``. A symlink that stays inside the clone is read normally, and so is a
    symlinked ``base`` (a clone reached through a link), because both sides are resolved.
    ``name`` may carry a sub-path (``.contextlake/wiki.toml``); a symlinked directory that
    escapes fails the same check, because the whole resolved path is compared.

    The RESOLVED path is read, not ``base / name``, so the file checked is the file opened:
    re-following the link at read time would leave a window for it to be swapped after the
    check. Never raises: an unreadable candidate is skipped like a missing one.
    """
    for name in names:
        candidate = base / name
        try:
            resolved = candidate.resolve()
            if not within(base, resolved):
                if candidate.is_symlink() or candidate.exists():
                    log(f"refused {name!r} under {base}: it resolves outside the clone",
                        level=logging.WARNING)
                continue
            if not resolved.is_file():
                continue
            return name, resolved.read_text(encoding="utf-8", errors="replace")
        # RuntimeError for the symlink loop ``resolve()`` raises on 3.10 to 3.12 (see
        # ``within``). Missed at first because it was written and checked on 3.14, where a
        # loop resolves quietly; CI's 3.10 and 3.11 cells failed on the loop test.
        except (OSError, ValueError, RuntimeError):
            continue
    return None
