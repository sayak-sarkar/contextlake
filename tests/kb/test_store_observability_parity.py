"""Every place that opens a store must register it for --redact and --metrics-file.

`_open_store` did this inline, under a comment reading "Every kb command funnels through
here". Four constructions did not funnel through it: `cmds/doctor.py`,
`dashboard/server.py`, and `dashboard/site.py` twice. Measured consequence:
`kb dashboard --site --redact` printed a raw repository id that `kb lint --redact` had
just redacted -- on the artefact most likely to be shared with somebody else.

The comment was true of the funnel and false of the codebase, which is the more dangerous
combination: it told every later reader that the invariant was already handled.

This is a parity sweep for the same reason the store-lock one is. The defect was not a
bad registration, it was four call sites nobody enumerated -- so the list of sites is
derived from the source rather than typed here, and a fifth `SqliteStore(...)` added
tomorrow fails this test until it is registered too.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "contextlake"

#: The one place allowed to construct a store without calling the registrar, because it
#: IS the registrar's home and calls it directly.
DEFINING_MODULE = SRC / "kb" / "cmds" / "_common.py"

#: Not a real store: the class definition itself.
CLASS_HOME = SRC / "kb" / "store" / "sqlite_store.py"


def _modules_constructing_a_store() -> list[pathlib.Path]:
    """Every module with a `SqliteStore(...)` call, found by parsing rather than grep.

    Parsed so a mention in a docstring, a comment or a type annotation cannot satisfy
    or trip the sweep -- this test exists because something read as handled and was not.
    """
    out = []
    for path in sorted(SRC.rglob("*.py")):
        if path in (CLASS_HOME,):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "SqliteStore"):
                out.append(path)
                break
    return out


def test_every_module_that_opens_a_store_registers_it():
    """THE LOAD-BEARING ASSERTION. Fails for doctor.py, server.py and site.py before."""
    missing = []
    for path in _modules_constructing_a_store():
        src = path.read_text(encoding="utf-8")
        if "register_store_for_observability" not in src:
            missing.append(str(path.relative_to(SRC)))
    assert not missing, (
        "these modules construct a store without registering it, so --redact will not "
        "hide its repository ids and --metrics-file will lose its graph gauges:\n  "
        + "\n  ".join(missing))


def test_the_sweep_actually_finds_the_known_call_sites():
    """The near-miss. Both assertions above pass vacuously if the AST walk finds
    nothing -- which is exactly how a 'green' parity test can mean the opposite."""
    found = {p.name for p in _modules_constructing_a_store()}
    for expected in {"_common.py", "doctor.py", "server.py", "site.py"}:
        assert expected in found, (
            f"the sweep did not find {expected}, which is known to open a store — "
            "the walk is not looking where it thinks it is")


def test_the_registrar_covers_module_names_not_only_repo_ids():
    """A redacted export still emitted `repo-<hash>::<real-subsystem-dir>`, because only
    `list_repos()` was registered. Half a redaction reads exactly like a whole one."""
    src = DEFINING_MODULE.read_text(encoding="utf-8")
    i = src.find("def register_store_for_observability")
    assert i > 0
    body = src[i:i + 2500]
    assert "repo_modules" in body, (
        "module/subsystem names are not registered, so a redacted export still names "
        "real directories")
