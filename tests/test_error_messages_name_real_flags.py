"""An error message that names a CLI flag must name one that exists.

`RepoTooLarge` told the user to "narrow it with --languages" from the day it shipped
(`fa1af6fc`). There has never been a `--languages` flag. A reader who followed the
advice got "unrecognized arguments" and no way to act on the error they were handed.
The setting is real, but it lives in `kb.toml` as `kb.languages`, and only the message
was wrong.

Nothing could catch that. The string is built inside an exception constructor, so no
test that does not TRIGGER the exception ever reads it, and the flag it named looked
exactly like the many real ones around it.

Scope is deliberate. A scan of every string literal in `src/` finds 178 flag-shaped
tokens and calls 77 of them missing -- git's `--abbrev-ref`, the AWS CLI's
`--cli-input-json`, relation names like `--calls`, and CSS custom properties such as
`--deepwater`, which are not flags at all. A guard with that noise floor gets ignored,
which is the failure mode of a bare pattern (see the `just`/`only` lint that flagged 45
correct sentences). Restricting it to strings inside a `raise` or an exception's
`__init__` drops it to four hits, and all four belong to other programs.
"""

from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "contextlake"
FLAG = re.compile(r"--[a-z][a-z0-9-]{2,}")

# Flags belonging to OTHER programs, quoted so the user can run them. Each is here
# because the message tells you to run that program, not contextlake.
FOREIGN = {
    "--help",             # argparse's own, on every parser including ours
    "--force-reinstall",  # pip, in the "your install is broken" message
    "--user", "--now",    # systemctl, in the unit-activation instructions
}


def _registered_flags() -> set[str]:
    """Every long flag any parser in the package registers."""
    flags = set()
    for f in SRC.rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        flags.update(m.group(1) for m in
                     re.finditer(r'add_argument\(\s*"(--[a-z0-9-]+)"', src))
    return flags


def _exception_strings():
    """(file, string) for every literal inside a `raise` or an exception `__init__`."""
    for f in SRC.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:                     # pragma: no cover - none today
            continue
        for node in ast.walk(tree):
            raising = (isinstance(node, ast.Raise) and node.exc is not None) or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "__init__")
            if not raising:
                continue
            for n in ast.walk(node):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    yield f.relative_to(SRC).as_posix(), n.value


def test_no_error_message_names_a_flag_that_does_not_exist():
    real = _registered_flags()
    # the scan found something to scan -- otherwise every assertion below is vacuous
    assert len(real) > 50, f"only {len(real)} flags found; the parser scan is broken"
    seen_any = False
    offenders = []
    for path, text in _exception_strings():
        for flag in FLAG.findall(text):
            seen_any = True
            if flag in real or flag in FOREIGN:
                continue
            offenders.append(f"{path}: {flag!r} in {text.strip()[:80]!r}")
    assert seen_any, "no flags found in any exception message; the AST walk is broken"
    assert not offenders, (
        "these messages tell the user to pass a flag that is not registered:\n  "
        + "\n  ".join(offenders))


def test_the_repo_too_large_message_names_the_setting_that_exists():
    """The specific case above, pinned separately.

    The general guard passes if the advice is deleted outright, and advice that is
    merely absent is a different, quieter defect: the error still tells the reader
    their repository is too big and nothing about what to do.
    """
    from contextlake.kb.parse import RepoTooLarge

    msg = str(RepoTooLarge("some/repo", 9 * 1073741824, 4 * 1073741824, {"c": 5}))
    assert "kb.languages" in msg
    assert "--languages" not in msg
    assert "kb.max_repo_memory" in msg
