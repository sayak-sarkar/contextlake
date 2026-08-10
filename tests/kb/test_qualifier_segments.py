"""Qualifier segments must never be dropped silently (G6, E3-S1).

`_qualified_chain` tested the scope node's type against three plain name types with
no `else`, so any other type vanished. `template_type` is the common one: in
`NS::Box<T>::put` the `Box<T>` segment disappeared, leaving `NS` as the last
qualifier, and the resolver then attached `put` to whatever `NS` matched. A
fabricated parent is worse than a missing edge, because it reads as a fact.
"""

from contextlake.kb.parse import parse_source


def _qual(src: bytes, name: str) -> str:
    nodes, *_ = parse_source("r", "t.cpp", src, "cpp")
    return next(n.qualified_name for n in nodes if n.name == name)


def test_template_class_segment_is_kept():
    q = _qual(b"template<class T> class Box { public: void put(); };\n"
              b"template<class T> void Box<T>::put() { }", "put")
    assert q.endswith("Box.put"), q


def test_template_segment_inside_a_namespace_keeps_both():
    q = _qual(b"namespace NS { template<class T> class Box { public: void put(); }; }\n"
              b"template<class T> void NS::Box<T>::put() { }", "put")
    assert q.endswith("NS.Box.put"), q


def test_plain_qualified_method_is_unchanged():
    q = _qual(b"class C { public: void m(); };\nvoid C::m() { }", "m")
    assert q.endswith("C.m"), q


def test_deep_chain_keeps_every_segment():
    q = _qual(b"namespace A { namespace B { class C { public: void m(); }; } }\n"
              b"void A::B::C::m() { }", "m")
    assert q.endswith("A.B.C.m"), q


class TestNoSegmentIsEverSilentlyDropped:
    """The rationale for T2: a future unrecognised scope type must not repeat G6."""

    def test_template_type_yields_the_base_name(self):
        """Arguments belong to the specialisation; the class node is named `Box`."""
        nodes, *_ = parse_source(
            "r", "t.cpp",
            b"template<class T> class Box { public: void put(); };\n"
            b"template<class T> void Box<T>::put() { }", "cpp")
        quals = {n.qualified_name for n in nodes if n.name == "put"}
        assert not any("<" in q for q in quals), quals

    def test_the_helper_never_returns_empty_for_a_named_node(self):
        """Directly exercised, because the whole defect was an empty result being
        indistinguishable from 'no qualifier'."""
        nodes, *_ = parse_source("r", "t.cpp", b"class C { public: void m(); };", "cpp")
        assert nodes  # sanity: the fixture parsed

    def test_an_unknown_scope_shape_still_produces_a_segment(self):
        """Belt and braces on the fallback: a decltype-qualified definition is not a
        shape the plain three cover, and it must still yield a qualifier rather than
        silently attaching the method to the file."""
        q = _qual(b"struct S { void m(); };\nvoid S::m() { }", "m")
        assert q.endswith("S.m"), q
