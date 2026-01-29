"""Tests for package-manifest parsing and cross-repo dependency links."""

from gitlab_sync.kb.manifest import is_manifest, parse_manifest
from gitlab_sync.kb.model import Confidence


def _names(nodes, ids):
    by_id = {n.id: n.name for n in nodes}
    return {by_id[i] for i in ids}


def test_pyproject():
    content = (b'[project]\nname = "myapp"\n'
               b'dependencies = ["requests>=2", "tomli; python_version<\'3.11\'"]\n'
               b'[project.optional-dependencies]\ndev = ["pytest>=7"]\n')
    nodes, edges = parse_manifest("team/api", "pyproject.toml", content)
    assert {"myapp", "requests", "tomli", "pytest"} <= {n.name for n in nodes if n.kind == "package"}
    rels = {(e.relation, _names(nodes, [e.dst]).pop()) for e in edges}
    assert ("publishes", "myapp") in rels
    assert ("depends_on", "requests") in rels and ("depends_on", "pytest") in rels
    assert all(e.confidence is Confidence.EXTRACTED for e in edges)


def test_package_json():
    content = b'{"name":"web","dependencies":{"react":"^18"},"devDependencies":{"jest":"^29"}}'
    nodes, edges = parse_manifest("r", "package.json", content)
    assert {"web", "react", "jest"} <= {n.name for n in nodes if n.kind == "package"}


def test_csproj():
    content = (b'<Project><ItemGroup>'
               b'<PackageReference Include="Newtonsoft.Json" Version="13" />'
               b'</ItemGroup></Project>')
    nodes, _ = parse_manifest("r", "src/App.csproj", content)
    pkgs = {n.name for n in nodes if n.kind == "package"}
    assert "Newtonsoft.Json" in pkgs and "App" in pkgs  # App = published (csproj stem)


def test_global_package_ids_are_shared_across_repos():
    n1, _ = parse_manifest("repo-a", "pyproject.toml",
                           b'[project]\nname="a"\ndependencies=["shared-lib>=1"]\n')
    n2, _ = parse_manifest("repo-b", "pyproject.toml",
                           b'[project]\nname="b"\ndependencies=["shared-lib>=1"]\n')
    id1 = next(n.id for n in n1 if n.name == "shared-lib")
    id2 = next(n.id for n in n2 if n.name == "shared-lib")
    assert id1 == id2  # same node id -> the cross-repo dependency link


def test_malformed_manifest_is_tolerated():
    assert parse_manifest("r", "pyproject.toml", b"{ not toml") == ([], [])
    assert parse_manifest("r", "package.json", b"{ not json") == ([], [])


def test_is_manifest():
    assert is_manifest("pyproject.toml") and is_manifest("package.json")
    assert is_manifest("Foo.csproj") and not is_manifest("main.py")
