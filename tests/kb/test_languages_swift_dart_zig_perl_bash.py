"""Five languages added together: Swift, Dart, Zig, Perl and Bash.

Each query here was compiled and run against a real snippet before it was written into
`parse.py`, because every one of these grammars names things differently from what the
language's syntax suggests, and three of them differently from what any other grammar in
this project does:

- **Swift has no struct node.** `struct Box` parses as `class_declaration`, the same as a
  class. Both therefore arrive as `class` rather than being told apart by reading source
  text back out.
- **Dart splits a top-level function in two.** `bool helper(int n) => n > 0;` is a
  `function_signature` followed by a SIBLING `function_body`, so the signature is the
  definition node.
- **Zig declares a struct as a constant.** `const Engine = struct {...}` is a
  `variable_declaration`, not a definition node, so only functions are captured. Reading the
  initializer to recover struct names is deliberately left undone rather than half-done, and
  the test below pins the current behaviour so the gap is visible instead of assumed closed.

The negative assertions matter as much as the positive ones: a query that captures too much
is how a graph fills with names nobody wrote.
"""

from __future__ import annotations

import pytest

from contextlake.kb.parse import parse_source

SWIFT = b"""import Foundation

class Engine {
    var count = 0
    func start(n: Int) -> Bool { return helper(n) }
}

struct Box { let size: Int }

protocol Drawable { func draw() }

func helper(_ n: Int) -> Bool { return n > 0 }
"""

DART = b"""class Engine {
  int count = 0;
  bool start(int n) => helper(n);
}

mixin Drawable { void draw() {} }

bool helper(int n) => n > 0;
"""

ZIG = b"""const std = @import("std");

pub fn helper(n: u32) bool { return n > 0; }
pub fn start(n: u32) bool { return helper(n); }
"""

PERL = b"""package Engine;
use strict;

sub start { my ($n) = @_; return helper($n); }
sub helper { return $_[0] > 0; }
1;
"""

BASH = b"""#!/usr/bin/env bash
MAX_RETRIES=5

start() { helper "$1"; }
function helper { return 0; }
"""

CASES = {
    "swift": ("a.swift", SWIFT),
    "dart": ("a.dart", DART),
    "zig": ("a.zig", ZIG),
    "perl": ("a.pl", PERL),
    "bash": ("a.sh", BASH),
}


def _syms(lang):
    path, src = CASES[lang]
    nodes, _e, _c, _i = parse_source("r", path, src, lang)
    return {(n.kind, n.name) for n in nodes if n.kind != "file"}


@pytest.mark.parametrize("lang", sorted(CASES))
def test_each_language_extracts_something(lang):
    """The floor. A grammar wired up with a query that matches nothing produces a file node
    and silence, which reads exactly like a language nobody used."""
    got = _syms(lang)
    assert got, f"{lang} produced no symbols at all from a file that defines several"


def test_swift_class_struct_protocol_and_function():
    got = _syms("swift")
    assert ("class", "Engine") in got
    # `struct` is the same grammar node as `class`; documented, not accidental.
    assert ("class", "Box") in got
    assert ("interface", "Drawable") in got
    assert ("function", "helper") in got
    # A function inside a class is promoted by the shared containment rule, not by anything
    # Swift-specific, which is why it is worth asserting here.
    assert ("method", "start") in got


def test_dart_class_mixin_and_split_function_signature():
    got = _syms("dart")
    assert ("class", "Engine") in got
    assert ("interface", "Drawable") in got
    assert ("function", "helper") in got, (
        "the top-level function was missed; its definition node is `function_signature`, "
        "and the body is a sibling rather than a child")
    assert ("method", "start") in got


def test_zig_functions_are_captured():
    got = _syms("zig")
    assert ("function", "helper") in got
    assert ("function", "start") in got


def test_zig_struct_is_a_known_gap_not_a_silent_one():
    """`const Engine = struct {...}` is a variable_declaration, so no struct node exists.

    Pinned deliberately. If someone later teaches the extractor to read the initializer,
    this test fails and points at the docs claim that has to change with it, instead of the
    gap being quietly closed or quietly forgotten."""
    got = _syms("zig")
    assert not any(k in ("class", "struct") for k, _ in got), (
        f"Zig now extracts a type; update the language-depth table in the docs: {got}")


def test_perl_package_and_subs():
    got = _syms("perl")
    assert ("module", "Engine") in got
    assert ("function", "start") in got
    assert ("function", "helper") in got


def test_bash_functions_both_spellings_and_variables():
    got = _syms("bash")
    # `f() {}` and `function f {}` are the same grammar node; both must land.
    assert ("function", "start") in got
    assert ("function", "helper") in got
    assert ("global_variable", "MAX_RETRIES") in got, (
        "a bash variable is global unless declared `local`, so an assignment anywhere is a "
        "global; this is NOT the module-scope-only rule JavaScript and Python follow")


def test_an_import_target_is_a_module_not_a_definition():
    """`import Foundation` becomes a `module` node, the same as Python's `import os`.

    Written the other way round first, asserting Foundation was not a node at all, and that
    was wrong about the design rather than finding a defect: an import target has been a
    dependency node in every language this project supports. Kept as the positive form,
    which pins the contract instead of a guess about it."""
    got = _syms("swift")
    assert ("module", "Foundation") in got
    assert ("class", "Foundation") not in got
    assert ("function", "Foundation") not in got


@pytest.mark.parametrize("lang,absent", [
    ("perl", "strict"),   # `use strict` is a pragma, and perl's query captures no imports
    ("bash", "1"),        # a literal is not a name
])
def test_non_definitions_do_not_become_symbols(lang, absent):
    assert absent not in {n for _k, n in _syms(lang)}, (
        f"{lang}: {absent!r} is not a definition and should not be a node")
