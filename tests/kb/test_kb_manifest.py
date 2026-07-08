"""Tests for package-manifest parsing and cross-repo dependency links."""

from contextlake.kb.manifest import is_manifest, parse_manifest
from contextlake.kb.model import Confidence


def _names(nodes, ids):
    by_id = {n.id: n.name for n in nodes}
    return {by_id[i] for i in ids}


def _pkg_name(nodes, node_id):
    return next(n.name for n in nodes if n.id == node_id)


def test_pyproject():
    content = (b'[project]\nname = "myapp"\n'
               b'dependencies = ["requests>=2", "tomli; python_version<\'3.11\'"]\n'
               b'[project.optional-dependencies]\ndev = ["pytest>=7"]\n')
    nodes, edges = parse_manifest("team/api", "pyproject.toml", content)
    pkgs = {n.name for n in nodes if n.kind == "package"}
    assert {"myapp", "requests", "tomli", "pytest"} <= pkgs
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


_POM = b"""<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme.orders</groupId>
  <artifactId>orders-api</artifactId>
  <version>1.2.0</version>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
      <version>3.1.0</version>
    </dependency>
    <dependency>
      <groupId>com.acme.common</groupId>
      <artifactId>acme-common</artifactId>
    </dependency>
  </dependencies>
</project>
"""

_POM_PARENT_GROUP = b"""<?xml version="1.0"?>
<project>
  <parent>
    <groupId>com.acme.platform</groupId>
    <artifactId>platform-parent</artifactId>
    <version>2.0.0</version>
  </parent>
  <artifactId>billing-svc</artifactId>
</project>
"""


def test_pom_is_a_manifest():
    assert is_manifest("pom.xml") is True


def test_pom_publishes_project_and_depends_on_each_dependency():
    nodes, edges = parse_manifest("acme/orders-api", "pom.xml", _POM)
    rels = {(e.relation, _pkg_name(nodes, e.dst)) for e in edges}
    assert ("publishes", "com.acme.orders:orders-api") in rels
    assert ("depends_on", "org.springframework.boot:spring-boot-starter-web") in rels
    assert ("depends_on", "com.acme.common:acme-common") in rels
    # all package nodes are the maven ecosystem
    assert all(n.attrs.get("ecosystem") == "maven"
               for n in nodes if n.kind == "package")


def test_pom_inherits_group_id_from_parent_when_absent():
    nodes, edges = parse_manifest("acme/billing", "pom.xml", _POM_PARENT_GROUP)
    published = [_pkg_name(nodes, e.dst) for e in edges if e.relation == "publishes"]
    assert published == ["com.acme.platform:billing-svc"]
