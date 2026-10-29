"""Guard: nothing the program prints, serves or writes holds an em-dash (U+2014).

Printed output uses a colon or a hyphen. A scripted caller that searches the output, and a
reader who copies a line into a terminal or an issue, both get plain ASCII punctuation.

Three scans cover the package:

1. Every string constant in a ``.py`` file under ``src/contextlake``, except docstrings
   and bare string statements (documentation, never output). The docstring of an MCP tool
   function is checked, because the MCP SDK sends it to the client as the tool description.
2. The live MCP tool catalog: the description, input schema and output schema of every tool
   a server offers. This reaches pydantic class docstrings and ``Field`` descriptions, which
   the scan of function docstrings does not. It needs the ``[kb]`` extra, so it lives in
   ``tests/kb/test_mcp_catalog_has_no_emdash.py``.
3. The shipped dashboard and graph-viewer pages (``.js``, ``.html``, ``.css``), with
   comments removed. Vendored ``*.min.js`` files are skipped: they are third-party code.

Comments are never output, so they stay as they are. The allowlist below holds what is left
over, each entry keyed by file, enclosing function and exact value, with its reason. An
entry that no longer matches anything fails, so the list cannot go stale.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

EM = "\u2014"
SRC = Path(__file__).resolve().parents[1] / "src" / "contextlake"

# (path under src/contextlake, enclosing function or "<module>", exact value, reason)
ALLOWED = [
    ("kb/git_hook.py", "<module>",
     "# >>> contextlake (managed) " + EM + " do not edit this block >>>",
     "legacy hook marker: read so an old hook is refreshed in place, never written"),
    ("kb/wiki/generate.py", "<module>",
     "Ground every statement in the facts above; do not speculate. Omit a section "
     "entirely if the facts above give you nothing to say for it " + EM + " do not write a "
     "heading with no content.",
     "prompt text sent to a model, not printed; tests/kb/test_kb_wiki.py pins it"),
    ("kb/wiki/generate.py", "render_prompt",
     "Key symbols (kind, name, file " + EM + " with signature/docstring where known):",
     "prompt text sent to a model, not printed"),
    ("kb/wiki/generate.py", "render_prompt", " " + EM + " ",
     "separator inside the facts block of the prompt sent to a model, not printed"),
    ("kb/wiki/council.py", "review", "\n\nReview lens " + EM + " ",
     "prompt text sent to a reviewer model, not printed"),
]

_TOOL_DECORATORS = {"bounded_tool", "tool"}


# --- the scans ---------------------------------------------------------------------------


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _is_tool(fn: ast.AST) -> bool:
    return isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        _decorator_name(d) in _TOOL_DECORATORS for d in fn.decorator_list)


class _Scan(ast.NodeVisitor):
    """Collects (line, enclosing function, value) for each string constant holding U+2014
    that is output. Docstrings and bare string statements are skipped, except the docstring
    of an MCP tool function."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.skip: set[int] = set()
        self.keep: set[int] = set()
        self.found: list[tuple[int, str, str]] = []
        self.tools: list[str] = []

    def _enter(self, node) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_tool(node):
            self.tools.append(node.name)
        body = node.body
        first = body[0] if body else None
        is_doc = (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                  and isinstance(first.value.value, str))
        if is_doc and not _is_tool(node):
            self.skip.add(id(first.value))
        elif is_doc:
            self.keep.add(id(first.value))
        name = getattr(node, "name", "<module>")
        if not isinstance(node, ast.Module):
            self.stack.append(name)
        self.generic_visit(node)
        if not isinstance(node, ast.Module):
            self.stack.pop()

    visit_Module = visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _enter

    def visit_Expr(self, node: ast.Expr) -> None:
        # A string on a line of its own is documentation (an attribute docstring).
        value = node.value
        if (isinstance(value, ast.Constant) and isinstance(value.value, str)
                and id(value) not in self.keep):
            self.skip.add(id(value))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and EM in node.value and id(node) not in self.skip:
            enclosing = next((n for n in reversed(self.stack)), "<module>")
            self.found.append((node.lineno, enclosing, node.value))


def scan_python(root: Path) -> tuple[list[tuple[str, int, str, str]], list[str]]:
    """(findings, tool function names) for every ``.py`` file under ``root``.

    A finding is (path relative to root, line, enclosing function, value)."""
    findings: list[tuple[str, int, str, str]] = []
    tools: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scan = _Scan()
        scan.visit(tree)
        rel = path.relative_to(root).as_posix()
        findings += [(rel, line, func, value) for line, func, value in scan.found]
        tools += scan.tools
    return findings, tools


def not_allowed(findings, allowed=ALLOWED):
    """The findings no allowlist entry covers: same file, same function, same exact value."""
    ok = {(p, f, v) for p, f, v, _reason in allowed}
    return [x for x in findings if (x[0], x[2], x[3]) not in ok]


def unused_entries(findings, allowed=ALLOWED):
    seen = {(p, f, v) for p, _line, f, v in findings}
    return [e for e in allowed if (e[0], e[1], e[2]) not in seen]


_BLOCK = re.compile(r"/\*.*?\*/|<!--.*?-->", re.S)
_LINE = re.compile(r"(?:^|(?<=\s))//[^\n]*", re.M)
_ESCAPES = re.compile(r"\\u2014|&mdash;|&#8212;|&#x2014;", re.I)


def _blank(match: re.Match) -> str:
    return "\n" * match.group().count("\n")


def static_files(root: Path) -> list[Path]:
    """The pages the static scan reads: every .js, .html and .css file but vendored *.min.js."""
    return [p for p in sorted(root.rglob("*"))
            if p.suffix in {".js", ".html", ".css"} and not p.name.endswith(".min.js")]


def scan_static(root: Path) -> list[tuple[str, int, str]]:
    """(path, line, text) for each em-dash, or an escape that renders as one, outside a
    comment in a ``.js``, ``.html`` or ``.css`` file under ``root``.

    Comments come out first: ``/* */`` and ``<!-- -->`` blocks, and ``//`` to the end of a
    line when the ``//`` starts the line or follows a space (so ``http://`` survives).
    A ``//`` inside a string literal after a space would hide the rest of that line. None of
    the shipped pages has one today, and a miss costs one unflagged line, never a false alarm."""
    found: list[tuple[str, int, str]] = []
    for path in static_files(root):
        text = _LINE.sub("", _BLOCK.sub(_blank, path.read_text(encoding="utf-8")))
        for n, line in enumerate(text.split("\n"), 1):
            if EM in line or _ESCAPES.search(line):
                found.append((path.relative_to(root).as_posix(), n, line.strip()[:100]))
    return found


# --- the package ---------------------------------------------------------------------------


def test_no_python_string_in_the_package_holds_an_em_dash():
    findings, _tools = scan_python(SRC)
    bad = not_allowed(findings)
    assert not bad, (
        "an em-dash in a string the program prints, logs or serves (use a colon or a hyphen):\n"
        + "\n".join(f"{p}:{line} in {func}: {value[:80]!r}" for p, line, func, value in bad))


def test_every_allowlist_entry_still_matches_something():
    findings, _tools = scan_python(SRC)
    stale = unused_entries(findings)
    assert not stale, f"allowlist entries that match nothing, remove them: {stale}"


def test_every_allowlist_entry_gives_a_reason():
    assert all(reason.strip() for *_key, reason in ALLOWED)


def test_no_shipped_page_shows_an_em_dash():
    bad = scan_static(SRC)
    assert not bad, "an em-dash outside a comment in a shipped page:\n" + "\n".join(
        f"{p}:{n}: {text}" for p, n, text in bad)


# --- the guard itself: each rule is shown to fail on a seeded case ---------------------------


def _tree(tmp_path: Path, **files: str) -> Path:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_a_printed_string_is_flagged(tmp_path):
    root = _tree(tmp_path, **{"a.py": f'def f():\n    print("done {EM} ok")\n'})
    findings, _ = scan_python(root)
    assert [(p, f) for p, _l, f, _v in findings] == [("a.py", "f")]


def test_a_dash_in_an_f_string_part_is_flagged(tmp_path):
    root = _tree(tmp_path, **{"a.py": f'def f(x):\n    log(f"{{x}} {EM} ok")\n'})
    findings, _ = scan_python(root)
    assert len(findings) == 1 and findings[0][3] == f" {EM} ok"


def test_a_dash_written_as_an_escape_is_flagged(tmp_path):
    root = _tree(tmp_path, **{"a.py": 'X = "a \\u2014 b"\n'})
    findings, _ = scan_python(root)
    assert len(findings) == 1


def test_docstrings_and_bare_strings_are_not_flagged(tmp_path):
    src = (f'"""Module {EM} doc."""\n\n\nclass C:\n    """Class {EM} doc."""\n\n'
           f'    attr = 1\n    """Attribute {EM} doc."""\n\n\n'
           f'def f():\n    """Function {EM} doc."""\n    # comment {EM} here\n')
    root = _tree(tmp_path, **{"a.py": src})
    assert scan_python(root)[0] == []


def test_the_docstring_of_a_tool_function_is_flagged(tmp_path):
    src = (f"def build(mcp):\n    @bounded_tool\n    def find(x):\n"
           f'        """Find {EM} a thing."""\n\n'
           f"    @mcp.tool()\n    def other(x):\n"
           f'        """Other {EM} thing."""\n\n'
           f'    def helper(x):\n        """Helper {EM} thing."""\n')
    root = _tree(tmp_path, **{"a.py": src})
    findings, tools = scan_python(root)
    assert sorted(tools) == ["find", "other"]
    assert sorted(f for _p, _l, f, _v in findings) == ["find", "other"]


def test_an_allowlist_entry_covers_one_value_in_one_function_only(tmp_path):
    root = _tree(tmp_path, **{"a.py": (
        f'M = "marker {EM} x"\n\n\n'
        f'def f():\n    return "marker {EM} x"\n\n\n'
        f'def g():\n    return "marker {EM} x and more"\n')})
    findings, _ = scan_python(root)
    allowed = [("a.py", "<module>", f"marker {EM} x", "test")]
    left = not_allowed(findings, allowed)
    assert sorted((f, v) for _p, _l, f, v in left) == [
        ("f", f"marker {EM} x"), ("g", f"marker {EM} x and more")]
    assert unused_entries(findings, [("a.py", "h", f"marker {EM} x", "test")]) != []


def test_the_legacy_hook_marker_is_allowed_only_by_its_exact_value():
    findings, _ = scan_python(SRC)
    assert any(p == "kb/git_hook.py" for p, *_rest in findings)
    padded = [(p, line, f, v + " " if p == "kb/git_hook.py" else v)
              for p, line, f, v in findings]
    assert [x for x in not_allowed(padded) if x[0] == "kb/git_hook.py"]


def test_the_static_scan_flags_visible_text_and_ignores_comments(tmp_path):
    root = _tree(
        tmp_path,
        **{"a.js": (f'// comment {EM} here\nvar a = "run {EM} now";\n'
                    f'/* block {EM}\n   more */\nvar b = "x \\u2014 y";\n'
                    f'var u = "http://x.test/a";  // tail {EM}\n'),
           "a.html": f"<!-- note {EM} -->\n<title>a {EM} b</title>\n<p>x &mdash; y</p>\n",
           "a.css": f"/* css {EM} */\na::after {{ content: \"{EM}\"; }}\n",
           "vendor.min.js": f'var v = "{EM}";\n'})
    assert [(p, n) for p, n, _t in scan_static(root)] == [
        ("a.css", 2), ("a.html", 2), ("a.html", 3), ("a.js", 2), ("a.js", 5)]


def test_the_static_scan_reaches_the_shipped_pages():
    seen = {p.relative_to(SRC).as_posix() for p in static_files(SRC)}
    assert {"kb/dashboard/static/dashboard.html", "kb/static/app.js",
            "kb/dashboard/static/dashboard.js", "kb/static/app.css"} <= seen
    assert not any(name.endswith(".min.js") for name in seen)
