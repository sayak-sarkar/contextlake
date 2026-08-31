"""The dashboard must render once at boot, not twice (E12).

`boot()` attaches a `hashchange` listener and then, when the page is opened with
no hash, gives it a default. Assigning `location.hash` FIRES `hashchange`, and
the listener is already attached by then, so the page rendered twice: once from
the explicit `CL.router.render()` and once from the event.

Both renders fetch `/api/overview`, and they overlap, so neither can serve the
other from cache. Measured in a browser against a 961,633-node store: two
requests 35 ms apart taking 2,769 ms and 4,005 ms, while the page shell was
ready in 114 ms. The whole wait was one endpoint being asked for twice.

`history.replaceState` writes the same URL and fires no event. The router never
needed the assignment at all: `parseHash` already reads an absent hash as
`/fleet`, so the line only makes the default visible in the address bar.

Read from the source the same way the other dashboard guards do. A browser test
would assert this more directly, and there is no browser in this suite; the
behaviour was verified in headless Chromium when the fix landed, and this pins
the shape that made it wrong so it cannot come back by an edit.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb" / "dashboard" / "static"
JS = (STATIC / "dashboard.js").read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """`dashboard.js` without comments.

    The comment on the fix quotes the very pattern it warns against, so a
    substring check over the raw file matches the warning and passes while the
    code does the wrong thing.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//.*$", "", src)


def _boot_body() -> str:
    code = _strip_comments(JS)
    start = code.index("function boot()")
    depth, i = 0, code.index("{", start)
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[i:j + 1]
    raise AssertionError("boot() body not found")


def test_boot_sets_the_default_hash_without_firing_hashchange():
    """The whole fix: replaceState writes the URL and dispatches nothing."""
    body = _boot_body()
    assert "history.replaceState" in body, (
        "boot() must set the default hash with history.replaceState. Assigning "
        "location.hash fires hashchange, and the listener attached above it then "
        "renders a second time, so /api/overview is fetched twice on every load."
    )


def test_boot_still_renders_once_explicitly():
    """The explicit render is what guarantees the page draws at all.

    replaceState fires no event, so nothing else would trigger the first render.

    The `hashchange` listener's own body also calls `render()`, so it is dropped
    before counting. Counting it was the first version of this test, and it
    failed against the correct code, which is the shape a guard has to not have.
    """
    body = _boot_body()
    listener = r"window\.addEventListener\(\s*\"hashchange\".*?\);"
    without_listener = re.sub(listener, "", body, flags=re.S)
    assert without_listener.count("CL.router.render()") == 1


def test_the_bare_location_hash_assignment_survives_only_as_a_fallback():
    """A browser without replaceState still has to reach the fleet view.

    That path renders twice, which is the old behaviour and is correct, just
    wasteful. It must stay behind the capability check rather than becoming the
    default again.
    """
    body = _boot_body()
    if 'location.hash = "#/fleet"' in body:
        guard = body[:body.index('location.hash = "#/fleet"')]
        assert "history.replaceState" in guard, (
            "the location.hash assignment must sit in the else-branch of a "
            "replaceState capability check, not run unconditionally"
        )


def test_the_router_does_not_depend_on_the_hash_being_set():
    """`parseHash` defaults an absent hash to /fleet.

    This is why the assignment is cosmetic and why removing its side effect is
    safe. If this stops being true, the fix above needs rethinking rather than
    keeping.
    """
    code = _strip_comments(JS)
    assert re.search(r'location\.hash\.replace\([^)]*\)\s*\|\|\s*"/fleet"', code), (
        "parseHash no longer defaults an empty hash to /fleet, so boot() setting "
        "the hash is load-bearing again"
    )
