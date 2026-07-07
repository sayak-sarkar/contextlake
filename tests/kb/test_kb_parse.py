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
           b"public class OrderService extends BaseService implements Auditable {\n"
           b"  public OrderService() {}\n"
           b"  public Order get(String id) { return repo.find(id); }\n}\n"
           b"interface Auditable { void audit(); }\nenum Status { OPEN, CLOSED }\n")
    nodes, _e, calls, _i = parse_source("r", "f.java", src, "java")
    k = _kinds(nodes)
    assert "OrderService" in k["class"] and "Auditable" in k["interface"] and "Status" in k["enum"]
    assert "get" in k["method"] and "OrderService" in k["method"]   # constructor indexes as method
    assert "java.util.List" in k["module"]
    assert "find" in {c[1] for c in calls}


def test_lang_by_ext_covers_target_languages():
    from contextlake.kb.parse import LANG_BY_EXT
    for ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".go", ".java"):
        assert ext in LANG_BY_EXT


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
    from contextlake.kb.parse import discover_repos

    (tmp_path / "team" / "a" / ".git").mkdir(parents=True)
    (tmp_path / "b" / ".git").mkdir(parents=True)
    (tmp_path / "team" / "a" / "nested" / ".git").mkdir(parents=True)  # inside a repo -> skip
    repos = dict(discover_repos(str(tmp_path)))
    assert set(repos) == {"team/a", "b"}  # nested repo not descended into


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
        "class OrderService extends BaseService implements Auditable {}\n")
    inh = _inherits(index_repo_dir(str(tmp_path), "demo"))
    assert ("Child", "Base") in inh
    assert ("Dog", "Animal") in inh
    assert ("Cat", "Animal") in inh and ("Cat", "Named") in inh   # extends + implements
    assert ("Car", "Vehicle") in inh
    assert ("Button", "Widget") in inh
    assert ("OrderService", "BaseService") in inh and ("OrderService", "Auditable") in inh


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
