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

This fuzzing found two REAL quadratic blowups, both since fixed:
``parse_manifest`` on a Maven ``pom.xml`` with many unclosed
``<dependency>``/``<parent>``/``<dependencies>`` tags, and ``parse_hcl`` on
deeply nested block bodies -- the latter in contextlake's own tree navigation,
not in the third-party grammar (the raw ``tree-sitter-hcl`` ``parse()`` was
linear and about 1% of the runtime). The last two tests in this file are the
regression guards for those fixes and carry the before/after numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contextlake.kb.flow.http import extract_http_flow
from contextlake.kb.hcl import parse_hcl
from contextlake.kb.manifest import parse_manifest
from contextlake.kb.sql import parse_sql
from contextlake.kb.xml_cfg import parse_xml_config
from contextlake.kb.xsd import parse_xsd
from contextlake.kb.xsl import parse_xsl

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
# Regression guards for two quadratic blowups this fuzzing found and that are
# now fixed. Both were confirmed by direct wall-clock measurement before and
# after the fix, at the sizes below. They stay here as *active* tests (they
# were xfail while the bugs were open) because both fixes are easy to undo
# accidentally: the manifest one by reintroducing a single
# `<tag ...>(.*?)</tag>` regex, the HCL one by reaching for `Node.parent` or
# `Node.next_sibling` again. Inputs are generated in-code rather than committed
# as fixture files -- the repro needs a tuned repetition count, and committing
# a ~150KB blob whose only purpose is "be slow" isn't worth it when three
# lines reproduce it exactly.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(2)
def test_manifest_pom_unclosed_dependency_tags_is_not_quadratic():
    """A pom.xml full of unclosed <dependency> tags must not blow up.

    The old `_MVN_DEP_BLOCK = r'<dependency\\b[^>]*>(.*?)</dependency>'` (DOTALL)
    was O(n^2) here: for each of k openers the lazy `.*?` scanned forward to the
    absent closing tag, i.e. to end-of-string, before failing and moving on.
    Measured 6.5s at 8k reps/80KB tail and 39.8s at 16k/160KB -- more than
    quadrupling per doubling. `_MVN_PARENT_BLOCK` (6.0s -> 25.6s) and
    `_MVN_NON_PROJECT` (7.7s -> 29.0s) shared the shape and the bug; all three
    now pair openers with closing tags found in one linear pass and land at
    ~0.001s. A truncated pom from an aborted download is enough to trigger this;
    it does not take an attacker.
    """
    # pytest-timeout is what bounds this if the fix is ever reverted; without
    # the plugin registered nothing would interrupt the call and a regression
    # would show up as a ~18s hang instead of a failure. importorskip keeps
    # this test honest when the dev extra isn't installed, matching
    # test_properties.py's guard for the same reason.
    pytest.importorskip("pytest_timeout")
    content = b"<dependency>" * 12_000 + b"x" * 120_000
    nodes, edges = parse_manifest("repo", "pom.xml", content)
    assert isinstance(nodes, list)
    assert isinstance(edges, list)


# 10s, not the 2s its siblings use, and the wider budget is calibrated rather
# than guessed. This test flaked on the py3.13 CI job while every other test in
# the file sat at or below 0.02s. The 2s budget had been measured on a dev
# machine with no coverage; the knowledge-layer CI job runs under `--cov`, and
# coverage.py traces every line of a traversal that visits 10_000 nesting
# levels. Measured on py3.13: 0.27s bare, 0.60s under coverage, and the shared
# CI runner is roughly 3x slower again, which lands the worst shape (`locals`)
# right on the 2s line. That is a coin flip, not a budget.
#
# Widening it does not blunt the regression signal, because the gap between
# working and broken is enormous: the pre-fix O(depth^2) implementation took
# 6.03s bare at this depth, so it would need roughly 40s under the same CI
# conditions. 10s leaves the fixed code about 5x headroom and still catches a
# reintroduced blowup about 4x over.
@pytest.mark.timeout(10)
@pytest.mark.parametrize("shape", ["no_refs", "refs", "locals"])
def test_hcl_deep_nesting_is_not_quadratic(shape):
    """Deeply nested block bodies must not blow up -- and the cost was ours.

    `parse_hcl` was O(depth^2): 0.38s / 1.40s / 6.03s at depth 2500 / 5000 /
    10000, while the raw tree_sitter `Parser.parse()` of the deepest of those
    took 0.034s (about 1% of the total). The cause was py-tree-sitter's
    `Node.parent` and `Node.next_sibling`, which each re-descend from the tree
    root -- O(depth) per access, called once per node at increasing depth.
    `hcl._walk_ctx` now carries that context down the traversal instead.

    Three shapes because they exercise different accessors, and the first one
    alone would have missed two of them: `no_refs` produces zero refs (every
    `variable_expr` is a bare object key, so `_reference_address` returns None)
    and so only ever reaches `_reference_segments`/`next_sibling`; `refs` and
    `locals` produce a ref per level and so also reach `_src_id_for` and the
    enclosing-`locals`-attribute lookup, which used `.parent`.
    """
    pytest.importorskip("pytest_timeout")  # see the sibling manifest test's comment
    depth = 10_000
    if shape == "no_refs":
        content = b'resource "a" "b" {\n' + b"  x = {\n" * depth + b"}\n" * depth + b"}\n"
    elif shape == "refs":
        body = "".join(f"  x{i} = {{\n    r{i} = var.v{i}\n" for i in range(depth))
        content = ('resource "a" "b" {\n' + body + "  }\n" * depth + "}\n").encode()
    else:
        body = "".join(f"    x{i} = {{\n      r{i} = var.v{i}\n" for i in range(depth))
        content = ("locals {\n  top = {\n" + body + "    }\n" * depth + "  }\n}\n").encode()
    nodes, refs = parse_hcl("repo", "main.tf", content)
    assert isinstance(nodes, list)
    assert isinstance(refs, list)


# ---------------------------------------------------------------------------
# XML, XSD, XSL and SQL scanners -- shapes that made one small file stall the run.
#
# Each regex below rescanned the rest of the text from every start that could not
# finish, so the time grew with the square of the file. A truncated file is enough to
# trigger it; it does not take an attacker. Measured before the fix, one parse, xml_cfg /
# xsd / xsl in seconds, at the size named and at double it:
#   `<a ` * 3_000 (9 KB)                  1.6 / 0.72 / 0.74 -> 6.4 / 3.9 / 3.5    `_TAG`
#   `<a ` + `b` * 9_000 + `>` (9 KB)      0.99 / 1.1 / 1.2 -> 4.2 / 4.2 / 4.0     `_ATTR`
#   `<!-- ` * 3_000 (15 KB)               0.20 / 0.33 / 0.29 -> 0.82 / 1.1 / 1.1  `_MASKED`
#   `<a "'<a '"<a "'` * 3_000 (45 KB)     9.3 / 4.3 / 4.4 -> 39 / 21 / 23         `_TAG`
# SQL, in seconds: `CREATE TRIGGER t1 ` + `AFTER ` * 3_000 (18 KB) 0.52, 36 KB 2.6
# (`_TRIGGER_ON`); a table followed by 9 KB of blank lines 0.58, 18 KB 2.2 (`_SCOPE_END`).
# The cases below are 2.7 to 13 times those sizes. The fixed code takes milliseconds on them.
# ---------------------------------------------------------------------------

_XML_HOSTILE = {
    "unquoted_lt": b"<a " * 20_000,
    "name_run": b"<a " + b"b" * 60_000 + b">",
    "unclosed_comment": b"<!-- " * 25_000,
    "unclosed_cdata": b"<![CDATA[ " * 20_000,
    "unclosed_pi": b"<? " * 40_000,
    "unclosed_doctype": b"<!DOCTYPE " * 40_000,
    "mixed_quotes": b"<a \"'<a '\"<a \"'" * 8_000,
}
_XML_PARSERS = {
    "xml_cfg": lambda data: parse_xml_config("repo", "a.xml", data),
    "xsd": lambda data: parse_xsd("repo", "a.xsd", data),
    "xsl": lambda data: parse_xsl("repo", "a.xsl", data),
}


@pytest.mark.timeout(2)
@pytest.mark.parametrize("shape", sorted(_XML_HOSTILE))
@pytest.mark.parametrize("parser", sorted(_XML_PARSERS))
def test_xml_scanners_are_not_quadratic(parser, shape):
    pytest.importorskip("pytest_timeout")  # see the manifest test's comment above
    _XML_PARSERS[parser](_XML_HOSTILE[shape])


_SQL_HOSTILE = {
    "trigger_event_words": b"CREATE TRIGGER t1 AFTER " + b"AFTER " * 30_000,
    "trigger_before_words": b"CREATE TRIGGER t1 " + b"BEFORE x " * 20_000,
    "blank_lines_after_table": b"CREATE TABLE t (a int)\n" + b"\n" * 60_000,
    "blank_lines_after_trigger": b"CREATE TRIGGER t1 AFTER\n" + b"\n" * 60_000,
}


@pytest.mark.timeout(2)
@pytest.mark.parametrize("shape", sorted(_SQL_HOSTILE))
def test_sql_scanner_is_not_quadratic(shape):
    pytest.importorskip("pytest_timeout")
    parse_sql("repo", "t.sql", _SQL_HOSTILE[shape])
