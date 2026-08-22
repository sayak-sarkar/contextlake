"""A repository's own say in how its wiki page is written.

`.contextlake/wiki.toml`, authored by the repository and read from its working tree, is the
one place a maintainer can steer generated output. It carries two keys:

* ``notes`` -- free text the page quotes, attributed, near the top.
* ``pages`` -- an explicit list of module prefixes that replaces the automatic
  federation heuristic's choice of which subsystems get their own page.

**Why quoting is safe here and executing would not be.** A file inside a cloned repository is
untrusted input, and `kb/trust.py` refuses settings from such a file when they would cause
contextlake to RUN something. Neither key here runs anything: ``notes`` is prose the page
attributes to the repository rather than asserting in its own voice, and ``pages`` selects
among modules the graph already found -- a name that matches no module is dropped, so the file
cannot invent a page. The precedent is `readme_excerpt`, which has quoted a repository's own
README into the wiki since before this existed, at exactly the same trust level.

**Why `notes` is not fed to the model separately.** The structural page IS the prompt (see
`wiki/validate.py`'s replacement gate), so putting the notes on that page is what puts them in
front of the model. One insertion point, both paths, and the gate keeps working: a name the
notes introduce becomes a name the draft may legitimately cite.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...logging_setup import log

#: Relative to the repository root. A directory rather than a bare dotfile so a repository can
#: grow sibling contextlake config without a second convention.
STEERING_PATH = (".contextlake", "wiki.toml")

#: A note is quoted verbatim into a generated page, so it is bounded. Long enough for a real
#: paragraph of guidance, short enough that a file cannot turn a wiki page into its own
#: document.
MAX_NOTE_CHARS = 2000
MAX_NOTES = 10
MAX_PAGES = 50


def steering_file(repo_path: str | Path) -> Path:
    return Path(repo_path).joinpath(*STEERING_PATH)


def read_wiki_steering(repo_path: str | Path | None) -> dict:
    """``{"notes": [...], "pages": [...]}`` for a repository, both possibly empty.

    Never raises. A malformed or unreadable steering file is reported once and then ignored,
    because a repository that cannot be parsed must still get a wiki page -- the alternative
    is one bad file in one clone silently costing a fleet-wide run its output.
    """
    empty: dict = {"notes": [], "pages": []}
    if not repo_path:
        return empty
    path = steering_file(repo_path)
    try:
        if not path.is_file():
            return empty
        raw = path.read_bytes()
    except OSError:
        return empty
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - 3.10 only
        import tomli as tomllib
    try:
        data = tomllib.loads(raw.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 - a broken file costs itself, not the run
        log(f"wiki: ignoring {path} -- it is not readable TOML ({type(e).__name__}: {e}). "
            f"The page is generated without it.", level=logging.WARNING)
        return empty
    return {"notes": _notes(data), "pages": _pages(data)}


def _notes(data: dict) -> list[str]:
    """``notes`` as a list of non-empty strings, however it was written.

    A single string and a list of strings are both natural things to write, so both are
    accepted rather than one being a silent no-op. Anything else is dropped: a table or a
    number under this key is a mistake, and quoting its repr into a wiki page would put
    obvious nonsense in front of a reader with contextlake's name on it.
    """
    raw = data.get("notes")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_NOTES]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:MAX_NOTE_CHARS])
    return out


def _pages(data: dict) -> list[str]:
    """``pages`` as a list of module prefixes, order preserved, duplicates dropped."""
    raw = data.get("pages")
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw[:MAX_PAGES]:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            seen.add(item.strip())
            out.append(item.strip())
    return out
