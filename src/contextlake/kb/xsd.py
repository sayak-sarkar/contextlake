"""XML Schema (``.xsd``) extraction -> the schema's named components and what they name.

An ``.xsd`` file answers a different question from an ``.xml`` config file, so it gets a
different extractor. Sent through :mod:`.xml_cfg` it would produce a ``config_key`` for every
``<xs:element name="Order">``, because ``name`` is one of that module's key attributes: a
message definition filed as a setting, in the kind a user filters *out* when looking for
settings. One extension, one extractor.

**Why the name index is its own.** Reference resolution in :mod:`.parse` is repo-wide and by
NAME, narrowed only by target kind; the language-family filter applies to the call and
inheritance streams, not to the cross-domain ones. ``table`` and ``view`` are safe to resolve
that way because no code kind is called either. A schema type is not so lucky: reusing
``struct`` and ``typedef`` would let ``type="tns:Address"`` resolve, confidently and with no
warning, onto a C++ ``struct Address`` in an unrelated directory. So schema components carry
two kinds of their own, :data:`TYPE_KIND` and :data:`ELEMENT_KIND`, and their reference stream
resolves to those two and nothing else.

**Why a scanner and not an XML parser.** The same three reasons :mod:`.xml_cfg` gives, and
they apply here with more force, not less: entity expansion is a denial-of-service on
untrusted mirrored input, a two-decade-old schema set contains hand-edited files a strict
parser abandons whole, and the stdlib tree parsers report no line numbers. The masking
helper is imported from :mod:`.xml_cfg` rather than copied, so a comment is recognised the
same way in both.

Scope is the schema's GLOBAL components -- those whose parent is ``<xs:schema>`` itself --
and the names they reference. A locally-scoped element inside a complex type is not a node:
it has no name anything else can refer to, and minting one would put the same
``<xs:element name="Id"/>`` into the graph a few thousand times over.
"""

from __future__ import annotations

import re

from .ids import make_id
from .model import Node
from .xml_cfg import mask_non_markup

#: A global ``complexType``, ``simpleType``, ``group``, ``attributeGroup`` or ``attribute``.
#: One kind rather than five, because which one it is answers a question about the schema
#: language and not about the graph; it is recorded as the ``schema_construct`` attribute.
TYPE_KIND = "schema_type"

#: A global ``xs:element``. Separate from the types because it is the name a document, a
#: message or a SOAP body is actually called, which is the name a person searches for.
ELEMENT_KIND = "schema_element"

#: Every kind this module emits. Imported by the registry-parity test, so adding a kind here
#: cannot leave the test asserting a list somebody remembered to update.
EMITTED_KINDS = frozenset({TYPE_KIND, ELEMENT_KIND})

# Bounds one generated or machine-exported schema, for the reason `.xml_cfg` gives: an
# industry message schema set runs to thousands of types and must not dominate the graph.
_MAX_NODES_PER_FILE = 2000

_TAG = re.compile(r"<(/?)([A-Za-z_][\w.:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_ATTR = re.compile(r"([A-Za-z_][\w.:-]*)\s*=\s*(\"([^\"]*)\"|'([^']*)')")

# Local names of the global components worth a node, mapped to what they are. A global
# `attribute` is here for the same reason a group is: something else reaches it by `ref=`,
# and a reference whose target was never minted resolves to nothing at all.
_GLOBAL_TYPES = {
    "complextype": "complex",
    "simpletype": "simple",
    "group": "group",
    "attributegroup": "attribute_group",
    "attribute": "attribute",
}

# Attributes that name another schema component. `memberTypes` is space-separated and is
# handled apart from the rest; the others carry exactly one name.
_REF_ATTRS = ("type", "base", "ref", "itemtype")
_LIST_REF_ATTRS = ("membertypes",)

# The built-in schema datatypes. A `type="xs:string"` is not a reference to anything this
# graph holds, and letting those through would attach every element in the fleet to any
# unlucky repository that happened to define a component called `string` or `date`.
_BUILTINS = frozenset({
    "string", "boolean", "decimal", "float", "double", "duration", "datetime", "time",
    "date", "gyearmonth", "gyear", "gmonthday", "gday", "gmonth", "hexbinary",
    "base64binary", "anyuri", "qname", "notation", "normalizedstring", "token",
    "language", "nmtoken", "nmtokens", "name", "ncname", "id", "idref", "idrefs",
    "entity", "entities", "integer", "nonpositiveinteger", "negativeinteger", "long",
    "int", "short", "byte", "nonnegativeinteger", "unsignedlong", "unsignedint",
    "unsignedshort", "unsignedbyte", "positiveinteger", "anytype", "anysimpletype",
})


def _local(name: str) -> str:
    """A qualified XML name reduced to its local part, case PRESERVED.

    The prefix is a per-file alias for a namespace URI, so ``tns:Address``, ``ord:Address``
    and a bare ``Address`` in three files are one name. Stripping it is what makes a
    definition and a reference in a DIFFERENT file land on one node; keeping it would split
    the same component into one node per prefix spelling.

    Case is kept because XML is case-sensitive and SQL is not. Folding it here -- which an
    earlier draft did, by symmetry with :mod:`.sql` -- would merge ``OrderId`` and
    ``orderId`` into one node while displaying a name that appears nowhere in the schema.
    """
    return name.rsplit(":", 1)[-1].strip()


def _keyword(tag: str) -> str:
    """A tag reduced to its local part and folded, for matching the schema's own keywords.

    Folded where :func:`_local` is not, and the asymmetry is deliberate: ``complexType`` is
    a fixed word in the XSD grammar, so folding can only add tolerance for a spelling no
    valid schema uses, whereas a component's name is data and folding it loses information.
    """
    return tag.rsplit(":", 1)[-1].strip().casefold()


def _attrs(raw: str) -> dict[str, str]:
    return {m.group(1).casefold(): (m.group(3) if m.group(3) is not None else m.group(4))
            for m in _ATTR.finditer(raw or "")}


def parse_xsd(
    repo_id: str, rel_path: str, source: bytes,
) -> tuple[list[Node], list[tuple[str, str, str, int]]]:
    """Parse one ``.xsd`` file into (global component nodes, unresolved reference tuples).

    A reference is attributed to the innermost GLOBAL component enclosing it, so a
    ``type=`` on an element nested four levels inside a complex type is recorded as that
    complex type naming the other one. Attributing it to the nested element instead would
    need a node for the nested element, which this module deliberately does not mint.
    """
    raw = (source.decode("utf-8", "replace")
           if isinstance(source, (bytes, bytearray)) else source)
    text = mask_non_markup(raw)

    nodes: list[Node] = []
    refs: list[tuple[str, str, str, int]] = []
    seen: dict[str, str] = {}
    # Every open tag, innermost last. Global components are the ones whose parent is the
    # single `schema` root, so depth is read off this rather than counted separately.
    path: list[str] = []
    # The global component currently being read. A stack is not needed: XSD forbids a
    # global component inside another one.
    owner: str | None = None
    owner_name: str = ""
    line = 1
    pos = 0

    def emit(kind: str, name: str, where: int, attrs: dict[str, str]) -> str | None:
        if not name or len(nodes) >= _MAX_NODES_PER_FILE:
            return None
        nid = make_id(repo_id, rel_path, kind, name)
        if nid in seen:
            # Node ids are casefolded (`ids.normalize_id`), and XML names are not. So
            # `Order` and `order` -- both legal, and different components -- arrive here
            # with one id. Returning it would hand the second component's references to
            # the first, which is a wrong edge stated confidently. The second is skipped
            # WITHOUT an owner instead, so its references are dropped rather than
            # misattributed. A repeated declaration of the SAME name is a genuine
            # duplicate and keeps the node it already has.
            return nid if seen[nid] == name else None
        seen[nid] = name
        nodes.append(Node(
            id=nid, repo=repo_id, kind=kind, name=name,
            qualified_name=f"{rel_path}::{name}", file=rel_path,
            line_start=where, line_end=where, lang="xsd", attrs=attrs))
        return nid

    for m in _TAG.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        closing, tag, attr_raw = m.group(1), m.group(2), m.group(3)
        # `/` satisfies the attribute group's `[^>"']`, so a greedy match swallows it and a
        # trailing `(/?)` group in the pattern is always empty. Decided from the text
        # instead, exactly as `.xml_cfg` does and for the same bug it records.
        self_closing = attr_raw.rstrip().endswith("/")
        local = _keyword(tag)

        if closing:
            if path and path[-1] == local:
                path.pop()
            # The owner ends with the element that opened it, never with a sibling: a
            # complex type and the element after it are both at depth 1, and clearing on
            # any close would hand the second one the first one's references.
            if owner is not None and len(path) <= 1:
                owner, owner_name = None, ""
            continue

        attrs = _attrs(attr_raw)
        # Depth 1 is a direct child of `<xs:schema>`, which is what "global" means here.
        is_global = len(path) == 1 and path[0] == "schema"
        name = attrs.get("name")

        if is_global:
            # Cleared FIRST, and on every global tag rather than only on the ones that mint
            # a node. A self-closing global component is never pushed onto the path, so
            # nothing closes it: without this, `<xs:element name="A" type="AType"/>`
            # followed by any other global tag carrying a `type=` recorded that second
            # type as something A refers to.
            owner, owner_name = None, ""
            if name and local == "element":
                owner_name = _local(name)
                owner = emit(ELEMENT_KIND, owner_name, line, {"schema_construct": "element"})
            elif name and local in _GLOBAL_TYPES:
                owner_name = _local(name)
                owner = emit(
                    TYPE_KIND, owner_name, line,
                    {"schema_construct": _GLOBAL_TYPES[local]})

        if owner is not None:
            targets = [attrs.get(a, "") for a in _REF_ATTRS]
            targets += [t for a in _LIST_REF_ATTRS for t in (attrs.get(a) or "").split()]
            for token in targets:
                target = _local(token)
                # A component naming itself is a recursive definition, not a dependency on
                # something else; an edge from a node to itself is noise in every consumer
                # that reads degree.
                # The builtin check folds where the name does not: `xs:dateTime` is a fixed
                # word in the schema language, so it is matched the way `_keyword` matches
                # a tag. Comparing the unfolded name let every camel-cased builtin --
                # `dateTime`, `hexBinary`, `anyURI`, `NMTOKEN` -- through as a reference.
                if (target and target.casefold() not in _BUILTINS
                        and target != owner_name):
                    refs.append((owner, target, rel_path, line))

        if not self_closing:
            path.append(local)

    return nodes, refs
