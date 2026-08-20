"""XSLT (``.xsl`` / ``.xslt``) extraction -> the stylesheet's templates and what calls them.

A stylesheet is a program, and the question asked of it is the one asked of any program:
what is here, and what calls what. ``<xsl:call-template name="X"/>`` is a call by name, so a
stylesheet has a real call graph -- which the graph could not show, because the extensions
were routed nowhere.

**Why no new kinds.** Unlike :mod:`.xsd`, this module reuses ``function`` and
``global_variable``, and the difference is not inconsistency. The schema reference stream is
cross-domain and resolves on name alone, so a schema type sharing ``struct`` with C++ would
collide. The ``calls`` and ``uses`` streams are **same-language**: a candidate must belong to
the reference's own language family, and ``xsl`` is its own family, so an ``xsl:template``
named ``format`` cannot resolve onto a Python ``format``. The isolation is already there and
does not need a kind to express it.

**Two things a stylesheet has that this does not extract**, said plainly rather than left to
be discovered:

* ``<xsl:import>`` and ``<xsl:include>``. The target is a relative href, not a name, and the
  only honest edge would point at a file node that may not exist in the repository. A
  dangling edge is worse than a missing one.
* XPath calls to ``<xsl:function>``. The function is a node, but a call to it lives inside a
  ``select`` expression, and reading XPath needs an XPath parser rather than one more
  regular expression.

The scanner reasoning is :mod:`.xml_cfg`'s, and its masking helper is imported rather than
copied so that a commented-out template is recognised the same way in every XML extractor.
"""

from __future__ import annotations

import re

from .ids import make_id
from .model import Node
from .xml_cfg import mask_non_markup

#: A named template, a match template, or an ``xsl:function``. All three are ``function``
#: nodes: the language family filter is what keeps them apart from code, not the kind.
TEMPLATE_KIND = "function"

#: A top-level ``xsl:variable`` or ``xsl:param`` -- a stylesheet's configuration surface.
VARIABLE_KIND = "global_variable"

#: Every kind this module emits, declared here so the registry-parity test imports it
#: instead of carrying a list somebody has to remember to update.
EMITTED_KINDS = frozenset({TEMPLATE_KIND, VARIABLE_KIND})

# Bounds one generated stylesheet, matching the other XML extractors.
_MAX_NODES_PER_FILE = 2000

_TAG = re.compile(r"<(/?)([A-Za-z_][\w.:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_ATTR = re.compile(r"([A-Za-z_][\w.:-]*)\s*=\s*(\"([^\"]*)\"|'([^']*)')")

# `$name` in an attribute value: a variable or parameter being read. `$` is not otherwise
# meaningful in XPath, so this needs no context beyond the attribute it sits in.
_VAR_USE = re.compile(r"\$([A-Za-z_][\w.-]*)")


def _keyword(tag: str) -> str:
    """A tag reduced to its local part and folded, for matching XSLT's own element names.

    Folded because ``call-template`` is a fixed word in the language; a template's *name*
    is data and is never folded (see :func:`_name`).
    """
    return tag.rsplit(":", 1)[-1].strip().casefold()


def _name(value: str) -> str:
    """A name attribute's value, prefix stripped, case preserved.

    XML is case-sensitive, so ``Format`` and ``format`` are two templates. The namespace
    prefix on an ``xsl:function`` name is a per-file alias and comes off, exactly as in
    :mod:`.xsd`.
    """
    return value.rsplit(":", 1)[-1].strip()


def _attrs(raw: str) -> dict[str, str]:
    return {m.group(1).casefold(): (m.group(3) if m.group(3) is not None else m.group(4))
            for m in _ATTR.finditer(raw or "")}


def parse_xsl(
    repo_id: str, rel_path: str, source: bytes,
) -> tuple[list[Node], list[tuple[str, str, str, int]], list[tuple[str, str, str, int]]]:
    """Parse one stylesheet into (nodes, call references, variable-use references).

    A call or a variable read is attributed to the template it sits in, or to the
    declaration it sits on when that declaration is itself a node -- so a top-level
    ``<xsl:variable name="b" select="$a"/>`` records b reading a. A reference with neither
    is dropped, never attributed to the file node: see the comment on the call branch for
    why that would defeat the same-language filter this module relies on.
    """
    raw = (source.decode("utf-8", "replace")
           if isinstance(source, (bytes, bytearray)) else source)
    text = mask_non_markup(raw)

    nodes: list[Node] = []
    calls: list[tuple[str, str, str, int]] = []
    var_uses: list[tuple[str, str, str, int]] = []
    seen: dict[str, str] = {}
    path: list[str] = []
    # The template currently being read, and the depth its tag sits at. Templates do not
    # nest in XSLT, so one slot is enough; the depth is what tells a template's own close
    # tag from any other close tag.
    owner: str | None = None
    owner_depth = -1
    line = 1
    pos = 0

    def emit(kind: str, name: str, where: int, attrs: dict[str, str]) -> str | None:
        if not name or len(nodes) >= _MAX_NODES_PER_FILE:
            return None
        nid = make_id(repo_id, rel_path, kind, name)
        if nid in seen:
            # Ids are casefolded and XML names are not, so two templates differing only by
            # case arrive with one id. The second is skipped without becoming the owner,
            # so its calls are dropped rather than attributed to the first.
            return nid if seen[nid] == name else None
        seen[nid] = name
        nodes.append(Node(
            id=nid, repo=repo_id, kind=kind, name=name,
            qualified_name=f"{rel_path}::{name}", file=rel_path,
            line_start=where, line_end=where, lang="xsl", attrs=attrs))
        return nid

    for m in _TAG.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        closing, tag, attr_raw = m.group(1), m.group(2), m.group(3)
        self_closing = attr_raw.rstrip().endswith("/")
        local = _keyword(tag)

        if closing:
            if path:
                path.pop()
            if owner is not None and len(path) <= owner_depth:
                owner, owner_depth = None, -1
            continue

        attrs = _attrs(attr_raw)
        depth = len(path)
        # What this tag's references belong to. Usually the enclosing template; for a tag
        # that declares something, itself, so `<xsl:variable name="b" select="$a"/>` at the
        # top level records b reading a rather than dropping it.
        scope = owner

        if local in ("template", "function"):
            name = _name(attrs.get("name", ""))
            match = attrs.get("match", "").strip()
            node_attrs = {"xsl_construct": local}
            if match:
                node_attrs["xsl_match"] = match
            if attrs.get("mode"):
                node_attrs["xsl_mode"] = attrs["mode"]
            # A template may carry both; the name wins, because that is what a
            # `call-template` reaches it by. A match-only template is named after its
            # pattern instead -- not an identifier, but the only handle it has, and a
            # template with no handle at all cannot be pointed at from anywhere.
            scope = emit(TEMPLATE_KIND, name or match, line, node_attrs)
            # A self-closing template has no body, so it never becomes the owner of
            # anything after this tag -- but its own attributes are still its own.
            owner, owner_depth = (None, -1) if self_closing else (scope, depth)
        elif local in ("variable", "param") and depth == 1:
            # Top level only. A variable declared inside a template is a local, and a large
            # stylesheet has thousands of them.
            scope = emit(VARIABLE_KIND, _name(attrs.get("name", "")), line,
                         {"xsl_construct": local}) or owner
        elif local == "call-template":
            target = _name(attrs.get("name", ""))
            # Dropped rather than attributed to the file node when there is no enclosing
            # template. The file node carries no language, and `_resolve_name_refs` reads
            # the SOURCE node's language to apply the same-language filter -- so a
            # file-attributed call would have that filter disabled and could resolve onto a
            # same-named function in any language in the repository. That is the exact
            # collision the family filter exists to prevent. A `call-template` outside a
            # sequence constructor is malformed XSLT anyway.
            if target and scope is not None:
                calls.append((scope, target, rel_path, line))

        if scope is not None:
            for value in attrs.values():
                for use in _VAR_USE.finditer(value or ""):
                    var_uses.append((scope, use.group(1), rel_path, line))

        if not self_closing:
            path.append(local)

    return nodes, calls, var_uses
