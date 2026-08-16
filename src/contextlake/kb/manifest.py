"""Parse package manifests into publish/depends-on facts.

Package nodes are **global** — keyed by ecosystem + name, not by repo — so a
package published by one repository and consumed by another reference the *same*
node. That shared node is the cross-repo dependency link: "who depends on repo A"
is "who depends_on a package A publishes".

Supported: ``pyproject.toml`` (PyPI), ``package.json`` (npm), ``*.csproj`` (NuGet),
``pom.xml`` (Maven).
"""

from __future__ import annotations

import bisect
import json
import re
from datetime import date

from .ids import make_id
from .model import PACKAGES_REPO, Confidence, Edge, Node, Provenance

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
# Element blocks are matched by pairing an opening tag with a closing tag found
# in a separate linear pass (see _xml_blocks), NOT by a single
# ``<tag\b[^>]*>(.*?)</tag>`` regex. That single-regex form is O(n^2) on a
# pom.xml whose closing tags are missing -- and a truncated download is enough
# to produce one. For each of k unclosed openers the lazy ``.*?`` scans forward
# to end-of-string before giving up, so cost grows as k * remaining_length:
# measured 7.1s for 8k unclosed <dependency> tags with an 80KB tail and 27.0s
# at double that, i.e. genuinely quadratic rather than a large constant.
# ``[^<>]`` rather than ``[^>]`` inside the opening tag for the same reason:
# ``[^>]*>`` re-scans to end-of-string from every ``<dependency`` that has no
# ``>`` after it at all (a second, ~10x smaller quadratic: 0.65s -> 2.5s on the
# same doubling). No well-formed XML has a raw ``<`` inside a tag, so nothing
# valid changes shape.
_MVN_DEP_OPEN = re.compile(r"<(dependency)\b[^<>]*>", re.IGNORECASE)
_MVN_DEP_CLOSE = re.compile(r"</(dependency)>", re.IGNORECASE)
_MVN_PARENT_OPEN = re.compile(r"<(parent)\b[^<>]*>", re.IGNORECASE)
_MVN_PARENT_CLOSE = re.compile(r"</(parent)>", re.IGNORECASE)
# Sections whose groupId/artifactId are NOT the project's own coordinate.
_MVN_NON_PROJECT_TAGS = (
    "dependencies|dependencyManagement|build|profiles|reporting|parent|pluginManagement|plugins")
_MVN_NON_PROJECT_OPEN = re.compile(rf"<({_MVN_NON_PROJECT_TAGS})\b[^<>]*>", re.IGNORECASE)
_MVN_NON_PROJECT_CLOSE = re.compile(rf"</({_MVN_NON_PROJECT_TAGS})>", re.IGNORECASE)


def is_manifest(filename: str) -> bool:
    return (filename in _MANIFEST_FILES or filename == "pom.xml"
            or filename.endswith(".csproj"))


def _dep_name(spec: str) -> str | None:
    m = _DEP_NAME.match(spec.strip())
    return m.group(0) if m else None


def _xml_blocks(
    text: str, open_re: re.Pattern[str], close_re: re.Pattern[str]
) -> list[tuple[int, int, int, int]]:
    """Locate ``<tag ...>inner</tag>`` blocks as ``(start, inner_start, inner_end, end)``.

    Same result as ``re.finditer(r'<tag\\b[^>]*>(.*?)</tag>', DOTALL)`` -- leftmost,
    non-overlapping, closed by the *nearest* following closing tag -- but linear
    instead of quadratic, because every closing tag is located once up front and
    then looked up by bisection rather than re-scanned for from each opener.

    ``open_re`` and ``close_re`` must each capture the tag name in group 1;
    an opener is paired only with a closer of the same (case-insensitive) name.
    An opener with no closer after it is skipped rather than ending the scan:
    with a multi-tag alternation a later opener of a *different* name may still
    close, which is exactly what ``re.sub`` would have done.
    """
    closes: dict[str, list[tuple[int, int]]] = {}
    for m in close_re.finditer(text):
        closes.setdefault(m.group(1).lower(), []).append((m.start(), m.end()))
    if not closes:
        return []
    close_starts = {name: [s for s, _e in spans] for name, spans in closes.items()}

    blocks: list[tuple[int, int, int, int]] = []
    pos = 0
    for m in open_re.finditer(text):
        if m.start() < pos:
            continue  # nested inside a block already taken; re.finditer skips these too
        spans = closes.get(m.group(1).lower())
        if not spans:
            continue
        i = bisect.bisect_left(close_starts[m.group(1).lower()], m.end())
        if i >= len(spans):
            continue
        close_start, close_end = spans[i]
        blocks.append((m.start(), m.end(), close_start, close_end))
        pos = close_end
    return blocks


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
    for _start, inner_start, inner_end, _end in _xml_blocks(
            text, _MVN_DEP_OPEN, _MVN_DEP_CLOSE):
        coord = _mvn_coord(text[inner_start:inner_end])
        if coord:
            out.append(coord)
    return out


def _maven_project_coord(text: str) -> str | None:
    # The project's own coordinate is the first groupId/artifactId that is NOT
    # inside a dependency/parent/build/etc. section.
    kept: list[str] = []
    cut = 0
    for start, _inner_start, _inner_end, end in _xml_blocks(
            text, _MVN_NON_PROJECT_OPEN, _MVN_NON_PROJECT_CLOSE):
        kept.append(text[cut:start])
        cut = end
    kept.append(text[cut:])
    stripped = "".join(kept)
    a = _MVN_ARTIFACT.search(stripped)
    if a is None:
        return None
    artifact = a.group(1).strip()
    g = _MVN_GROUP.search(stripped)
    group = g.group(1).strip() if g else ""
    if not group:  # groupId inherited from <parent>
        parents = _xml_blocks(text, _MVN_PARENT_OPEN, _MVN_PARENT_CLOSE)
        if parents:
            _s, inner_start, inner_end, _e = parents[0]
            pg = _MVN_GROUP.search(text[inner_start:inner_end])
            group = pg.group(1).strip() if pg else ""
    return f"{group}:{artifact}" if group else artifact


def _package_node(name: str, ecosystem: str) -> Node:
    return Node(
        id=make_id("pkg", ecosystem, name), repo=PACKAGES_REPO, kind="package",
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
    # Console entry points the PACKAGING declares: the commands a user gets on their
    # PATH after installing this. A parse tree cannot see these -- `[project.scripts]`
    # names a command and points at a function that may live in another file entirely --
    # so they are a second producer of the same `entry_point` kind rather than something
    # the tree-sitter pass could be extended to cover.
    scripts: list[str] = []
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
        # `[project.scripts]` and `[project.gui-scripts]` both install a command.
        scripts = [k for key in ("scripts", "gui-scripts")
                   for k in (proj.get(key) or {})]
    elif fname == "package.json":
        ecosystem = "npm"
        try:
            data = json.loads(content.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return [], []
        published = data.get("name")
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            deps += list(data.get(section) or {})
        # `bin` is a string when the package installs ONE command named after itself,
        # and an object when it installs several. Both spellings are common and the
        # string form is the one a dict-only reading drops silently.
        #
        # `scripts` is deliberately NOT read: those are `npm run` targets, which is a
        # different fact from a command on your PATH, and treating a repo's `test` and
        # `lint` entries as entry points would bury the real one.
        binv = data.get("bin")
        if isinstance(binv, str):
            scripts = [published] if published else []
        elif isinstance(binv, dict):
            scripts = list(binv)
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
    # Scoped to the repo, unlike the package nodes above, which are fleet-wide on
    # purpose: `serve` is a command THIS project installs, and two repos that both
    # install one named `serve` install two different programs.
    for command in dict.fromkeys(c for c in scripts if c):
        eid = make_id(repo_id, f"{rel_path}#{command}")
        nodes.append(Node(id=eid, repo=repo_id, kind="entry_point", name=command,
                          file=rel_path, line_start=1))
        edges.append(Edge(src=file_id, dst=eid, relation="contains",
                          confidence=Confidence.EXTRACTED, provenance=prov))
    return nodes, edges
