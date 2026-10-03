"""Every place Mermaid is initialised pins `securityLevel: "strict"`.

Diagram text is built from repository content (symbol, table and resource names), which
is untrusted. `strict` makes Mermaid sanitise the rendered SVG and refuse click and
tooltip callbacks. `loose` or `antiscript` would let that text reach the page.

Nothing pinned this. It held because four calls happened to be written the same way. The
check reads the source of every place that configures Mermaid, so a new call, or an edit
to an old one, fails here.

Scope: the packaged dashboard and graph code under `src/contextlake`, and the docs
site builder `site/build_docs.py` when the checkout has it (an sdist or installed copy
does not). Vendored `*.min.js` files are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import contextlake

_PKG = Path(contextlake.__file__).resolve().parent
_SITE = _PKG.parents[1] / "site"
_HAS_SITE = (_SITE / "build_docs.py").is_file()
_SUFFIXES = {".js", ".html", ".py"}

# `mermaid.initialize(`, `window.mermaid.initialize(`, `mermaid.mermaidAPI.initialize(`.
_INIT = re.compile(r"\bmermaid(?:API)?\.initialize\s*\(")
# The old entry point. It takes its config per call, so no setting is pinned here.
_LEGACY_INIT = re.compile(r"\bmermaid\.init\s*\(")
# Anything that draws a diagram.
_RENDERS = re.compile(r"\bmermaid\.(?:render|run)\s*\(")
# Any `securityLevel: <value>`, as JS, JSON, or a `%%{init: ...}%%` directive.
_LEVEL = re.compile(r"""securityLevel["']?\s*:\s*(["'])?([A-Za-z]*)""")
_STRICT = re.compile(r"""securityLevel["']?\s*:\s*(["'])strict\1""")


def _sources() -> list[Path]:
    roots = [_PKG] + ([_SITE] if _HAS_SITE else [])
    return sorted(p for root in roots for p in root.rglob("*")
                  if p.is_file() and p.suffix in _SUFFIXES and not p.name.endswith(".min.js")
                  and "__pycache__" not in p.parts)


_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)


def _code_of(p: Path) -> str:
    """The text of ``p`` that can run. In an HTML page that is the ``<script>`` blocks
    only: the docs pages render the CHANGELOG, whose prose names `mermaid.initialize()`
    without calling it, so reading the whole page failed on any checkout that had built
    the site and passed on a fresh one."""
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix != ".html":
        return text
    return "\n".join(m.group(1) for m in _SCRIPT.finditer(text))


def _on_comment_line(text: str, pos: int) -> bool:
    """True when the line holding ``pos`` starts with a comment marker. Prose that names
    `mermaid.initialize()` is not a call. Only a line that STARTS as a comment counts, so
    a call with a trailing comment, or after other code, is still read as code."""
    line_start = text.rfind("\n", 0, pos) + 1
    return text[line_start:pos].lstrip().startswith(("//", "/*", "*", "#"))


def _matches(pattern: re.Pattern, text: str):
    return (m for m in pattern.finditer(text) if not _on_comment_line(text, m.start()))


def _call_text(text: str, start: int) -> str | None:
    """The text from the `(` at ``start`` to its matching `)`, across lines. None when it
    never closes. Only parentheses are counted, which is enough for these call sites and
    works on a JS call that sits inside a run of Python string literals."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _scan() -> tuple[dict[str, list[str]], list[str]]:
    """``({file: [call text, ...]}, problems)`` over every source."""
    calls: dict[str, list[str]] = {}
    problems: list[str] = []
    for p in _sources():
        text = _code_of(p)
        rel = (str(p.relative_to(_PKG)) if _PKG in p.parents
               else "site/" + str(p.relative_to(_SITE)))
        found = []
        for m in _matches(_INIT, text):
            call = _call_text(text, m.end() - 1)
            if call is None:
                problems.append(f"{rel}: an initialize call never closes, so it was not read")
                continue
            found.append(call)
            if not _STRICT.search(call):
                problems.append(f"{rel}: {' '.join(call.split())[:120]} does not pin "
                                f'securityLevel: "strict"')
        for m in _matches(_LEVEL, text):
            if m.group(1) is None or m.group(2) != "strict":
                problems.append(f"{rel}: securityLevel is {m.group(2) or '<not a string>'!r}")
        if any(_matches(_LEGACY_INIT, text)):
            problems.append(f"{rel}: calls mermaid.init(), which pins no securityLevel")
        if any(_matches(_RENDERS, text)) and not found:
            problems.append(f"{rel}: draws a diagram and never calls mermaid.initialize(), "
                            f"so it runs on Mermaid's default setting")
        if found:
            calls[rel] = found
    return calls, problems


def test_the_scan_finds_the_calls_it_exists_to_check():
    """A scan that finds no call passes the next test for nothing."""
    calls, _problems = _scan()
    dashboard = "kb/dashboard/static/dashboard.js"
    assert len(calls.get(dashboard, [])) >= 2, (
        "dashboard.js initialises Mermaid at load and again on every render")
    if _HAS_SITE:
        assert len(calls.get("site/build_docs.py", [])) >= 2


def test_every_mermaid_initialisation_pins_strict():
    _calls, problems = _scan()
    assert problems == []


@pytest.mark.parametrize("snippet, ok", [
    ('mermaid.initialize({ startOnLoad: false, securityLevel: "strict" })', True),
    ("mermaid.initialize({ securityLevel: 'strict',\n  maxEdges: 2 })", True),
    ('mermaid.initialize({ startOnLoad: false, securityLevel: "loose" })', False),
    ('mermaid.initialize({ securityLevel: "antiscript" })', False),
    ("mermaid.initialize({ startOnLoad: false })", False),
    ('mermaid.initialize({ securityLevel: level })', False),
])
def test_the_check_tells_strict_from_everything_else(snippet, ok):
    """The check itself, on text. Without this a regex that matched everything would
    leave the test above green."""
    m = _INIT.search(snippet)
    call = _call_text(snippet, m.end() - 1)
    assert bool(_STRICT.search(call)) is ok


def test_a_comment_that_names_initialize_is_not_a_call_but_code_is():
    comment = "  // mermaid.initialize() replaces the whole config each call\n"
    code = "  x = 1; mermaid.initialize()\n"
    assert list(_matches(_INIT, comment)) == []
    assert len(list(_matches(_INIT, code))) == 1
    assert len(list(_matches(_INIT, comment + code))) == 1
