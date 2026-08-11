"""Test-macro invocations must carry the case's name, not the macro's (G16).

A macro with a body parses as a `function_definition` whose name is the MACRO, so
every googletest case in a repo collapsed onto one node called `TEST` or `TEST_F`
and no test was findable by its own name. Measured before this fix on a large
legacy C++ tree: TEST_F 1,855, TEST 962, TEST_P 3 = 2,820 nodes, 6.8% of all
function and method nodes.
"""

import pytest

from contextlake.kb.parse import parse_source


def _defs(src: bytes):
    nodes, *_ = parse_source("r", "t.cpp", src, "cpp")
    return {n.name: n for n in nodes if n.kind != "file"}


@pytest.mark.parametrize("macro", ["TEST", "TEST_F", "TEST_P", "TYPED_TEST", "TYPED_TEST_P"])
def test_case_name_replaces_the_macro_name(macro):
    got = _defs(b"%s(SuiteName, TheCase) { }" % macro.encode())
    assert "TheCase" in got, f"{macro} did not yield the case name"
    assert macro not in got, f"{macro} still leaked as a node name"
    assert got["TheCase"].kind == "test"


def test_suite_becomes_the_qualifier():
    n = _defs(b"TEST(TimerSuite, HandlesMinutes) { }")["HandlesMinutes"]
    assert n.qualified_name.endswith("TimerSuite.HandlesMinutes")


def test_two_cases_in_one_suite_stay_distinct():
    got = _defs(b"TEST(S, First) { }\nTEST(S, Second) { }")
    assert {"First", "Second"} <= set(got)


def test_same_case_name_in_two_suites_stays_distinct():
    """Read the node LIST, not a dict keyed by name: two cases legitimately share the
    name `Run`, so keying by name hides exactly the collapse this asserts against."""
    nodes, *_ = parse_source(
        "r", "t.cpp", b"TEST(SuiteA, Run) { }\nTEST(SuiteB, Run) { }", "cpp")
    tests = [n for n in nodes if n.kind == "test"]
    assert len(tests) == 2
    assert {n.qualified_name for n in tests} == {"SuiteA.Run", "SuiteB.Run"}
    assert len({n.id for n in tests}) == 2


class TestThingsThatMustNotBeTouched:
    """Absence of a return type cannot be the discriminator on its own: a
    constructor and a destructor have none either."""

    def test_a_real_function_is_unchanged(self):
        assert _defs(b"void real(int x) { }")["real"].kind == "function"

    def test_a_constructor_is_not_mangled(self):
        got = _defs(b"class C { public: C(); };\nC::C() { }")
        assert "C" in got and all(n.kind != "test" for n in got.values())

    def test_a_destructor_is_not_mangled(self):
        got = _defs(b"class C { public: ~C(); };\nC::~C() { }")
        assert all(n.kind != "test" for n in got.values())

    def test_a_template_overload_set_is_unchanged(self):
        got = _defs(b"template<class T> void ForEachChild(T& r) { }")
        assert got["ForEachChild"].kind == "function"

    def test_an_unlisted_all_caps_macro_is_left_alone(self):
        """The macro list is closed on purpose; guessing from ALL_CAPS alone would
        mangle real symbols."""
        got = _defs(b"BEGIN_MESSAGE_MAP(A, B) { }")
        assert all(n.kind != "test" for n in got.values())
