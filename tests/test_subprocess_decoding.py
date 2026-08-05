"""Guard: every captured subprocess must decode leniently.

``subprocess.run(..., text=True)`` decodes the child's bytes with the strict
error handler, so a single byte the locale codec cannot map raises
``UnicodeDecodeError`` out of ``communicate()``. That exception is a
``ValueError``, so none of the ``except (OSError, subprocess.SubprocessError)``
guards this codebase wraps its git calls in catch it, and the whole command dies.

That is not hypothetical. ``kb connect`` over a 20-repository fleet aborted on
``'utf-8' codec can't decode byte 0x96 in position 99486`` -- 0x96 is a cp1252
en-dash, which is ordinary in commit messages written by older Windows tooling.
Git output is bytes: commit messages, author names and file paths carry whatever
encoding their author's machine used, and none of it is promised to be UTF-8.

So the rule is a property of the call, not of one file: anything that decodes a
child process's output passes ``errors=``. ``"replace"`` is the right default --
a mangled character in one commit subject must never abort an indexing run.

Keyed on the keyword rather than on the callee, deliberately: the GitLab
connector passes ``subprocess.run`` as an *argument* to a circuit breaker
(``breaker_for("glab-api").call(subprocess.run, ...)``), so ``text=True`` sits on
a call whose function is ``.call``. A callee-driven check reports green while
that site stays broken.

Lives outside ``tests/kb/`` on purpose: it only reads source text, never imports
``contextlake.kb``, so it runs in the core tier too -- which is where ``core.py``,
``safety.py`` and ``metrics.py`` live.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "contextlake"


def _kwargs(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _decodes_child_output(kw: dict[str, ast.expr]) -> bool:
    """Whether this call asks Python to decode a child process's bytes.

    Two spellings do it: ``text=True`` (what this codebase uses everywhere), and
    an explicit ``encoding=`` on a call that is also capturing output. The second
    is qualified by the capture keywords so that ordinary file reads
    (``read_text(encoding="utf-8")``) are not swept in -- those are a different
    question with different right answers.
    """
    text = kw.get("text")
    if isinstance(text, ast.Constant) and text.value is True:
        return True
    return "encoding" in kw and bool(
        {"capture_output", "stdout", "stderr", "input"} & set(kw)
    )


def _offenders(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _decodes_child_output(kw := _kwargs(node))
        and "errors" not in kw
    ]


def test_captured_subprocess_output_is_decoded_leniently():
    bad = {
        str(p.relative_to(SRC.parent.parent)): lines
        for p in sorted(SRC.rglob("*.py"))
        if (lines := _offenders(p))
    }
    assert not bad, (
        "these calls decode a child process's output strictly; add "
        'errors="replace" so one undecodable byte cannot abort the run:\n'
        + "\n".join(f"  {f}: line(s) {ls}" for f, ls in bad.items())
    )
