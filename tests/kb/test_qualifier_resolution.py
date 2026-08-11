"""Out-of-line methods resolve on the WHOLE qualifier (G7, G8, E3-S2).

`_resolve_pending_methods` keyed on the qualifier's LAST segment and bailed whenever
that bare name matched more than one class. Two consequences: `NS::Box::put` could
attach to an unrelated `Other::Box` (a fabricated parent, which reads as a fact
rather than as a gap), and a tie the qualifier already settles was thrown away.

These go through `index_repo_dir`, not `parse_source`: the resolver is a repo-wide
second pass, so calling the per-file parser directly never runs it at all.
"""

import pathlib

from contextlake.kb.parse import index_repo_dir


def _methods(tmp_path: pathlib.Path, files: dict[str, bytes]):
    """{method name: parent's scope chain} for every resolved method."""
    for fn, body in files.items():
        (tmp_path / fn).write_bytes(body)
    shard = index_repo_dir(str(tmp_path), "r")
    byid = {n.id: n for n in shard.nodes}
    out = {}
    for e in shard.edges:
        if e.relation != "contains":
            continue
        dst, src = byid.get(e.dst), byid.get(e.src)
        if dst is None or dst.kind != "method":
            continue
        chain = (src.qualified_name or "") if src else ""
        out[dst.name] = chain.split("::", 1)[-1]
    return out


def test_a_plain_qualified_method_still_resolves(tmp_path):
    got = _methods(tmp_path, {
        "a.h": b"class C { public: void m(); };",
        "a.cpp": b'#include "a.h"\nvoid C::m() { }'})
    assert got == {"m": "C"}


def test_a_template_class_method_resolves(tmp_path):
    """Needs E3-S1's segment fix and this one together: the `Box<T>` segment had to
    survive before the resolver could match on it."""
    got = _methods(tmp_path, {
        "b.h": b"namespace NS { template<class T> class Box { public: void put(); }; }",
        "b.cpp": b'#include "b.h"\ntemplate<class T> void NS::Box<T>::put() { }'})
    assert got == {"put": "NS.Box"}


def test_a_relative_qualifier_inside_a_namespace_resolves(tmp_path):
    """Inside `namespace NS`, `void Box::put()` carries pending ["Box"] while the class
    chain is "NS.Box". The suffix match has to accept that."""
    got = _methods(tmp_path, {
        "c.h": b"namespace NS { class Box { public: void put(); }; }",
        "c.cpp": b'#include "c.h"\nnamespace NS { void Box::put() { } }'})
    assert got.get("put") == "NS.Box"


def test_a_bare_name_tie_the_qualifier_resolves_is_resolved(tmp_path):
    """G8: two classes named `Dup`, and each method must land in its own namespace."""
    got = _methods(tmp_path, {
        "d.h": b"namespace A { class Dup { public: void m(); }; }\n"
               b"namespace B { class Dup { public: void n(); }; }",
        "d.cpp": b'#include "d.h"\nvoid A::Dup::m() { }\nvoid B::Dup::n() { }'})
    assert got == {"m": "A.Dup", "n": "B.Dup"}


class TestNoMethodGetsAParentItsQualifierExcludes:
    """E3-S2-T2. Nothing in the suite previously forbade a fabricated parent, which is
    why the defect survived: the graph looked richer, not wronger."""

    def test_the_qualifier_picks_the_right_class_of_two_same_named(self, tmp_path):
        got = _methods(tmp_path, {
            "e.h": b"namespace NS { class Box { public: void put(); }; }\n"
                   b"namespace Other { class Box { public: void other(); }; }",
            "e.cpp": b'#include "e.h"\nvoid NS::Box::put() { }'})
        assert got.get("put") == "NS.Box"
        assert got.get("put") != "Other.Box"

    def test_a_qualifier_naming_no_known_class_attaches_nothing(self, tmp_path):
        """Better file-contained than wrongly parented."""
        got = _methods(tmp_path, {
            "f.h": b"namespace NS { class Box { public: void put(); }; }",
            "f.cpp": b'#include "f.h"\nvoid Nonexistent::Box::put() { }'})
        assert got == {}

    def test_a_genuinely_ambiguous_qualifier_attaches_nothing(self, tmp_path):
        """A bare qualifier that suffix-matches two DIFFERENT scopes is ambiguous, and
        neither class may claim the method.

        This replaces an earlier version of this test that used the same full chain
        (`S::T`) declared in two files. That is no longer ambiguous, and correctly so:
        node identity is now file-independent for C/C++ external-linkage symbols, so two
        headers naming `S::T` produce ONE class node. In well-formed C++ `S::T` names
        exactly one class; two headers defining it differently is an ODR violation and
        ill-formed, so modelling them as one symbol is the accurate choice rather than a
        weakening. The shape below is what genuine ambiguity actually looks like.
        """
        got = _methods(tmp_path, {
            "a.h": b"namespace A { class T { public: void m(); }; }",
            "b.h": b"namespace B { class T { public: void m(); }; }",
            "c.cpp": b'#include "a.h"\n#include "b.h"\nvoid T::m() { }'})
        assert got == {}
