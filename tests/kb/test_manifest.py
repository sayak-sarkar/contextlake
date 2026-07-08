"""Tests for Maven pom.xml manifest parsing."""

from contextlake.kb.manifest import is_manifest, parse_manifest

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


def _pkg_name(nodes, node_id):
    return next(n.name for n in nodes if n.id == node_id)
