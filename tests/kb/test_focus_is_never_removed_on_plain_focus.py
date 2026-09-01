"""No dashboard rule may remove the focus outline on plain `:focus`.

WCAG 2.4.7 wants the keyboard focus indicator visible. `outline: none` on `:focus`
removes it for everyone; on `:focus-visible` the browser removes it only where it
would not have drawn a ring anyway (a mouse click into a text field), which is the
whole point of that pseudo-class.

The E13 audit on 2026-09-01 found exactly one such rule, `.cl-palette__input:focus`.
It was not a failure at the time: the command palette is a `role="combobox"` holding a
single focusable field, focus lands there when it opens, the field keeps a contrasting
bottom border, and the active option is tracked with `aria-activedescendant`. It would
have BECOME one the moment a second control was added inside the palette, and nothing
would have reported that -- an invisible focus ring raises no error and breaks no test.

So the guard is on the rule, not on the palette. A count would let a new offender in as
long as an old one left; this names them.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb"
       / "dashboard" / "static" / "dashboard.css").read_text(encoding="utf-8")


def _rules_killing_focus(css: str) -> list[str]:
    """Selectors whose block sets `outline: none|0` and which match `:focus` but not
    `:focus-visible`/`:focus-within`."""
    # comments first: this file explains the very pattern it forbids, so a raw scan
    # matches the explanation and reports a rule that does not exist.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        selector, block = m.group(1).strip(), m.group(2)
        if not re.search(r"outline\s*:\s*(none|0)\b", block):
            continue
        for part in selector.split(","):
            part = part.strip()
            if re.search(r":focus(?!-visible|-within)", part):
                out.append(part)
    return out


def test_the_scan_can_see_a_focus_killing_rule():
    """The guard below asserts an EMPTY list, which is what a broken scan also returns.
    This proves the scan finds one when one is there."""
    assert _rules_killing_focus(".x:focus { outline: none; }") == [".x:focus"]
    assert _rules_killing_focus(".x:focus-visible { outline: none; }") == []
    assert _rules_killing_focus(".x:focus { color: red; }") == []


def test_no_rule_removes_the_outline_on_plain_focus():
    offenders = _rules_killing_focus(CSS)
    assert not offenders, (
        "these rules remove the focus indicator for keyboard users (WCAG 2.4.7); "
        "use :focus-visible instead:\n  " + "\n  ".join(offenders))
