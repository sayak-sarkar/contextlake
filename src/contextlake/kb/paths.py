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

from pathlib import Path


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
    except (OSError, ValueError):
        return False
