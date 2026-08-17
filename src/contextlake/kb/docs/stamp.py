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


def _safe(value: str | None) -> str:
    return _UNSAFE.sub("_", (value or "").strip()) or UNKNOWN


def stamp(kind: str, repo_id: str, head_commit: str | None) -> list[str]:
    """The marker and its human sentence, as Markdown lines.

    ``head_commit`` of ``None`` becomes ``unknown`` rather than being omitted. An absent
    field reads as "nothing to report" and a present ``unknown`` reads as "this was checked
    and could not be determined", which is the difference between a consumer defaulting to
    fresh and defaulting to stale.
    """
    repo, commit = _safe(repo_id), _safe(head_commit)
    marker = f"<!-- contextlake:generated kind={_safe(kind)} repo={repo} commit={commit} -->"
    if commit == UNKNOWN:
        sentence = (f"Generated from `{repo}`, at an **unknown commit** -- the store did not "
                    f"record one, so there is no way to tell whether this still describes "
                    f"the code. Re-index and regenerate to get a page that can say.")
    else:
        sentence = f"Generated from `{repo}` at commit `{commit}`."
    return [marker, "", sentence, ""]


def read_stamp(text: str) -> tuple[str, str, str] | None:
    """`(kind, repo, commit)` from a document's marker, or `None` if it carries none.

    `None` means the page predates stamping or was not written by this code, which a caller
    must treat as unknown-and-therefore-stale rather than as current.
    """
    m = _MARKER.search(text or "")
    if m is None:
        return None
    return m.group("kind"), m.group("repo"), m.group("commit")
