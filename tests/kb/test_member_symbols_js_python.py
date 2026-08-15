"""Module-level bindings and class fields, for JavaScript and Python.

`_member_symbols` emits five symbol kinds the tree-sitter definition query cannot express,
and it was C/C++ only, by an explicit `if lang not in ("c", "cpp"): return []`. That was a
deliberate scope, not an oversight, and the head-to-head benchmark is what made the cost of
it visible: on a small public JavaScript tree the comparator emitted 461 `variable` nodes
where contextlake emitted none, which was most of a 764-node gap on a repo where both tools
read the same 141 files.

**Every pattern here was read off the grammar before it was written, not recalled.** Two of
them are not what you would guess:

- A Python module-level assignment is NOT an `assignment` child of the module. It is an
  `expression_statement` WRAPPING an `assignment`, and the same holds inside a class body.
- `export const NAME = 1` is an `export_statement` WRAPPING a `lexical_declaration`, so the
  export keyword has to be stepped through or every exported binding is missed.

The negative assertions carry as much weight as the positive ones. A local inside a function
is not a module-level binding, and emitting one would put a name in the graph that no other
file can refer to.
"""

from __future__ import annotations

import pytest

from contextlake.kb.parse import parse_source

JS = b"""const router = require('express').Router();
var LEGACY = 1;
let counter = 0;
export const EXPORTED = "x";
const {destructured_a, destructured_b} = obj;

class Box {
  kind = "box";
  static VERSION = "1";
  #priv = 2;
  area(w) { const local_to_method = 1; return local_to_method; }
}

function helper() { const local_to_function = 1; return local_to_function; }
"""

PY = b'''import os

MAX_RETRIES = 5
_registry: dict = {}
tuple_a, tuple_b = 1, 2


class Engine:
    VERSION = "1"
    typed_attr: int = 3

    def start(self):
        local_to_method = 1
        return local_to_method


def helper():
    local_to_function = 1
    return local_to_function
'''


def _kinds(lang, src, path):
    nodes, _e, _c, _i = parse_source("r", path, src, lang)
    return {(n.kind, n.name): n for n in nodes}


@pytest.fixture(scope="module")
def js():
    return _kinds("javascript", JS, "a.js")


@pytest.fixture(scope="module")
def py():
    return _kinds("python", PY, "a.py")


# --- what must be emitted ----------------------------------------------------

@pytest.mark.parametrize("name", ["router", "LEGACY", "counter", "EXPORTED"])
def test_js_module_bindings_become_nodes(js, name):
    assert ("global_variable", name) in js, (
        f"{name} is a module-level binding and produced no node; "
        f"got {sorted(n for k, n in js if k == 'global_variable')}")


def test_js_export_wrapper_is_stepped_through(js):
    """`export const` is the one that a from-memory pattern misses: the binding sits one
    level down inside an `export_statement`."""
    assert ("global_variable", "EXPORTED") in js


@pytest.mark.parametrize("name", ["kind", "VERSION", "#priv"])
def test_js_class_fields_become_nodes(js, name):
    assert ("field", name) in js, f"class field {name} produced no node"


def test_js_fields_are_qualified_by_their_class(js):
    assert js[("field", "VERSION")].qualified_name.endswith("Box.VERSION"), (
        f"field lost its class qualifier: {js[('field', 'VERSION')].qualified_name}")


@pytest.mark.parametrize("name", ["MAX_RETRIES", "_registry"])
def test_python_module_assignments_become_nodes(py, name):
    assert ("global_variable", name) in py, (
        f"{name} is a module-level assignment and produced no node; "
        f"got {sorted(n for k, n in py if k == 'global_variable')}")


@pytest.mark.parametrize("name", ["VERSION", "typed_attr"])
def test_python_class_attributes_become_nodes(py, name):
    """Includes the annotated form, which is the same `expression_statement` wrapper."""
    assert ("field", name) in py


def test_python_fields_are_qualified_by_their_class(py):
    assert py[("field", "VERSION")].qualified_name.endswith("Engine.VERSION")


# --- what must NOT be emitted ------------------------------------------------

@pytest.mark.parametrize("name", ["local_to_function", "local_to_method"])
def test_locals_are_not_module_bindings(js, py, name):
    """A local is not a symbol another file can refer to. Emitting it would inflate the
    graph with names that answer no cross-file question, which is the whole reason this
    descends into module scope and class bodies rather than walking every node."""
    for store, lang in ((js, "javascript"), (py, "python")):
        assert ("global_variable", name) not in store, f"{lang}: local {name} escaped"
        assert ("field", name) not in store, f"{lang}: local {name} became a field"


@pytest.mark.parametrize("name", ["destructured_a", "destructured_b"])
def test_js_destructuring_patterns_are_skipped(js, name):
    """A destructuring pattern binds several names at once. Naming a node after the whole
    pattern would invent a symbol nobody wrote, so these are deliberately not emitted;
    when that changes, it should be because each bound name is emitted separately."""
    assert ("global_variable", name) not in js


@pytest.mark.parametrize("name", ["tuple_a", "tuple_b"])
def test_python_tuple_targets_are_skipped(py, name):
    assert ("global_variable", name) not in py


# --- the boundary that used to be the whole behaviour ------------------------

def test_c_family_still_emits_its_own_five_kinds():
    """The C/C++ path is unchanged by the new branches. It carried this alone before, and
    a refactor that quietly dropped it would look like a JavaScript success."""
    src = b"""#define MAX 10
typedef int Handle;
enum Color { RED, GREEN };
static int file_scoped = 1;
struct S { int member_field; };
"""
    got = _kinds("cpp", src, "a.cpp")
    for kind, name in (("macro", "MAX"), ("typedef", "Handle"),
                       ("enum_constant", "RED"), ("global_variable", "file_scoped"),
                       ("field", "member_field")):
        assert (kind, name) in got, f"the C/C++ path lost {kind} {name}"
