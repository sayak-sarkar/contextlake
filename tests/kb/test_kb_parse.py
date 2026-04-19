"""Tests for tree-sitter code parsing."""

from datetime import date

from contextlake.kb.model import Confidence
from contextlake.kb.parse import (
    _has_generated_header,
    _is_generated_name,
    index_repo_dir,
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


def test_parse_extracts_defs_and_imports():
    nodes, edges, _ = parse_source(
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
    nodes, _, _ = parse_source("r", "m.py", PY, "python")
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


def test_lang_by_ext_covers_target_languages():
    from contextlake.kb.parse import LANG_BY_EXT
    for ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".cs"):
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
