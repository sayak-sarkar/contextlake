"""Which commit a generated document describes, written so both readers can find it.

A generated page is a claim about source code at a moment. Without the moment, a reader
cannot tell a current page from one describing code that changed months ago, and the page
looks equally authoritative either way. The wiki already carries its commit for exactly this
reason: ``get_wiki`` reports ``stale`` by comparing that stamp against the repo's indexed
head, and its docstring says why -- "so an agent never cites prose that describes code which
has since changed."

The API reference and the design notes carried no stamp at all, which was fine while they
only reached a human opening a file and is not fine now that anything can read them.

Two forms of the same fact, because there are two kinds of reader:

- a **visible sentence**, so somebody opening the file in an editor sees it;
- a **comment marker**, so a program gets it without parsing prose.

The marker is authoritative. The sentence exists to be read, and if the two ever disagree the
marker is what a consumer should trust, because prose is what gets reworded.
"""

from __future__ import annotations

import hashlib
import re

# `commit=` last and unquoted so the pattern stays anchored on a fixed prefix. A repo id can
# contain slashes and dots but not a space or `>`, which is what bounds it here.
_MARKER = re.compile(
    r"<!--\s*contextlake:generated\s+kind=(?P<kind>[a-z-]+)\s+"
    r"repo=(?P<repo>[^\s>]+)\s+commit=(?P<commit>[^\s>]+)\s*-->")

UNKNOWN = "unknown"

# Anything that would break the marker's own grammar. A repo id is used verbatim in an
# attribute-like position, so a space or an angle bracket in one would produce a marker that
# parses as a different repo, or does not parse at all.
_UNSAFE = re.compile(r"[\s<>]+")


#: The repo field a document that describes the whole store carries. A real repo id can
#: never collide with it: `_UNSAFE` would have to leave the parentheses, and no forge
#: permits them in a path. Same convention as the store's own `(shared)` / `(packages)`
#: pseudo-repos, so a reader who has seen one recognises this.
FLEET_REPO = "(fleet)"


def _safe(value: str | None) -> str:
    return _UNSAFE.sub("_", (value or "").strip()) or UNKNOWN


def fingerprint(triples) -> str:
    """Stable short hash of ``(repo_id, head_commit, parser_version)`` triples.

    A document describing MANY repositories has no single commit to stamp, which is why the
    fleet page carried no stamp at all and said so in prose: "this page spans many commits".
    That sentence is honest to a human and useless to a program, which is the exact gap
    :mod:`.stamp` exists to close. One hash over the whole member set gives that document
    the same yes/no freshness answer a single-repo page gets from its commit.

    The parser version is part of the key, not decoration: a generated page can go stale
    without a single commit moving, because the parser changes what it extracts from the
    same code. That happened twice on one day in this project.

    ``usedforsecurity=False`` because this is a cache key and nothing trusts it. Without the
    flag a FIPS-enabled host refuses SHA-1 outright and raises, so generation crashes there
    on a hash whose weakness is irrelevant to what it is used for.
    """
    pairs = sorted((str(r), str(h or UNKNOWN), str(p or UNKNOWN)) for r, h, p in triples)
    return hashlib.sha1(repr(pairs).encode("utf-8"),
                        usedforsecurity=False).hexdigest()[:12]


def stamp(kind: str, repo_id: str, head_commit: str | None, *, noun: str = "commit") -> list[str]:
    """The marker and its human sentence, as Markdown lines.

    ``head_commit`` of ``None`` becomes ``unknown`` rather than being omitted. An absent
    field reads as "nothing to report" and a present ``unknown`` reads as "this was checked
    and could not be determined", which is the difference between a consumer defaulting to
    fresh and defaulting to stale.

    ``noun`` names the value in the HUMAN sentence only; the marker's field stays ``commit=``
    so one parser reads every document. A page spanning many repositories stamps a
    :func:`fingerprint` rather than a commit, and calling that "commit" in prose would be a
    plainly false sentence sitting under a correct marker -- the kind of disagreement this
    module's docstring says to resolve in the marker's favour, which is no reason to write
    the prose wrong in the first place.
    """
    repo, commit = _safe(repo_id), _safe(head_commit)
    marker = f"<!-- contextlake:generated kind={_safe(kind)} repo={repo} commit={commit} -->"
    if commit == UNKNOWN:
        sentence = (f"Generated from `{repo}`, at an **unknown {noun}** -- the store did not "
                    f"record one, so there is no way to tell whether this still describes "
                    f"the code. Re-index and regenerate to get a page that can say.")
    else:
        sentence = f"Generated from `{repo}` at {noun} `{commit}`."
    return [marker, "", sentence, ""]


def strip_marker(text: str) -> str:
    """``text`` with the machine-readable marker removed, for rendering to a human.

    The marker is an HTML comment so a Markdown reader hides it. A renderer that
    escapes HTML rather than passing it through does not: it turns the comment into a
    visible paragraph of ``<!-- contextlake:generated ... -->`` at the top of the page.
    The dashboard's renderer escapes by design, so it strips first and reads the stamp
    from the original.

    Only the marker goes. The human sentence underneath it stays, because that is the
    part written to be read.
    """
    return _MARKER.sub("", text or "", count=1).lstrip("\n")


def read_stamp(text: str) -> tuple[str, str, str] | None:
    """`(kind, repo, commit)` from a document's marker, or `None` if it carries none.

    `None` means the page predates stamping or was not written by this code, which a caller
    must treat as unknown-and-therefore-stale rather than as current.
    """
    m = _MARKER.search(text or "")
    if m is None:
        return None
    return m.group("kind"), m.group("repo"), m.group("commit")
