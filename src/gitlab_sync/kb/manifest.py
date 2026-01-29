"""Parse package manifests into publish/depends-on facts.

Package nodes are **global** — keyed by ecosystem + name, not by repo — so a
package published by one repository and consumed by another reference the *same*
node. That shared node is the cross-repo dependency link: "who depends on repo A"
is "who depends_on a package A publishes".

Supported: ``pyproject.toml`` (PyPI), ``package.json`` (npm), ``*.csproj`` (NuGet).
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


def is_manifest(filename: str) -> bool:
    return filename in _MANIFEST_FILES or filename.endswith(".csproj")


def _dep_name(spec: str) -> str | None:
    m = _DEP_NAME.match(spec.strip())
    return m.group(0) if m else None


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
