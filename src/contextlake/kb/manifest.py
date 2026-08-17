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
from typing import NamedTuple

from .ids import make_id
from .model import PACKAGES_REPO, Confidence, Edge, Node, Provenance

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_DEP_NAME = re.compile(r"[A-Za-z0-9._-]+")
# The whole tag, then each attribute off it, because MSBuild writes `Include` and
# `Version` in either order and an `Include`-anchored pattern can only ever reach the
# attribute it is anchored on.
_PKG_REF = re.compile(r"<PackageReference\b[^<>]*>", re.IGNORECASE)
_XML_INCLUDE = re.compile(r'\bInclude\s*=\s*"([^"]*)"', re.IGNORECASE)
_XML_VERSION = re.compile(r'\bVersion\s*=\s*"([^"]*)"', re.IGNORECASE)
# MSBuild accepts the version as a CHILD element as well as an attribute. Reading only
# the attribute made a pinned dependency arrive with no constraint recorded, which the
# data contract reads as "the manifest pinned nothing" -- a wrong answer stated
# confidently, rather than a missing one.
_XML_VERSION_CHILD = re.compile(r"<Version>\s*([^<]*?)\s*</Version>", re.IGNORECASE)
_PKG_REF_END = re.compile(r"</PackageReference\s*>|<PackageReference\b", re.IGNORECASE)
_MVN_VERSION = re.compile(r"<version>\s*([^<\s][^<]*?)\s*</version>", re.IGNORECASE)
_MVN_SCOPE = re.compile(r"<scope>\s*([^<\s][^<]*?)\s*</scope>", re.IGNORECASE)
_MVN_OPTIONAL = re.compile(r"<optional>\s*true\s*</optional>", re.IGNORECASE)
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


class _Dep(NamedTuple):
    """One dependency as the manifest states it.

    ``constraint`` is the remainder of the spec **as written**, not a parsed range:
    ``>=1.9.0``, ``^4.17.1``, ``[redis]>=5.0``, or a whole environment marker. Nothing
    here decides whether a version satisfies it, so keeping the author's own text is
    both honest and useful, and empty means the manifest pinned nothing.

    ``group`` separates a hard runtime requirement from one a user opts into. Before
    this existed every group was flattened into one relation, so an extra that ships
    disabled by default was indistinguishable from a dependency the package cannot
    start without -- which is most of the point when the fact is being read as a
    recorded decision.
    """

    name: str
    constraint: str
    group: str  # runtime | dev | peer | optional:<extra>
    line: int


def _dep_split(spec: str) -> tuple[str, str] | None:
    """``"blinker>=1.9.0"`` -> ``("blinker", ">=1.9.0")``; ``None`` if there is no name."""
    spec = spec.strip()
    m = _DEP_NAME.match(spec)
    if not m:
        return None
    return m.group(0), spec[m.end():].strip()


def _line_starts(text: str) -> list[int]:
    """Offset of the first character of each line, for offset -> line lookups."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    return bisect.bisect_right(starts, offset)


def _locate(text: str, starts: list[int], needle: str, after: int = 0) -> int:
    """1-based line of ``needle`` at or after ``after``; 1 when it does not occur.

    A structured manifest is parsed by a real parser (tomllib, json) which does not
    report positions, so the line has to be recovered from the text. Two things make
    that recovery honest rather than merely plausible:

    **Quoted first.** The needle is looked for as a complete quoted string before it is
    looked for bare, because a bare name is a substring of every longer name around it.
    A package that depends on ``demo`` inside a project called ``demo-example-worker``
    matched the project's own ``name =`` line and cited it, which is worse than citing
    nothing: it is a precise-looking number pointing at a different fact. Measured on a
    real public tree, where a dependency declared on line 7 was reported at line 2.

    **From the group's own offset.** A package listed in two groups would otherwise
    resolve to the first group's line twice.
    """
    for form in (f'"{needle}"', f"'{needle}'", needle):
        at = text.find(form, after)
        if at >= 0:
            return _line_of(starts, at)
    return 1


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


def _maven_deps(text: str, starts: list[int]) -> list[_Dep]:
    """Every ``<dependency>`` block as a `_Dep`, with its own line.

    Maven states the group in the block rather than by which list it sits in, so
    ``<scope>test</scope>`` reads as `dev` and ``<optional>true</optional>`` as
    `optional`, to land on the same vocabulary the other ecosystems produce.
    """
    out = []
    for start, inner_start, inner_end, _end in _xml_blocks(
            text, _MVN_DEP_OPEN, _MVN_DEP_CLOSE):
        block = text[inner_start:inner_end]
        coord = _mvn_coord(block)
        if not coord:
            continue
        v = _MVN_VERSION.search(block)
        s = _MVN_SCOPE.search(block)
        scope = (s.group(1).strip().lower() if s else "compile")
        if _MVN_OPTIONAL.search(block):
            group = "optional"
        elif scope in ("test", "provided"):
            group = "dev"
        else:
            group = "runtime"
        out.append(_Dep(coord, v.group(1).strip() if v else "", group,
                        _line_of(starts, start)))
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
    deps: list[_Dep] = []
    text = content.decode("utf-8", "replace")
    starts = _line_starts(text)
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
        # (group, specs, offset to search from). An extra's specs are searched from the
        # extra's own key so two extras listing the same package cite different lines.
        groups: list[tuple[str, list, int]] = [
            ("runtime", list(proj.get("dependencies") or []), 0)]
        for extra, specs in (proj.get("optional-dependencies") or {}).items():
            # The EARLIEST occurrence of either spelling, not the later of the two:
            # taking the max starts the search past the specs whenever only one spelling
            # is present, and every lookup after that quietly falls back to line 1 -- a
            # wrong answer indistinguishable from "could not locate it".
            at = min([i for i in (text.find(f"{extra} = "), text.find(f'"{extra}"'))
                      if i >= 0], default=0)
            groups.append((f"optional:{extra}", list(specs), at))
        # PEP 735 `[dependency-groups]`, which is a sibling of `[project]` rather than a
        # key inside it, and is a different mechanism from an extra: a group is local to
        # the checkout and never published in the package's metadata. Kept as its own
        # prefix for that reason. Not reading it meant a large application that declares
        # every dependency this way reported NONE, measured on a public Django tree.
        for gname, specs in (data.get("dependency-groups") or {}).items():
            at = min([i for i in (text.find(f"{gname} = "), text.find(f'"{gname}"'))
                      if i >= 0], default=0)
            groups.append((f"group:{gname}", list(specs), at))
        for group, specs, after in groups:
            for spec in specs:
                if not isinstance(spec, str) or (split := _dep_split(spec)) is None:
                    continue
                name, constraint = split
                deps.append(_Dep(name, constraint, group, _locate(text, starts, spec, after)))
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
        for section, group in (("dependencies", "runtime"),
                               ("devDependencies", "dev"),
                               ("peerDependencies", "peer")):
            block = data.get(section) or {}
            if not isinstance(block, dict):
                continue
            after = max(text.find(f'"{section}"'), 0)
            for name, constraint in block.items():
                deps.append(_Dep(name, constraint if isinstance(constraint, str) else "",
                                 group, _locate(text, starts, name, after)))
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
        for m in _PKG_REF.finditer(text):
            inc = _XML_INCLUDE.search(m.group(0))
            if inc is None:  # a PackageReference with no Include names nothing
                continue
            ver = _XML_VERSION.search(m.group(0))
            constraint = ver.group(1) if ver else ""
            if not constraint and not m.group(0).rstrip().endswith("/>"):
                # Not self-closing, so the version may be a child element instead. Look
                # only as far as this reference's own end, or the start of the next one
                # if it was never closed, so a version never migrates between packages.
                end = _PKG_REF_END.search(text, m.end())
                child = _XML_VERSION_CHILD.search(
                    text, m.end(), end.start() if end else len(text))
                constraint = child.group(1) if child else ""
            deps.append(_Dep(inc.group(1), constraint, "runtime",
                             _line_of(starts, m.start())))
    elif fname == "pom.xml":
        ecosystem = "maven"
        published = _maven_project_coord(text)
        deps = _maven_deps(text, starts)
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
        pn = _package_node(dep.name, ecosystem)
        nodes.append(pn)
        # A per-dependency provenance, not the file-level one: the shared `prov` above
        # cited line 1 for every dependency in the manifest, so a citation named the
        # file and nothing else, while every other citation in the product names a line.
        attrs = {"group": dep.group}
        if dep.constraint:
            attrs["constraint"] = dep.constraint
        edges.append(Edge(
            src=file_id, dst=pn.id, relation="depends_on",
            confidence=Confidence.EXTRACTED, attrs=attrs,
            provenance=Provenance(source_file=rel_path, source_line=dep.line,
                                  verified_at=verified_at)))
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
