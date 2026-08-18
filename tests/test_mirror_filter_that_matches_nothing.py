"""A cached project list and a `--repos` that matches none of it are different events.

`_apply_repo_filter` returned `{}` for both "the cache is empty" and "the cache is full and
your filter matched none of it", so every caller printed its own generic "No projects
loaded, run 'fetch' first". That advice cannot help: the filter is the problem, and
re-fetching will not change it. `fetch` already words the live-enumeration case correctly,
so this is the same event read from cache getting the same sentence.
"""

from __future__ import annotations

import logging

import pytest

from contextlake.core import _apply_repo_filter


@pytest.fixture
def emitted():
    """Everything the `contextlake` logger emits during the test.

    Neither stock fixture is reliable here. `capsys` passed alone and failed whenever an
    earlier test had called `setup_logging()` and rebound the handler to a stream pytest
    had since closed; `caplog` is vacuous once that setup sets `propagate=False`. Attaching
    a handler to the logger itself is immune to both, and the emptiness assertions in each
    test are what caught the two false readings rather than trusting either fixture.
    """
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("contextlake")
    handler = _Capture()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _config(repos: str | None) -> dict:
    """The config `repo_filter_patterns` reads is a plain mapping, not a ConfigParser:
    `config.get("repo_filter")` takes one argument there. Written as a dict after the
    ConfigParser version raised a TypeError unrelated to the behaviour under test."""
    return {"repo_filter": repos} if repos is not None else {}


_PROJECTS = {
    "team-a/svc-billing": {"full_path": "acme/team-a/svc-billing"},
    "team-b/svc-orders": {"full_path": "acme/team-b/svc-orders"},
}


def test_a_filter_matching_nothing_says_so_and_says_how_many_are_cached(emitted):
    """Captured at the LOGGER, not on stdout.

    A first version read `capsys` and passed alone, then failed whenever another test had
    called `setup_logging()` first and rebound the handler to a stream pytest had already
    closed. The `assert printed.strip()` guard is what turned that into a loud failure
    rather than a silent pass, and the fix is to capture where the message is emitted.
    """
    kept = _apply_repo_filter(_PROJECTS, _config("zzz-nomatch"))
    printed = "\n".join(emitted)
    assert kept == {}
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert "zzz-nomatch" in printed
    assert "2 project(s) are cached" in printed, (
        "the number is the whole point: it distinguishes a full cache from an empty one")
    assert "fetch" in printed, "the message must contradict the advice it replaces"


def test_an_empty_cache_says_nothing_here(emitted):
    """Silence is correct for a genuinely empty cache -- the caller's own message is right
    then, and a second line about a filter would be noise."""
    assert _apply_repo_filter({}, _config("zzz-nomatch")) == {}
    assert not [m for m in emitted if "No cached project matches" in m]


def test_a_filter_that_matches_is_silent_and_narrows(emitted):
    """Patterns are ANCHORED (fnmatch over the whole path), so a bare `svc-billing` selects
    a repo called exactly that and not one whose path merely ends in it. The glob is the
    documented way to ask, and using the bare name here first made this test fail for a
    reason that had nothing to do with the diagnostic under test."""
    kept = _apply_repo_filter(_PROJECTS, _config("*/svc-billing"))
    assert set(kept) == {"team-a/svc-billing"}
    assert not [m for m in emitted if "No cached project matches" in m]


def test_no_filter_at_all_is_silent_and_keeps_everything(emitted):
    kept = _apply_repo_filter(_PROJECTS, _config(None))
    assert kept == _PROJECTS
    assert not [m for m in emitted if "No cached project matches" in m]


def test_the_flag_the_callers_read_follows_the_same_distinction(emitted):
    """The diagnostic alone was not enough: it printed ABOVE each caller's own "No projects
    loaded, run `fetch` first", so the right explanation and the wrong advice appeared one
    line apart. The callers suppress their line by reading this flag, so the flag has to
    distinguish the two cases as precisely as the message does.
    """
    from contextlake.core import filter_matched_nothing

    _apply_repo_filter(_PROJECTS, _config("zzz-nomatch"))
    assert filter_matched_nothing() is True

    _apply_repo_filter({}, _config("zzz-nomatch"))
    assert filter_matched_nothing() is False, (
        "an empty cache is the case the caller's own advice is RIGHT for")

    _apply_repo_filter(_PROJECTS, _config("*/svc-billing"))
    assert filter_matched_nothing() is False

    _apply_repo_filter(_PROJECTS, _config(None))
    assert filter_matched_nothing() is False, "no filter at all cannot have emptied anything"
