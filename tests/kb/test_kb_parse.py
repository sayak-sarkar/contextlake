"""Tests for tree-sitter code parsing."""

from datetime import date

from gitlab_sync.kb.model import Confidence
from gitlab_sync.kb.parse import index_repo_dir, parse_source

PY = b"""import os
from a.b import c


class Foo:
    def bar(self):
        pass


def top():
    pass
"""


def test_parse_extracts_defs_and_imports():
    nodes, edges = parse_source("team/api", "svc.py", PY, "python", verified_at=date(2026, 6, 21))
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
    nodes, _ = parse_source("r", "m.py", PY, "python")
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
    from gitlab_sync.kb.parse import LANG_BY_EXT
    for ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".cs"):
        assert ext in LANG_BY_EXT
