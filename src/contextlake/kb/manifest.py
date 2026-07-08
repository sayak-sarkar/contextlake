"""Parse package manifests into publish/depends-on facts.

Package nodes are **global** — keyed by ecosystem + name, not by repo — so a
package published by one repository and consumed by another reference the *same*
node. That shared node is the cross-repo dependency link: "who depends on repo A"
is "who depends_on a package A publishes".

Supported: ``pyproject.toml`` (PyPI), ``package.json`` (npm), ``*.csproj`` (NuGet),
``pom.xml`` (Maven).
"""

from __future__ import annotations

import json
import re
from datetime import date

from .ids import make_id
from .model import Confidence, Edge, Node, Provenance

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_DEP_NAME = re.compile(r"[A-Za-z0-9._-]+")
_PKG_REF = re.compile(r'<PackageReference\s+Include="([^"]+)"', re.IGNORECASE)
_MANIFEST_FILES = {"pyproject.toml", "package.json"}

# Maven: pull coordinates from the XML text with regex (dependency-free, same
# spirit as _PKG_REF for .csproj) rather than an XML AST — robust to namespaces.
_MVN_GROUP = re.compile(r"<groupId>\s*([^<\s][^<]*?)\s*</groupId>", re.IGNORECASE)
_MVN_ARTIFACT = re.compile(r"<artifactId>\s*([^<\s][^<]*?)\s*</artifactId>", re.IGNORECASE)
_MVN_DEP_BLOCK = re.compile(r"<dependency\b[^>]*>(.*?)</dependency>", re.DOTALL | re.IGNORECASE)
_MVN_PARENT_BLOCK = re.compile(r"<parent\b[^>]*>(.*?)</parent>", re.DOTALL | re.IGNORECASE)
# Sections whose groupId/artifactId are NOT the project's own coordinate.
_MVN_NON_PROJECT = re.compile(
    r"<(dependencies|dependencyManagement|build|profiles|reporting|parent|pluginManagement|plugins)\b"
    r"[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def is_manifest(filename: str) -> bool:
    return (filename in _MANIFEST_FILES or filename == "pom.xml"
            or filename.endswith(".csproj"))


def _dep_name(spec: str) -> str | None:
    m = _DEP_NAME.match(spec.strip())
    return m.group(0) if m else None


def _mvn_coord(block: str) -> str | None:
    g = _MVN_GROUP.search(block)
    a = _MVN_ARTIFACT.search(block)
    if a is None:
        return None
    artifact = a.group(1).strip()
    group = g.group(1).strip() if g else ""
    return f"{group}:{artifact}" if group else artifact


def _maven_deps(text: str) -> list[str]:
    out = []
    for m in _MVN_DEP_BLOCK.finditer(text):
        coord = _mvn_coord(m.group(1))
        if coord:
            out.append(coord)
    return out


def _maven_project_coord(text: str) -> str | None:
    # The project's own coordinate is the first groupId/artifactId that is NOT
    # inside a dependency/parent/build/etc. section.
    stripped = _MVN_NON_PROJECT.sub("", text)
    a = _MVN_ARTIFACT.search(stripped)
    if a is None:
        return None
    artifact = a.group(1).strip()
    g = _MVN_GROUP.search(stripped)
    group = g.group(1).strip() if g else ""
    if not group:  # groupId inherited from <parent>
        pm = _MVN_PARENT_BLOCK.search(text)
        if pm:
            pg = _MVN_GROUP.search(pm.group(1))
            group = pg.group(1).strip() if pg else ""
    return f"{group}:{artifact}" if group else artifact


def _package_node(name: str, ecosystem: str) -> Node:
    return Node(
        id=make_id("pkg", ecosystem, name), repo="(packages)", kind="package",
        name=name, lang=ecosystem, attrs={"ecosystem": ecosystem},
    )


def parse_manifest(
    repo_id: str, rel_path: str, content: bytes, verified_at: date | None = None
) -> tuple[list[Node], list[Edge]]:
    """Parse a manifest into (nodes, edges): a manifest file node, global package
    nodes, and ``publishes`` / ``depends_on`` edges."""
    verified_at = verified_at or date.today()
    fname = rel_path.rsplit("/", 1)[-1]
    published: str | None = None
    deps: list[str] = []
    ecosystem = ""

    if fname == "pyproject.toml":
        ecosystem = "pypi"
        try:
            data = tomllib.loads(content.decode("utf-8", "replace"))
        except (tomllib.TOMLDecodeError, ValueError):
            return [], []
        proj = data.get("project", {})
        published = proj.get("name")
        raw = list(proj.get("dependencies", []))
        for group in (proj.get("optional-dependencies") or {}).values():
            raw += list(group)
        deps = [n for d in raw if (n := _dep_name(d))]
    elif fname == "package.json":
        ecosystem = "npm"
        try:
            data = json.loads(content.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return [], []
        published = data.get("name")
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            deps += list(data.get(section) or {})
    elif fname.endswith(".csproj"):
        ecosystem = "nuget"
        published = fname[: -len(".csproj")]
        deps = _PKG_REF.findall(content.decode("utf-8", "replace"))
    elif fname == "pom.xml":
        ecosystem = "maven"
        text = content.decode("utf-8", "replace")
        published = _maven_project_coord(text)
        deps = _maven_deps(text)
    else:
        return [], []

    file_id = make_id(repo_id, rel_path)
    nodes = [Node(id=file_id, repo=repo_id, kind="file", name=rel_path, file=rel_path,
                  lang="manifest")]
    edges = []
    prov = Provenance(source_file=rel_path, source_line=1, verified_at=verified_at)

    if published:
        pn = _package_node(published, ecosystem)
        nodes.append(pn)
        edges.append(Edge(src=file_id, dst=pn.id, relation="publishes",
                          confidence=Confidence.EXTRACTED, provenance=prov))
    for dep in deps:
        pn = _package_node(dep, ecosystem)
        nodes.append(pn)
        edges.append(Edge(src=file_id, dst=pn.id, relation="depends_on",
                          confidence=Confidence.EXTRACTED, provenance=prov))
    return nodes, edges
