"""Tests for tree-sitter code parsing."""

from datetime import date

from contextlake.kb.model import Confidence
from contextlake.kb.parse import (
    _has_generated_header,
    _is_generated_name,
    index_repo_dir,
    load_ignore_patterns,
    match_ignore,
    parse_source,
)

PY = b"""import os
from a.b import c


class Foo:
    def bar(self):
        pass


def top():
    pass
"""


def test_captures_python_docstring_and_signature():
    src = (b'def charge(amount, currency="USD"):\n'
           b'    """Charge a card and return a receipt."""\n'
           b'    return 1\n\n\n'
           b'class Order:\n'
           b'    """An order aggregate."""\n'
           b'    pass\n')
    nodes, _edges, _, _ = parse_source("r", "pay.py", src, "python", verified_at=date(2026, 6, 21))
    by_name = {n.name: n for n in nodes}
    assert by_name["charge"].attrs.get("doc") == "Charge a card and return a receipt."
    assert "amount" in by_name["charge"].attrs.get("signature", "")
    assert by_name["Order"].attrs.get("doc") == "An order aggregate."   # class docstring too


def test_signature_captured_across_languages():
    js = b"function charge(amount, currency) {\n  return 1;\n}\n"
    nodes, _e, _, _ = parse_source("r", "pay.js", js, "javascript", verified_at=date(2026, 6, 21))
    by_name = {n.name: n for n in nodes}
    assert "amount" in by_name["charge"].attrs.get("signature", "")   # JS, not just Python


def test_doc_comment_captured_for_js_and_csharp():
    js = b"/**\n * Charge a card.\n */\nfunction charge(amount) { return 1; }\n"
    jn = {n.name: n for n in parse_source("r", "p.js", js, "javascript",
                                          verified_at=date(2026, 6, 21))[0]}
    assert jn["charge"].attrs.get("doc") == "Charge a card."           # JSDoc block
    cs = (b"class P {\n  /// <summary>Charges a card.</summary>\n"
          b"  public int Charge(int a) { return 1; }\n}\n")
    cn = {n.name: n for n in parse_source("r", "P.cs", cs, "csharp",
                                          verified_at=date(2026, 6, 21))[0]}
    assert cn["Charge"].attrs.get("doc") == "Charges a card."          # /// XML, tags stripped


def test_parse_extracts_defs_and_imports():
    nodes, edges, _, _ = parse_source(
        "team/api", "svc.py", PY, "python", verified_at=date(2026, 6, 21)
    )
    by_kind: dict[str, list[str]] = {}
    for n in nodes:
        by_kind.setdefault(n.kind, []).append(n.name)

    assert by_kind["file"] == ["svc.py"]
    assert "Foo" in by_kind["class"]
    assert "bar" in by_kind.get("method", [])  # method (inside a class)
    assert "top" in by_kind["function"]
    assert set(by_kind["module"]) == {"os", "a.b"}
    # module nodes aren't owned by the repo that happened to import them (Finding
    # #10) -- the same "os" node would collide with every other repo's "os" import
    module_repos = {n.repo for n in nodes if n.kind == "module"}
    assert module_repos == {"(shared)"}

    ids = {n.name: n.id for n in nodes}
    contains = {(e.src, e.dst) for e in edges if e.relation == "contains"}
    assert (ids["Foo"], ids["bar"]) in contains  # class -> method
    assert (ids["svc.py"], ids["Foo"]) in contains  # file -> class
    assert (ids["svc.py"], ids["top"]) in contains  # file -> function

    imports = {(e.src, e.dst) for e in edges if e.relation == "imports"}
    assert (ids["svc.py"], ids["os"]) in imports

    assert all(e.confidence is Confidence.EXTRACTED for e in edges)
    assert all(e.provenance.source_file == "svc.py" for e in edges)


def test_qualified_names_disambiguate_methods():
    nodes, _, _, _ = parse_source("r", "m.py", PY, "python")
    bar = next(n for n in nodes if n.name == "bar")
    assert bar.qualified_name == "m.py::Foo.bar"
    assert bar.line_start == 6


def test_index_repo_dir_walks_and_skips(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("class A:\n    def m(self):\n        pass\n")
    (tmp_path / "pkg" / "b.py").write_text("import os\ndef f():\n    pass\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.py").write_text("def ignored():\n    pass\n")
    (tmp_path / "readme.md").write_text("# not code\n")

    shard = index_repo_dir(str(tmp_path), "demo/app")
    names = {n.name for n in shard.nodes}
    assert {"A", "m", "f"} <= names
    assert "ignored" not in names  # .git skipped
    files = {n.name for n in shard.nodes if n.kind == "file"}
    assert files == {"pkg/a.py", "pkg/b.py"}  # markdown ignored, .git skipped
    assert shard.repo == "demo/app"


def test_parse_error_does_not_abort_directory(tmp_path):
    (tmp_path / "good.py").write_text("def ok():\n    pass\n")
    (tmp_path / "weird.py").write_bytes(b"\xff\xfe not utf8 def x(:\n")  # tolerated
    shard = index_repo_dir(str(tmp_path), "r")
    assert any(n.name == "ok" for n in shard.nodes)  # good file still indexed


def _kinds(nodes):
    out: dict[str, set] = {}
    for n in nodes:
        out.setdefault(n.kind, set()).add(n.name)
    return out


def test_parse_javascript():
    src = b"import {a} from 'm';\nexport class Foo { bar() { } }\nfunction top() {}\n"
    k = _kinds(parse_source("r", "f.js", src, "javascript")[0])
    assert k["class"] == {"Foo"} and "bar" in k["method"] and "top" in k["function"]
    assert "m" in k["module"]  # import quotes stripped


def test_parse_typescript():
    src = b"import x from 'm';\nclass C {}\ninterface I {}\nenum E { A }\nfunction f(): void {}\n"
    k = _kinds(parse_source("r", "f.ts", src, "typescript")[0])
    assert k["class"] == {"C"} and k["interface"] == {"I"} and k["enum"] == {"E"}
    assert "f" in k["function"]


def test_parse_csharp():
    src = (b"using System;\nusing System.Collections;\n"
           b"namespace N { class Foo { void Bar() {} } interface IT {} }\n")
    k = _kinds(parse_source("r", "f.cs", src, "csharp")[0])
    assert "Foo" in k["class"] and "IT" in k["interface"] and "Bar" in k["method"]
    assert {"System", "System.Collections"} <= k["module"]


def test_parse_go():
    src = (b'package main\nimport "net/http"\n'
           b'type Server struct { Addr string }\ntype Handler interface { Serve() }\n'
           b'func New(a string) *Server { return &Server{Addr: a} }\n'
           b'func (s *Server) Start() error { return http.ListenAndServe(s.Addr, nil) }\n')
    nodes, _e, calls, _i = parse_source("r", "f.go", src, "go")
    k = _kinds(nodes)
    assert "Server" in k["struct"] and "Handler" in k["struct"]   # Go types index as struct-kind
    assert "New" in k["function"] and "Start" in k["method"]
    assert "net/http" in k["module"]
    assert "ListenAndServe" in {c[1] for c in calls}


def test_parse_java():
    src = (b"package com.acme;\nimport java.util.List;\n"
           b"public class CatalogService extends BaseService implements Auditable {\n"
           b"  public CatalogService() {}\n"
           b"  public Order get(String id) { return repo.find(id); }\n}\n"
           b"interface Auditable { void audit(); }\nenum Status { OPEN, CLOSED }\n")
    nodes, _e, calls, _i = parse_source("r", "f.java", src, "java")
    k = _kinds(nodes)
    assert "CatalogService" in k["class"] and "Auditable" in k["interface"]
    assert "Status" in k["enum"]
    assert "get" in k["method"] and "CatalogService" in k["method"]  # constructor -> method
    assert "java.util.List" in k["module"]
    assert "find" in {c[1] for c in calls}


def test_parse_c():
    src = (b'#include <stdio.h>\n#include "mylib.h"\n'
           b"struct Point { int x; };\nenum Color { RED };\n"
           b"int add(int a,int b){ return a+b; }\n"
           b'void run(void){ printf("%d", add(1,2)); }\n')
    nodes, _e, calls, _i = parse_source("r", "f.c", src, "c")
    k = _kinds(nodes)
    assert "Point" in k["struct"] and "Color" in k["enum"]
    assert "add" in k["function"] and "run" in k["function"]
    assert {"<stdio.h>", "mylib.h"} <= k["module"]   # quotes stripped like other langs
    # call attribution reaches the enclosing function (not just the file)
    named = {n.id: n.name for n in nodes}
    assert ("run", "add") in {(named.get(c[0]), c[1]) for c in calls}


def test_parse_cpp():
    src = (b"#include <string>\n"
           b"class Animal { public: void speak(); };\n"
           b"class Dog : public Animal { public: void speak(){} };\n"
           b"int compute(int x){ return x*2; }\n")
    nodes, _e, _c, inh = parse_source("r", "f.cpp", src, "cpp")
    k = _kinds(nodes)
    assert "Animal" in k["class"] and "Dog" in k["class"]
    assert "compute" in k["function"] and "speak" in k["method"]   # in-class method
    assert "Animal" in {base for _sub, base, _f, _ln in inh}       # Dog : public Animal


def test_parse_cpp_nested_def_under_unnamed_container_falls_back_to_file():
    """A def nested directly under an unnamed struct/union/enum -- common in real
    C++ under anonymous unions -- must not crash the whole file. The anonymous
    container matches a def-type structurally but is never captured (the query
    requires a `name:` field), so it's never registered in def_node_to_id; the
    containment edge must fall back to the file, not silently produce
    Edge(src=None) and abort every node/edge this file would have contributed."""
    src = (b"struct Outer {\n"
           b"    struct {\n"
           b"        void inner() {}\n"
           b"    } anon;\n"
           b"};\n")
    nodes, edges, _calls, _inh = parse_source("r", "f.cpp", src, "cpp")
    k = _kinds(nodes)
    assert "Outer" in k["struct"] and "inner" in k["function"]
    inner_id = next(n.id for n in nodes if n.name == "inner")
    file_id = next(n.id for n in nodes if n.kind == "file")
    assert (file_id, inner_id, "contains") in {(e.src, e.dst, e.relation) for e in edges}


def test_parse_cpp_out_of_line_method_two_segments_links_to_class():
    # parse_source's own (pre-resolution) contract: the pending marker is set and
    # the node is still "function"/file-contained. The repo-wide resolution that
    # turns this into a "method" contained by its class is index_repo_dir's job
    # (it needs to see every file first) -- see
    # test_index_repo_dir_resolves_out_of_line_method_across_files below.
    src = (b"class Widget {\npublic:\n    void Draw();\n};\n\n"
           b"void Widget::Draw() {\n    Render();\n}\n")
    nodes, _edges, calls, _inh = parse_source("r", "f.cpp", src, "cpp")
    draw = next(n for n in nodes if n.name == "Draw" and n.line_start == 6)
    assert draw.qualified_name == "f.cpp::Widget.Draw"
    assert draw.attrs["_pending_method_of"] == ["Widget"]
    assert ("Render" in {c[1] for c in calls})


def test_parse_cpp_out_of_line_method_three_segments_not_lost():
    # Same pre-resolution contract as above, for a 3-segment qualified name.
    src = (b"namespace App {\nclass Gadget {\npublic:\n    void Spin();\n};\n}\n\n"
           b"void App::Gadget::Spin() {\n    Tick();\n}\n")
    nodes, _edges, calls, _inh = parse_source("r", "f.cpp", src, "cpp")
    spin = next((n for n in nodes if n.name == "Spin"), None)
    assert spin is not None, "a 3-segment qualified definition must not vanish"
    assert spin.qualified_name == "f.cpp::App.Gadget.Spin"
    assert spin.attrs["_pending_method_of"] == ["App", "Gadget"]
    assert "Tick" in {c[1] for c in calls}


def test_parse_cpp_namespace_block_contains_its_members():
    src = b"namespace App {\nclass Gadget {\npublic:\n    void Spin() {}\n};\n}\n"
    nodes, edges, _c, _i = parse_source("r", "f.cpp", src, "cpp")
    k = _kinds(nodes)
    assert "App" in k.get("namespace", set())
    ns = next(n for n in nodes if n.name == "App")
    gadget = next(n for n in nodes if n.name == "Gadget")
    assert (ns.id, gadget.id, "contains") in {
        (e.src, e.dst, e.relation) for e in edges}


def test_parse_cpp_ifdef_else_twins_collapse_to_one_node():
    src = (b"#ifdef USE_FAST\n"
           b"void Setup() { FastInit(); }\n"
           b"#else\n"
           b"void Setup() { SlowInit(); }\n"
           b"#endif\n"
           b"void Run() { Setup(); }\n")
    nodes, _edges, calls, _inh = parse_source("r", "f.cpp", src, "cpp")
    setups = [n for n in nodes if n.name == "Setup"]
    assert len(setups) == 1
    # The surviving node absorbs outgoing calls from BOTH merged branch bodies --
    # there's no way to know which branch really compiles, so both FastInit (the
    # #ifdef body) and SlowInit (the #else body) attribute to the one kept Setup.
    setup_calls = {c[1] for c in calls if c[0] == setups[0].id}
    assert setup_calls == {"FastInit", "SlowInit"}


def test_parse_cpp_ifdef_else_twins_calls_resolve_inferred(tmp_path):
    (tmp_path / "f.cpp").write_text(
        "#ifdef USE_FAST\n"
        "void Setup() { FastInit(); }\n"
        "#else\n"
        "void Setup() { SlowInit(); }\n"
        "#endif\n"
        "void Run() { Setup(); }\n"
        "void FastInit() {}\n"
        "void SlowInit() {}\n"
    )
    shard = index_repo_dir(str(tmp_path), "demo/twins")
    calls_to_setup = [e for e in shard.edges if e.relation == "calls"
                      and any(n.id == e.dst and n.name == "Setup" for n in shard.nodes)]
    assert len(calls_to_setup) == 1
    assert calls_to_setup[0].confidence.value == "INFERRED"


def test_parse_cpp_ifdef_overloads_are_not_conflated_as_twins():
    # A genuine overload set inside ONE #ifdef branch must survive: same qualified
    # name and same conditional root as each other, but different parameter lists.
    # _doc_sig's attrs["signature"] is always empty for C/C++ (the parameter_list
    # sits under function_declarator, not a direct field of function_definition),
    # so the dedup's signature discriminator must be read directly off def_ts
    # (see _signature_text) -- otherwise this collapses to 1 node, silently
    # deleting a real overload.
    src = (b"#ifdef USE_FAST\n"
           b"void Setup() {}\n"
           b"void Setup(int x) {}\n"
           b"#endif\n")
    nodes, _edges, _calls, _inh = parse_source("r", "f.cpp", src, "cpp")
    setups = [n for n in nodes if n.name == "Setup"]
    assert len(setups) == 2


def test_parse_cpp_ifndef_guard_overloads_are_not_conflated_as_twins():
    # An #ifndef/#define/.../#endif header guard is ITSELF a preproc_ifdef node
    # with no #else -- every def in the guarded header shares that one root, so
    # a same-root-only pre-filter is a no-op for the whole file. Two genuine
    # overloads (const vs. non-const) sitting directly under the guard, with no
    # #else branch between them, must both survive: same conditional root, but
    # NOT in different branches of it (_conditional_branch returns None for
    # both), so _dedupe_preprocessor_twins must refuse to merge them regardless
    # of what _signature_text reports.
    src = (b"#ifndef FOO_H\n"
           b"#define FOO_H\n"
           b"class C {\n"
           b"public:\n"
           b"    int at(int i) { return 1; }\n"
           b"    int at(int i) const { return 2; }\n"
           b"};\n"
           b"#endif\n")
    nodes, _edges, _calls, _inh = parse_source("r", "f.h", src, "cpp")
    ats = [n for n in nodes if n.name == "at"]
    assert len(ats) == 2


def test_parse_cpp_ifndef_guard_ref_qualified_overloads_are_not_conflated():
    # Same shape as the const/non-const case above, but for ref-qualifiers
    # (& vs &&) -- tree-sitter attaches these as siblings of the parameter_list
    # inside the same function_declarator, not inside the parameter_list itself,
    # so _signature_text must read the whole declarator's text, not just its
    # parameter_list child, or these look identical and one is deleted.
    src = (b"#ifndef FOO_H\n"
           b"#define FOO_H\n"
           b"class C {\n"
           b"public:\n"
           b"    int get() & { return 1; }\n"
           b"    int get() && { return 2; }\n"
           b"};\n"
           b"#endif\n")
    nodes, _edges, _calls, _inh = parse_source("r", "f.h", src, "cpp")
    gets = [n for n in nodes if n.name == "get"]
    assert len(gets) == 2


def test_parse_cpp_ifdef_else_twin_nested_inside_guard_still_collapses():
    # The intended behavior must survive branch-differentiation: a genuine
    # #ifdef/#else twin NESTED inside an #ifndef header guard still collapses
    # to 1 node -- its conditional root is the inner #ifdef (the nearest
    # enclosing preproc_ifdef), independent of the outer guard, and the two
    # branches are the inner #ifdef's primary body vs. its #else body.
    src = (b"#ifndef FOO_H\n"
           b"#define FOO_H\n"
           b"#ifdef USE_FAST\n"
           b"void Setup() {}\n"
           b"#else\n"
           b"void Setup() {}\n"
           b"#endif\n"
           b"#endif\n")
    nodes, _edges, _calls, _inh = parse_source("r", "f.h", src, "cpp")
    setups = [n for n in nodes if n.name == "Setup"]
    assert len(setups) == 1


def test_index_repo_dir_resolves_out_of_line_method_across_files(tmp_path):
    (tmp_path / "widget.h").write_text(
        "class Widget {\npublic:\n    void Draw();\n};\n")
    (tmp_path / "widget.cpp").write_text(
        "#include \"widget.h\"\nvoid Widget::Draw() {\n}\n")
    shard = index_repo_dir(str(tmp_path), "demo/widgets")
    draw = next(n for n in shard.nodes if n.name == "Draw")
    assert draw.kind == "method"
    widget = next(n for n in shard.nodes if n.name == "Widget")
    assert (widget.id, draw.id, "contains") in {
        (e.src, e.dst, e.relation) for e in shard.edges}
    # exactly one containment edge into Draw -- not both file and class
    assert sum(1 for e in shard.edges if e.dst == draw.id and e.relation == "contains") == 1


def test_index_repo_dir_resolves_out_of_line_method_three_segments_across_files(tmp_path):
    (tmp_path / "gadget.h").write_text(
        "namespace App {\nclass Gadget {\npublic:\n    void Spin();\n};\n}\n")
    (tmp_path / "gadget.cpp").write_text(
        "#include \"gadget.h\"\nvoid App::Gadget::Spin() {\n}\n")
    shard = index_repo_dir(str(tmp_path), "demo/gadgets")
    spin = next(n for n in shard.nodes if n.name == "Spin")
    assert spin.kind == "method"
    gadget = next(n for n in shard.nodes if n.name == "Gadget")
    assert (gadget.id, spin.id, "contains") in {
        (e.src, e.dst, e.relation) for e in shard.edges}
    assert sum(1 for e in shard.edges if e.dst == spin.id and e.relation == "contains") == 1


def test_index_repo_dir_forward_declaration_does_not_block_resolution(tmp_path):
    # A forward declaration ("class Widget;", the standard way to break an
    # #include cycle) must not produce a second same-named class node -- that
    # would make out-of-line-method resolution ambiguous (candidates != 1) and
    # silently leave Draw as "function"/file-contained.
    (tmp_path / "fwd.h").write_text("class Widget;\n")
    (tmp_path / "widget.h").write_text(
        "class Widget {\npublic:\n    void Draw();\n};\n")
    (tmp_path / "widget.cpp").write_text(
        "#include \"widget.h\"\nvoid Widget::Draw() {\n}\n")
    shard = index_repo_dir(str(tmp_path), "demo/widgets_fwd")
    widgets = [n for n in shard.nodes if n.name == "Widget"]
    assert len(widgets) == 1, "the forward declaration must not create a second class node"
    draw = next(n for n in shard.nodes if n.name == "Draw")
    assert draw.kind == "method"
    assert (widgets[0].id, draw.id, "contains") in {
        (e.src, e.dst, e.relation) for e in shard.edges}


def test_h_extension_uses_cpp_grammar_without_losing_plain_c_defs(tmp_path):
    # ".h" is parsed with the "cpp" grammar (a near-superset of C) so the common
    # C++ header/.cpp split resolves out-of-line methods (see the two tests
    # above); this pins that a genuinely C-only ".h" still extracts its
    # definitions correctly under that grammar -- no regression for plain C.
    (tmp_path / "point.h").write_text(
        "struct Point {\n    int x;\n    int y;\n};\n\n"
        "int distance(struct Point a, struct Point b) {\n    return 0;\n}\n")
    shard = index_repo_dir(str(tmp_path), "demo/point")
    point = next(n for n in shard.nodes if n.name == "Point")
    assert point.kind == "struct"
    distance = next(n for n in shard.nodes if n.name == "distance")
    assert distance.kind == "function"


def test_parse_rust():
    src = (b"use std::io::Read;\nstruct Server { addr: String }\ntrait Handler { fn go(&self); }\n"
           b"enum State { On }\nfn mk() -> Server { work(); Server { addr: String::new() } }\n")
    nodes, _e, calls, _i = parse_source("r", "f.rs", src, "rust")
    k = _kinds(nodes)
    assert "Server" in k["struct"] and "Handler" in k["interface"] and "State" in k["enum"]
    assert "mk" in k["function"] and "std::io::Read" in k["module"]
    assert "work" in {c[1] for c in calls}


def test_parse_ruby():
    src = (b"module Acme\n class Animal\n  def speak; end\n end\n"
           b" class Dog < Animal\n  def bark; end\n end\nend\n")
    nodes, _e, _c, inh = parse_source("r", "f.rb", src, "ruby")
    k = _kinds(nodes)
    assert "Animal" in k["class"] and "Dog" in k["class"]   # module also indexes as class-kind
    assert "speak" in k["method"] and "bark" in k["method"]
    assert "Animal" in {base for _s, base, _f, _l in inh}   # Dog < Animal


def test_parse_php():
    src = (b"<?php\nnamespace App;\nuse App\\Base;\n"
           b"interface Auditable { public function audit(); }\n"
           b"class CatalogService extends Base implements Auditable {\n"
           b"  public function get($id) { return $this->repo->find($id); }\n}\n")
    nodes, _e, calls, inh = parse_source("r", "f.php", src, "php")
    k = _kinds(nodes)
    assert "CatalogService" in k["class"] and "Auditable" in k["interface"] and "get" in k["method"]
    assert "App\\Base" in k["module"] and "find" in {c[1] for c in calls}
    assert {"Base", "Auditable"} <= {base for _s, base, _f, _l in inh}


def test_parse_scala():
    src = (b"trait Handler { def serve(): Unit }\n"
           b"class Server(a: String) extends Handler { def serve(): Unit = { help() } }\n"
           b"object App { def main(): Unit = () }\n")
    nodes, _e, calls, inh = parse_source("r", "f.scala", src, "scala")
    k = _kinds(nodes)
    assert "Handler" in k["interface"] and "Server" in k["class"] and "App" in k["class"]
    assert "serve" in k["method"] and "help" in {c[1] for c in calls}
    assert "Handler" in {base for _s, base, _f, _l in inh}   # extends Handler


def test_parse_kotlin():
    src = (
        b"package com.example.demo\n"
        b"import kotlinx.coroutines.launch\n"
        b"import com.example.Base\n\n"
        b"interface Repository {\n"
        b"    fun findById(id: String): Order?\n"
        b"}\n\n"
        b"sealed class Result {\n"
        b"    data class Success(val order: Order) : Result()\n"
        b"    object Empty : Result()\n"
        b"}\n\n"
        b"enum class Status { ACTIVE, CANCELLED }\n\n"
        b"class CatalogService(private val repo: Repository) : Base(), Repository {\n"
        b"    companion object {\n"
        b"        fun create(): CatalogService = CatalogService(InMemoryRepo())\n"
        b"    }\n"
        b"    fun process(id: String): Result {\n"
        b"        return helper(id)\n"
        b"    }\n"
        b"}\n\n"
        b"class Repo2 : com.example.Base(), Comparable<Order> {\n"
        b"}\n\n"
        b"fun freeFunction() {\n"
        b"    doThing()\n"
        b"}\n\n"
        b"fun Order.validate() {\n"
        b"    check()\n"
        b"}\n"
    )
    nodes, edges, calls, inherits = parse_source("team/kt", "Svc.kt", src, "kotlin")
    by_kind: dict[str, set] = {}
    for n in nodes:
        by_kind.setdefault(n.kind, set()).add(n.name)
    # interface/enum/data/sealed/class ALL map to kind "class" (intentional collapse)
    assert {"Repository", "Result", "Success", "Status", "CatalogService", "Repo2"} \
        <= by_kind["class"]
    # a top-level fun is a function; a fun inside a class is a method
    assert "freeFunction" in by_kind["function"]
    assert "process" in by_kind.get("method", set())
    # an extension function (fun Order.validate()) is a top-level function named after
    # the function itself, not the receiver type
    assert "validate" in by_kind["function"]
    assert "Order" not in by_kind["function"]
    # imports -> module nodes
    modules = {n.name for n in nodes if n.kind == "module"}
    assert "kotlinx.coroutines.launch" in modules
    assert "com.example.Base" in modules
    # unresolved calls captured (callee names)
    callee_names = {c[1] for c in calls}
    assert "doThing" in callee_names
    assert "helper" in callee_names
    # inheritance captured (subclass -> base name); Base and Repository are bases
    base_names = {i[1] for i in inherits}
    assert "Base" in base_names
    assert "Repository" in base_names
    # dotted supertype (com.example.Base) resolves to its last segment only; the
    # intermediate package segments must NOT leak in as spurious bases
    assert "com" not in base_names and "example" not in base_names
    # generic supertype (Comparable<Order>) resolves to the bare type name; the type
    # argument (Order) must NOT leak in as a spurious base
    assert "Comparable" in base_names
    assert "Order" not in base_names


def test_lang_by_ext_covers_target_languages():
    from contextlake.kb.parse import LANG_BY_EXT
    for ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".go", ".java",
                ".c", ".h", ".cpp", ".hpp", ".rs", ".rb", ".php", ".scala", ".kt", ".kts"):
        assert ext in LANG_BY_EXT
    assert LANG_BY_EXT[".kts"] == "kotlin"


def test_cross_repo_dependency_via_shared_package(tmp_path):
    from contextlake.kb.model import Repo
    from contextlake.kb.store.sqlite_store import SqliteStore

    (tmp_path / "producer").mkdir()
    (tmp_path / "producer" / "pyproject.toml").write_text('[project]\nname = "libx"\n')
    (tmp_path / "consumer").mkdir()
    (tmp_path / "consumer" / "pyproject.toml").write_text(
        '[project]\nname = "app"\ndependencies = ["libx>=1"]\n'
    )
    store = SqliteStore(tmp_path / "kb.sqlite")
    for r in ("producer", "consumer"):
        shard = index_repo_dir(str(tmp_path / r), r)
        store.upsert_repo(Repo(id=r, path=str(tmp_path / r)))
        store.upsert_nodes(r, shard.nodes)
        store.upsert_edges(r, shard.edges)

    libx = store.nodes_by_name("libx")[0]  # one shared global package node
    incoming = {e.relation for e in store.neighbors(libx.id, direction="in")}
    assert {"publishes", "depends_on"} <= incoming  # producer publishes, consumer depends_on
    store.close()


def test_discover_repos_finds_and_prunes(tmp_path):
    # A repo_id is now canonical (from the remote, or a dirname+root-commit
    # fallback with no remote) -- not the path relative to root. These repos
    # have neither a real remote nor commits, so each falls back to its own
    # directory name.
    from contextlake.kb.parse import discover_repos

    _empty_git_repo(tmp_path / "team" / "a")
    _empty_git_repo(tmp_path / "b")
    _empty_git_repo(tmp_path / "team" / "a" / "nested")  # inside a repo -> skip
    repos = dict(discover_repos(str(tmp_path)))
    assert set(repos) == {"a", "b"}  # nested repo not descended into
    assert repos["a"] == str(tmp_path / "team" / "a")


def test_discover_repos_skips_vendored_nested(tmp_path):
    from contextlake.kb.parse import discover_repos

    # a normal repo, plus a nested vendored upstream repo (module-federation) found
    # because its parent (app-host) is not itself a repo. The vendored one is skipped.
    _empty_git_repo(tmp_path / "app")
    _empty_git_repo(tmp_path / "app-host" / "module-federation" / "demo")
    ids = {rid for rid, _ in discover_repos(str(tmp_path))}
    assert "app" in ids
    assert not any("module-federation" in rid for rid in ids)


def test_discover_repos_skips_a_corrupted_git_instead_of_misattributing_it(tmp_path):
    """The exact bug this guards against: a broken nested .git must never be
    silently indexed under an unrelated ancestor repo's identity (git itself
    walks up past an incomplete gitdir to find the nearest real one)."""
    from contextlake.kb.parse import discover_repos

    _git_repo(tmp_path / "workspace", remote="https://example.com/acme/ancestor.git")
    broken = tmp_path / "workspace" / "nested" / "broken-checkout"
    broken.mkdir(parents=True)
    (broken / ".git").mkdir()
    (broken / ".git" / "HEAD").write_text("not a real gitdir\n")
    ids = {rid for rid, _ in discover_repos(str(tmp_path))}
    assert ids == {"example.com/acme/ancestor"}  # the broken checkout never became a second node


def _git_repo(path, *, remote=None, commit_message="init"):
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,  # noqa: E731
                                    capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "a@b.c")
    run("config", "user.name", "a")
    (path / "f.txt").write_text(commit_message)
    run("add", "-A")
    run("commit", "-q", "-m", commit_message)
    if remote:
        run("remote", "add", "origin", remote)


def _empty_git_repo(path):
    """A real (structurally valid) git repo with no commits and no remote --
    unlike a bare ``mkdir(".git")``, this is a state ``git`` itself recognizes
    as this directory's own repo, which discover_repos now requires."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True,
                   capture_output=True, text=True)


def test_discover_repos_uses_canonical_remote_id_not_local_path(tmp_path):
    from contextlake.kb.parse import discover_repos

    _git_repo(tmp_path / "some" / "local" / "path", remote="https://example.com/acme/widgets.git")
    ids = {rid for rid, _ in discover_repos(str(tmp_path))}
    assert ids == {"example.com/acme/widgets"}  # not "some/local/path"


def test_discover_repos_collapses_duplicate_checkouts_of_the_same_remote(tmp_path):
    """The exact bug this fixes: a stale pre-reorg clone left alongside its
    replacement must not become two nodes for one project."""
    from contextlake.kb.parse import discover_repos

    old = tmp_path / "old-namespace" / "widgets"
    new = tmp_path / "new-namespace" / "widgets"
    _git_repo(old, remote="https://example.com/acme/widgets.git", commit_message="old commit")
    _git_repo(new, remote="https://example.com/acme/widgets.git", commit_message="new commit")
    # make `new` the more recently committed checkout
    import subprocess
    import time
    time.sleep(1.1)  # commit-time resolution is whole seconds
    (new / "f.txt").write_text("newer")
    subprocess.run(["git", "-C", str(new), "commit", "-aqm", "newer"], check=True)

    repos = dict(discover_repos(str(tmp_path)))
    assert len(repos) == 1
    assert repos["example.com/acme/widgets"] == str(new)  # the more recent checkout wins


def test_resolves_call_edges(tmp_path):
    (tmp_path / "a.py").write_text("def helper():\n    pass\n\n\ndef main():\n    helper()\n")
    shard = index_repo_dir(str(tmp_path), "r")
    ids = {n.name: n.id for n in shard.nodes}
    call_edges = [e for e in shard.edges if e.relation == "calls"]
    assert (ids["main"], ids["helper"]) in {(e.src, e.dst) for e in call_edges}
    assert all(e.confidence is Confidence.INFERRED for e in call_edges)


def test_resolves_calls_across_files(tmp_path):
    (tmp_path / "util.py").write_text("def shared():\n    pass\n")
    (tmp_path / "app.py").write_text("def run():\n    shared()\n")
    shard = index_repo_dir(str(tmp_path), "r")
    ids = {n.name: n.id for n in shard.nodes}
    assert (ids["run"], ids["shared"]) in {(e.src, e.dst) for e in shard.edges
                                           if e.relation == "calls"}


def test_ambiguous_calls_emit_ambiguous_edges(tmp_path):
    # two methods named 'h' -> a call to h() is ambiguous: emit an AMBIGUOUS edge
    # to each candidate (so blast-radius doesn't lose the hottest symbols)
    (tmp_path / "a.py").write_text(
        "class A:\n    def h(self):\n        pass\n\n\n"
        "class B:\n    def h(self):\n        pass\n\n\n"
        "def c():\n    h()\n"
    )
    shard = index_repo_dir(str(tmp_path), "r")
    calls = [e for e in shard.edges if e.relation == "calls"]
    assert len(calls) == 2
    assert all(e.confidence == Confidence.AMBIGUOUS for e in calls)
    assert all(e.context == "ambiguous" for e in calls)
    assert len({e.dst for e in calls}) == 2  # both A.h and B.h are candidate targets


def test_resolve_name_refs_dedup_keeps_lowest_line():
    """A repeated call site (same caller, same callee) must keep the LOWEST source
    line among duplicates, deterministically -- not whichever occurrence tree-sitter's
    capture order happens to list first (unordered w.r.t. source position, verified
    empirically to not always match line order). A sequence-diagram renderer sorting
    calls edges by line depends on this."""
    from contextlake.kb.model import Node
    from contextlake.kb.parse import _resolve_name_refs

    nodes_by_id = {"caller": Node(id="caller", repo="r", kind="function", name="caller"),
                   "helper": Node(id="helper", repo="r", kind="function", name="helper")}
    # deliberately out of line order, as an unordered capture list could be
    refs = [("caller", "helper", "a.py", 10), ("caller", "helper", "a.py", 3)]
    edges = _resolve_name_refs(refs, nodes_by_id, relation="calls", target_kinds={"function"})
    assert len(edges) == 1
    assert edges[0].provenance.source_line == 3


def test_over_ambiguous_calls_are_skipped(tmp_path):
    # a name matching more than the fan-out cap is too generic to be signal
    from contextlake.kb.parse import _MAX_AMBIG_FANOUT
    defs = "\n\n\n".join(f"class C{i}:\n    def g(self):\n        pass"
                         for i in range(_MAX_AMBIG_FANOUT + 1))
    (tmp_path / "a.py").write_text(defs + "\n\n\ndef caller():\n    g()\n")
    shard = index_repo_dir(str(tmp_path), "r")
    assert [e for e in shard.edges if e.relation == "calls"] == []


def test_is_generated_name():
    assert _is_generated_name("Widget.Designer.cs")
    assert _is_generated_name("app.min.js")
    assert _is_generated_name("AssemblyInfo.cs")
    assert not _is_generated_name("service.cs")
    assert not _is_generated_name("app.js")


def test_has_generated_header():
    assert _has_generated_header(b"// <auto-generated/>\nclass X {}")
    assert _has_generated_header(b"# Code generated by protoc. DO NOT EDIT.\n")
    assert not _has_generated_header(b"class Real:\n    pass\n")


def test_index_repo_dir_skips_generated_by_name_and_header(tmp_path):
    (tmp_path / "real.py").write_text("class Keep:\n    pass\n")
    (tmp_path / "Widget.designer.cs").write_text("class DesignerJunk {}\n")
    (tmp_path / "gen.py").write_text("# <auto-generated>\nclass HeaderJunk:\n    pass\n")

    shard = index_repo_dir(str(tmp_path), "demo/app")
    names = {n.name for n in shard.nodes}
    assert "Keep" in names
    assert "DesignerJunk" not in names  # skipped by name
    assert "HeaderJunk" not in names    # skipped by header


def test_index_repo_dir_skip_generated_disabled(tmp_path):
    (tmp_path / "Widget.designer.cs").write_text(
        "namespace N { class DesignerJunk { } }\n")
    shard = index_repo_dir(str(tmp_path), "demo/app", skip_generated=False)
    assert "DesignerJunk" in {n.name for n in shard.nodes}  # now indexed


def test_index_repo_dir_skips_oversized_code(tmp_path):
    (tmp_path / "small.py").write_text("class Small:\n    pass\n")
    (tmp_path / "big.py").write_text("class Big:\n    pass\n" + "# pad\n" * 1000)
    shard = index_repo_dir(str(tmp_path), "demo/app", max_file_bytes=200)
    names = {n.name for n in shard.nodes}
    assert "Small" in names and "Big" not in names  # big.py skipped by size


def test_match_ignore_semantics():
    pats = ["*.lock", "vendor/", "src/gen/*"]
    assert match_ignore("poetry.lock", pats)        # basename glob
    assert match_ignore("a/b/x.lock", pats)         # ...anywhere in the tree
    assert match_ignore("vendor", pats)             # bare dir
    assert match_ignore("vendor/lib/x.py", pats)    # ...and everything under it
    assert match_ignore("src/gen/api.py", pats)     # path glob
    assert not match_ignore("src/app.py", pats)
    assert not match_ignore("vendoring.py", pats)   # not a loose prefix match


def test_load_ignore_patterns(tmp_path):
    (tmp_path / ".contextlakeignore").write_text("# comment\n\nvendor/\n  *.lock  \n")
    assert load_ignore_patterns(tmp_path) == ["vendor/", "*.lock"]
    assert load_ignore_patterns(tmp_path / "missing") == []


def test_contextlakeignore_excludes_dirs_and_files(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "keep.py").write_text("def keep():\n    pass\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.py").write_text("def vendored():\n    pass\n")
    (tmp_path / "thing_pb2.py").write_text("def gen():\n    pass\n")
    (tmp_path / ".contextlakeignore").write_text("vendor/\n*_pb2.py\n")

    shard = index_repo_dir(str(tmp_path), "r")
    files = {n.name for n in shard.nodes if n.kind == "file"}
    assert files == {"pkg/keep.py"}
    names = {n.name for n in shard.nodes}
    assert "keep" in names
    assert "vendored" not in names and "gen" not in names


def _inherits(shard):
    names = {n.id: n.name for n in shard.nodes}
    return sorted((names.get(e.src, e.src), names.get(e.dst, e.dst))
                  for e in shard.edges if e.relation == "inherits")


def test_inherits_edges_across_languages(tmp_path):
    (tmp_path / "a.py").write_text(
        "class Base:\n    pass\nclass Child(Base):\n    pass\n"
        "class Multi(Base, object):\n    pass\n")
    (tmp_path / "b.ts").write_text(
        "class Animal {}\nclass Dog extends Animal {}\n"
        "interface Named {}\nclass Cat extends Animal implements Named {}\n")
    (tmp_path / "c.cs").write_text("class Vehicle { }\nclass Car : Vehicle { }\n")
    (tmp_path / "d.js").write_text("class Widget {}\nclass Button extends Widget {}\n")
    (tmp_path / "e.java").write_text(
        "class BaseService {}\ninterface Auditable {}\n"
        "class CatalogService extends BaseService implements Auditable {}\n")
    (tmp_path / "f.cpp").write_text("class Base {};\nclass Derived : public Base {};\n")
    inh = _inherits(index_repo_dir(str(tmp_path), "demo"))
    assert ("Child", "Base") in inh
    assert ("Dog", "Animal") in inh
    assert ("Cat", "Animal") in inh and ("Cat", "Named") in inh   # extends + implements
    assert ("Car", "Vehicle") in inh
    assert ("Button", "Widget") in inh
    assert ("CatalogService", "BaseService") in inh and ("CatalogService", "Auditable") in inh
    assert ("Derived", "Base") in inh


def test_inherits_unresolved_external_base_is_dropped(tmp_path):
    # A base class not defined in the repo (external framework) yields no edge —
    # same policy as unresolved calls, so the graph never points at a phantom node.
    (tmp_path / "v.py").write_text(
        "import framework\nclass MyView(framework.View):\n    pass\n")
    assert _inherits(index_repo_dir(str(tmp_path), "demo")) == []


def test_inherits_ambiguous_base_marked(tmp_path):
    # Two classes named Base in different files -> the subclass inherits both,
    # emitted AMBIGUOUS (never silently dropped).
    (tmp_path / "one.py").write_text("class Base:\n    pass\n")
    (tmp_path / "two.py").write_text("class Base:\n    pass\n")
    (tmp_path / "sub.py").write_text("from x import Base\nclass Sub(Base):\n    pass\n")
    shard = index_repo_dir(str(tmp_path), "demo")
    inh = [e for e in shard.edges if e.relation == "inherits"]
    assert len(inh) == 2 and all(e.confidence == Confidence.AMBIGUOUS for e in inh)
