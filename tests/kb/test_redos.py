"""ReDoS / pathological-input resilience for the regex-based extractors.

These parsers run directly on untrusted repository content -- a cloned repo's
``.sql``/``.tf``/manifest/source files, never validated or sanitized before
reaching them. There is no per-parser timeout anywhere in the indexing
pipeline, so a single adversarial (or just corrupted/truncated) file could
hang the whole ``contextlake kb index`` run. Each test here bounds itself with
``@pytest.mark.timeout(2)`` and asserts *parse-or-degrade*: the parser must
return (possibly empty/partial results), never raise, never hang past budget.

The corpus lives in ``tests/kb/fixtures/fuzz/`` -- four pathological shapes
(long single-character runs, deeply nested brackets, a heavily repeated
statement prefix, unbalanced quotes) per extractor family. Sizes are kept in
the low tens-of-KB: large enough to be a meaningful stress case, small enough
that a healthy parser finishes in milliseconds and the whole file stays a
negligible fraction of the suite's ~65s budget.

Two tests below are marked ``xfail`` for REAL bugs this fuzzing found (see
RC-P1-6 report): ``parse_manifest`` on a Maven ``pom.xml`` with many unclosed
``<dependency>``/``<parent>`` tags is O(n^2) (confirmed by direct
measurement: 6s at 8k reps, ~25s at 16k reps -- quadratic scaling, not just a
slow constant), and ``parse_hcl`` on deeply nested block bodies is *also*
O(n^2) -- but, verified by isolating the raw ``tree-sitter-hcl`` ``parse()``
call (linear, 33ms at depth 10000) from the rest of ``parse_hcl``, the
quadratic cost is in contextlake's *own* ``_src_id_for`` helper (an
O(depth)-per-call parent-chain walk run once per ``variable_expr`` node, of
which there are O(depth) at this depth), not in the third-party grammar.
Neither is fixed here -- this task is tests only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contextlake.kb.flow.http import extract_http_flow
from contextlake.kb.hcl import parse_hcl
from contextlake.kb.manifest import parse_manifest
from contextlake.kb.sql import parse_sql

FUZZ_DIR = Path(__file__).parent / "fixtures" / "fuzz"


def _read(name: str) -> bytes:
    return (FUZZ_DIR / name).read_bytes()


# ---------------------------------------------------------------------------
# SQL parser (kb/sql.py) -- pure regex, no AST fallback.
# ---------------------------------------------------------------------------

_SQL_FIXTURES = [
    "sql_long_run.sql",
    "sql_deep_nested_brackets.sql",
    "sql_repeated_prefix.sql",
    "sql_unbalanced_quotes.sql",
]


@pytest.mark.timeout(2)
@pytest.mark.parametrize("fixture", _SQL_FIXTURES)
def test_sql_parser_survives_pathological_input(fixture):
    nodes, refs = parse_sql("repo", "schema.sql", _read(fixture))
    assert isinstance(nodes, list)
    assert isinstance(refs, list)


# ---------------------------------------------------------------------------
# HCL parser (kb/hcl.py) -- tree-sitter AST, not regex, but still consumes
# untrusted .tf content directly and is named explicitly in this task's
# extractor list, so it gets the same pathological-input treatment.
# ---------------------------------------------------------------------------

_HCL_FIXTURES = [
    "hcl_long_run.tf",
    "hcl_deep_nested_braces.tf",
    "hcl_repeated_resource_prefix.tf",
    "hcl_unbalanced_quotes.tf",
]


@pytest.mark.timeout(2)
@pytest.mark.parametrize("fixture", _HCL_FIXTURES)
def test_hcl_parser_survives_pathological_input(fixture):
    nodes, refs = parse_hcl("repo", "main.tf", _read(fixture))
    assert isinstance(nodes, list)
    assert isinstance(refs, list)


# ---------------------------------------------------------------------------
# Manifest parser (kb/manifest.py) -- regex for .csproj/pom.xml, json/tomllib
# for package.json/pyproject.toml (those two can't ReDoS: dict-based parsers,
# not regex, and both decode errors are already caught).
# ---------------------------------------------------------------------------

_MANIFEST_FIXTURES = [
    ("manifest_pom_long_run.xml", "pom.xml"),
    ("manifest_pom_deep_nested.xml", "pom.xml"),
    ("manifest_pom_repeated_dependency_small.xml", "pom.xml"),
    ("manifest_pom_unbalanced_quotes.xml", "pom.xml"),
    ("manifest_package_deep_nested.json", "package.json"),
    ("manifest_csproj_repeated_prefix.csproj", "lib.csproj"),
]


@pytest.mark.timeout(2)
@pytest.mark.parametrize("fixture,rel_path", _MANIFEST_FIXTURES)
def test_manifest_parser_survives_pathological_input(fixture, rel_path):
    nodes, edges = parse_manifest("repo", rel_path, _read(fixture))
    assert isinstance(nodes, list)
    assert isinstance(edges, list)


# ---------------------------------------------------------------------------
# HTTP-flow extractor (kb/flow/http.py) -- regex, framework-targeted per lang.
# ---------------------------------------------------------------------------

_HTTP_FIXTURES = [
    # The two Python fixtures are deliberately invalid Python (unterminated
    # string literal / undefined names) -- fine for extract_http_flow, which
    # is a regex extractor and never calls ast.parse, but ruff lints every
    # *.py file it finds regardless of what reads it. Kept as .txt so `ruff
    # check tests` doesn't try to compile them as real Python source.
    ("http_py_long_run.txt", "python"),
    ("http_py_repeated_prefix.txt", "python"),
    ("http_js_unbalanced_quotes.js", "javascript"),
    ("http_cs_deep_nested.cs", "csharp"),
]


@pytest.mark.timeout(2)
@pytest.mark.parametrize("fixture,lang", _HTTP_FIXTURES)
def test_http_flow_extractor_survives_pathological_input(fixture, lang):
    nodes, edges = extract_http_flow("repo", f"app.{lang}", _read(fixture), lang)
    assert isinstance(nodes, list)
    assert isinstance(edges, list)


# ---------------------------------------------------------------------------
# Known bugs (xfail, not fixed here) -- confirmed by direct wall-clock
# measurement before writing these tests (see RC-P1-6 report for the raw
# numbers). Reproduced at a scale chosen to reliably exceed the 2s budget
# while still completing in a few seconds if the timeout somehow didn't fire
# (verified empirically that pytest-timeout's SIGALRM interrupts CPython's
# regex engine mid-match, so these consistently land as a ~2s XFAIL, not a
# multi-second hang). Generated in-code rather than committed as fixture
# files: the repro needs a precisely-tuned repetition count, and committing a
# ~150KB blob to a public repo whose only purpose is "be slow" isn't worth it
# when a 3-line generator reproduces it exactly.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=False,
    reason=(
        "REAL BUG (RC-P1-6): parse_manifest's Maven dependency-block regex "
        "(_MVN_DEP_BLOCK = r'<dependency\\b[^>]*>(.*?)</dependency>', DOTALL) "
        "is O(n^2) on a pom.xml with many unclosed <dependency> tags: for each "
        "of k occurrences, the lazy .*? scans forward to the (absent) closing "
        "tag, i.e. to end-of-string, before failing and trying the next "
        "occurrence -- O(k * remaining_length) total. Measured directly (no "
        "timeout): 8k reps/80KB tail -> 6.2s, 16k reps/160KB tail -> 24.9s; "
        "doubling input ~quadruples time, confirming O(n^2) not just a large "
        "constant. _MVN_NON_PROJECT and _MVN_PARENT_BLOCK share the same "
        "'[^>]*>(.*?)</TAG>' shape and are presumed to share the bug (not "
        "independently measured at this scale). A ~350KB adversarial or "
        "merely truncated/corrupted pom.xml is a plausible real file and "
        "would hang kb indexing for tens of seconds. Not fixed here -- tests "
        "only per task scope."
    ),
)
@pytest.mark.timeout(2)
def test_manifest_pom_unclosed_dependency_tags_is_quadratic():
    # pytest-timeout is what turns this into a fast ~2s XFAIL; without the
    # plugin registered, nothing would interrupt the call and it would just
    # run to its full ~13s before (non-strict) XPASSing. importorskip keeps
    # this test itself fast and honest when the dev extra isn't installed,
    # matching test_properties.py's guard for the same "optional dev
    # dependency, not always present" reason.
    pytest.importorskip("pytest_timeout")
    # 12k reps was empirically ~13s unbounded; comfortably over the 2s budget
    # while the whole test (interrupted at ~2s by pytest-timeout) stays fast.
    content = b"<dependency>" * 12_000 + b"x" * 120_000
    parse_manifest("repo", "pom.xml", content)


@pytest.mark.xfail(
    strict=False,
    reason=(
        "REAL BUG (RC-P1-6): parse_hcl is O(n^2) on deeply nested block "
        "bodies -- and it's contextlake's own code, not the tree-sitter-hcl "
        "grammar. Measured directly (no timeout) on "
        "'resource \"a\" \"b\" {\\n' + '  x = {\\n'*depth + '}\\n'*depth + '}\\n': "
        "full parse_hcl() takes depth=2500 -> 0.28s, depth=5000 -> 1.10s, "
        "depth=10000 -> 4.52s (~4x time per 2x depth == O(depth^2)). Isolating "
        "the raw tree_sitter Parser.parse() call from the rest of parse_hcl "
        "shows parse() alone is fast and linear (depth=10000 -> 0.033s, "
        "~1% of the total) -- the quadratic cost is entirely in what "
        "parse_hcl does with the tree afterward. This deeply-right-nested "
        "'x = { x = { ... } }' shape is parsed as ~depth variable_expr nodes "
        "(one per nesting level; NOT the zero I first assumed before "
        "measuring -- an earlier version of this reason was wrong about "
        "that). hcl.py's _src_id_for(node) (~line 199) walks node.parent up "
        "to the tree root on every call to find the enclosing top-level "
        "block; called once per variable_expr node at increasing depths, "
        "that's an O(depth)-per-call walk run O(depth) times == O(depth^2) "
        "total, squarely in contextlake's own code. A deeply/adversarially "
        "nested .tf file can still hang kb indexing. Not fixed here -- tests "
        "only per task scope; a real fix would cache each node's enclosing "
        "top-level block instead of walking to the root from scratch every "
        "time (e.g. during the same _walk pass that already visits every "
        "node once)."
    ),
)
@pytest.mark.timeout(2)
def test_hcl_deep_nesting_is_quadratic():
    pytest.importorskip("pytest_timeout")  # see the sibling manifest test's comment
    depth = 10_000
    content = (
        b'resource "a" "b" {\n' + b"  x = {\n" * depth + b"}\n" * depth + b"}\n"
    )
    parse_hcl("repo", "main.tf", content)
