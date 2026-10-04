"""P01: ``parse_source`` must not rebuild the set of every node id per member symbol.

The member pass (macros, typedefs, enum constants, file-scope variables) asked
``m_id in {n.id for n in nodes}`` once per member. That is quadratic: 8,000
``#define`` lines took 1.9 s and 16,000 took 8.0 s, so a generated header of a few
megabytes ran for many minutes. The id set is now built once and extended as nodes
are appended.

Two properties are pinned here:

1. The output did not change. ``EXPECTED_NODES`` and ``EXPECTED_EDGES`` were recorded
   from the code BEFORE the fix, in emitted order, on fixtures that repeat a member id
   inside the one member loop (a macro defined twice, a variable declared twice).
   Edges are part of the check on purpose. A later pass collapses repeated NODES by
   id, so a fix that forgets to add each new id to the set still returns the right
   nodes, and only the extra ``contains`` edges give it away.
2. Time grows about linearly with the number of members. The quadratic code ran 59x
   slower for 8x the members; linear code runs about 8x slower. The bound sits
   between the two.
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from contextlake.kb.parse import parse_source

C_HEADER = b"""\
#define VERSION 1
#define MAX_LEN 64
#define VERSION 2
#ifdef FEATURE_X
#define MODE 1
#else
#define MODE 2
#endif
typedef unsigned int u32;
typedef unsigned int u32;
enum color { RED, GREEN, BLUE };
struct point { int x; int y; };
extern int counter;
extern int counter;
static int hidden_a;
static int hidden_a;
"""

CPP_SOURCE = b"""\
namespace {
struct Hidden { int a; int b; };
int local_counter;
int local_counter;
}
namespace api {
struct Box { int w; int h; };
constexpr int kMax = 4;
constexpr int kMax = 5;
class Widget { public: int size; int size; void put(int v); };
}
void api::Widget::put(int v) { (void)v; }
"""

PY_SOURCE = b"""\
import os
LIMIT = 3
LIMIT = 4


class A:
    count = 1
    count = 2

    def f(self):
        return os.getcwd()
"""

FIXTURES = [
    ("c_header.h", C_HEADER, "c"),
    ("cpp_source.cpp", CPP_SOURCE, "cpp"),
    ("py_source.py", PY_SOURCE, "python"),
]

# (kind, name, line_start) per node, in emitted order, recorded before the fix.
EXPECTED_NODES: dict[str, list[tuple[str, str, int | None]]] = {
    "c_header.h": [
        ("file", "c_header.h", None),
        ("enum", "color", 11),
        ("struct", "point", 12),
        ("global_variable", "hidden_a", 16),
        ("global_variable", "counter", 14),
        ("field", "y", 12),
        ("field", "x", 12),
        ("enum_constant", "BLUE", 11),
        ("enum_constant", "GREEN", 11),
        ("enum_constant", "RED", 11),
        ("typedef", "u32", 10),
        ("macro", "MODE", 7),
        ("macro", "VERSION", 3),
        ("macro", "MAX_LEN", 2),
    ],
    "cpp_source.cpp": [
        ("file", "cpp_source.cpp", None),
        ("struct", "Hidden", 2),
        ("namespace", "api", 6),
        ("struct", "Box", 7),
        ("class", "Widget", 10),
        ("function", "put", 12),
        ("field", "size", 10),
        ("global_variable", "kMax", 9),
        ("field", "h", 7),
        ("field", "w", 7),
        ("global_variable", "local_counter", 4),
        ("field", "b", 2),
        ("field", "a", 2),
    ],
    "py_source.py": [
        ("file", "py_source.py", None),
        ("class", "A", 6),
        ("method", "f", 10),
        ("global_variable", "LIMIT", 2),
        ("field", "count", 7),
        ("module", "os", None),
    ],
}

# (relation, src name, dst name, source_line) per edge, in emitted order.
EXPECTED_EDGES: dict[str, list[tuple[str, str, str, int]]] = {
    "c_header.h": [
        ("contains", "c_header.h", "hidden_a", 16),
        ("contains", "c_header.h", "counter", 14),
        ("contains", "point", "y", 12),
        ("contains", "point", "x", 12),
        ("contains", "color", "BLUE", 11),
        ("contains", "color", "GREEN", 11),
        ("contains", "color", "RED", 11),
        ("contains", "c_header.h", "u32", 10),
        ("contains", "c_header.h", "MODE", 7),
        ("contains", "c_header.h", "VERSION", 3),
        ("contains", "c_header.h", "MAX_LEN", 2),
        ("contains", "c_header.h", "color", 11),
        ("contains", "c_header.h", "point", 12),
    ],
    "cpp_source.cpp": [
        ("contains", "Widget", "size", 10),
        ("contains", "api", "kMax", 9),
        ("contains", "Box", "h", 7),
        ("contains", "Box", "w", 7),
        ("contains", "cpp_source.cpp", "local_counter", 4),
        ("contains", "Hidden", "b", 2),
        ("contains", "Hidden", "a", 2),
        ("contains", "cpp_source.cpp", "Hidden", 2),
        ("contains", "cpp_source.cpp", "api", 6),
        ("contains", "api", "Box", 7),
        ("contains", "api", "Widget", 10),
        ("contains", "cpp_source.cpp", "put", 12),
    ],
    "py_source.py": [
        ("contains", "py_source.py", "LIMIT", 2),
        ("contains", "A", "count", 7),
        ("contains", "py_source.py", "A", 6),
        ("contains", "A", "f", 10),
        ("imports", "py_source.py", "os", 1),
    ],
}


def _parse(rel: str, source: bytes, lang: str):
    nodes, edges, _calls, _inherits = parse_source(
        "r", rel, source, lang, verified_at=date(2026, 1, 1))
    return nodes, edges


@pytest.mark.parametrize("rel,source,lang", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_member_pass_nodes_are_unchanged(rel, source, lang):
    nodes, _edges = _parse(rel, source, lang)
    assert [(n.kind, n.name, n.line_start) for n in nodes] == EXPECTED_NODES[rel]


@pytest.mark.parametrize("rel,source,lang", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_member_pass_edges_are_unchanged(rel, source, lang):
    nodes, edges = _parse(rel, source, lang)
    name = {n.id: n.name for n in nodes}
    got = [(e.relation, name[e.src], name[e.dst], e.provenance.source_line)
           for e in edges]
    assert got == EXPECTED_EDGES[rel]


def _define_header(count: int) -> bytes:
    return ("\n".join(f"#define MACRO_{i} {i}" for i in range(count)) + "\n").encode()


def _best_of(count: int, runs: int = 2) -> float:
    source = _define_header(count)
    best = float("inf")
    for _ in range(runs):
        t0 = time.perf_counter()
        parse_source("r", "gen.h", source, "c")
        best = min(best, time.perf_counter() - t0)
    return best


def test_member_pass_time_grows_about_linearly():
    small, large = 2000, 16000
    t_small = _best_of(small)
    t_large = _best_of(large)
    ratio = t_large / t_small
    # 8x the members. Linear code lands near 8x (measured 6 to 7); the quadratic code
    # measured 59x (0.13 s for 2,000 defines, 7.8 s for 16,000).
    assert ratio < 24, (
        f"{small} defines took {t_small:.3f}s, {large} took {t_large:.3f}s "
        f"(ratio {ratio:.1f}, linear is about 8, quadratic about 64)")
