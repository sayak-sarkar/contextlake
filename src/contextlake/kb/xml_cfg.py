"""XML configuration extraction -> one node per configured setting.

Answers "where is this setting defined", which on a mature tree is asked far more
often than any question about the XML's own structure. Before this, ``.xml`` was
absent from :data:`~contextlake.kb.parse.LANG_BY_EXT` and contributed **zero**
nodes: measured on a large legacy C++ tree, 181 ``.xml`` files produced nothing at
all, so every setting in them was invisible to the graph.

**Why a line scanner and not a real XML parser.** Three independent reasons, and
any one of them would be enough:

* **Entity expansion.** ``xml.etree`` and ``xml.dom`` both expand entities, so a
  repository containing a billion-laughs file would hang or exhaust the indexer.
  contextlake parses whatever a mirror happens to clone, which makes every input
  untrusted by construction. This module never expands anything; a ``&foo;`` is
  bytes in a value. (It is also why bandit's S314/S405 do not fire here.)
* **Malformed input is normal.** Two-decade-old trees carry hand-edited XML with
  unclosed tags and stray markup. A strict parser raises and yields nothing for
  the whole file; a scanner degrades to extracting the parts it understands, which
  is the behaviour the rest of this package already prefers (see :mod:`.sql`,
  whose docstring records the same trade-off against tree-sitter).
* **Line numbers.** The stdlib tree parsers do not report them, and a config node
  whose provenance is a file with no line is exactly the half-citation this
  release removed from the callers verbs.

Scope is deliberately keys and values, not structure. No edges: an element named
after a class is not a verified reference, and minting one would break the same
never-speculate rule :mod:`.adr` cites.
"""

from __future__ import annotations

import re

from .ids import make_id
from .model import Node

# A setting node. Registered in the dashboard's kind vocabulary and legend --
# an unknown kind renders with a file icon and cannot be filtered (G13).
CONFIG_KIND = "config_key"

# Values are evidence, not payload: enough to recognise the setting, never enough
# to turn the graph into a copy of the file. A connection string or a licence blob
# is exactly the kind of thing that should stay in the file it lives in.
_MAX_VALUE = 200

# Bounds a single generated or exported XML file. An in-house code generator
# emitting a 40k-entry catalogue must not dominate the graph, and past incidents
# in this package were all unbounded work over one pathological file.
_MAX_NODES_PER_FILE = 2000

# Regions whose contents are not markup and must not mint settings or move the
# element path: comments, CDATA, doctype/processing instructions.
_MASKED = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<!DOCTYPE[^>]*>",
                     re.DOTALL | re.IGNORECASE)

_TAG = re.compile(r"<(/?)([A-Za-z_][\w.:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")

# A value is never stored when the setting's NAME says it is a credential. This is
# deliberate, not incidental: contextlake writes its store to disk and serves it
# over MCP, so a password lifted out of a config file into the graph is a secret
# copied into a second, less obvious place. The node still exists -- "there is a
# password configured here, at this line" is exactly the useful, safe answer.
_SECRETISH = re.compile(
    r"pass|pwd|secret|token|credential|apikey|api_key|private|salt|cert|"
    r"connectionstring|conn_?str", re.I)

# The name is not sufficient on its own. `<add name="Main" value="Server=x;Pwd=y"/>`
# has an entirely innocent name and a credential in the value, so the value is
# screened as well -- for an embedded `secret=`-style assignment, and for a bearer
# token or key blob, which has no giveaway word at all and can only be recognised
# by shape (long, unbroken, mixed-case-and-digits).
_SECRET_VALUE = re.compile(
    r"(?:pass(?:word)?|pwd|secret|token|api[_-]?key|auth)\s*[=:]|"
    r"\b(?:bearer|basic)\s+\S+|"
    r"\b[A-Za-z0-9+/_-]{40,}={0,2}\b", re.I)
_ATTR = re.compile(r"([A-Za-z_][\w.:-]*)\s*=\s*(\"([^\"]*)\"|'([^']*)')")
# <Timeout>30</Timeout> -- a leaf element carrying its value as text.
_LEAF = re.compile(r"<([A-Za-z_][\w.:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>"
                   r"([^<>]*?)</\1\s*>")

# The conventional "this attribute names the setting" attributes, in priority
# order. `<add key="Timeout" value="30"/>` is the .NET/Ant/Spring idiom and the
# reason a plain tag-name key would be wrong: every such element is named `add`,
# so keying on the tag would collapse a whole settings block into one name.
_KEY_ATTRS = ("key", "name", "id", "property", "setting")
_VALUE_ATTRS = ("value", "val", "default")


def _mask(text: str) -> str:
    """Blank out non-markup regions, preserving every newline.

    Line numbers are the point of this module, so the mask must be
    length-and-newline preserving rather than a delete.
    """
    def blank(m: re.Match) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in m.group(0))
    return _MASKED.sub(blank, text)


def _attrs(raw: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4))
            for m in _ATTR.finditer(raw or "")}


def _clean(v: str) -> str:
    return " ".join(v.split())[:_MAX_VALUE]


def _pick(attrs: dict[str, str], names: tuple[str, ...]) -> str | None:
    for n in names:
        if attrs.get(n):
            return attrs[n]
    return None


def parse_xml_config(repo_id: str, rel_path: str, source: bytes) -> list[Node]:
    """Parse one ``.xml`` file into ``config_key`` nodes, or ``[]``.

    Emits a node for each setting it can identify two ways: a key/value attribute
    pair (``<add key="X" value="Y"/>``) and a single-line leaf element carrying
    text (``<Timeout>30</Timeout>``). ``qualified_name`` is the element path, so
    the same key under two different sections stays two distinct settings.
    """
    raw = (source.decode("utf-8", "replace")
           if isinstance(source, (bytes, bytearray)) else source)
    text = _mask(raw)

    nodes: list[Node] = []
    seen: set[str] = set()
    # Path is tracked across the whole file, so a key deep in a settings tree
    # records where it lives rather than just its own tag.
    path: list[str] = []
    line = 1
    pos = 0

    def emit(key: str, value: str | None, where: int, parent: list[str]) -> None:
        name = _clean(key)
        if not name or len(nodes) >= _MAX_NODES_PER_FILE:
            return
        qual = "/".join([*parent, name])
        nid = make_id(repo_id, rel_path, CONFIG_KIND, qual)
        if nid in seen:
            return
        seen.add(nid)
        attrs = {"config_format": "xml"}
        if value and not _SECRETISH.search(name) and not _SECRET_VALUE.search(value):
            attrs["value"] = _clean(value)
        elif value:
            # Recorded as redacted rather than simply absent, so "this setting has
            # no value" and "this setting's value was withheld" stay distinguishable.
            attrs["value_redacted"] = True
        nodes.append(Node(
            id=nid, repo=repo_id, kind=CONFIG_KIND, name=name, qualified_name=qual,
            file=rel_path, line_start=where, line_end=where, lang="xml", attrs=attrs,
        ))

    # Leaf elements are matched over the whole text first: the tag walk below
    # cannot see an element's text content without buffering, and a leaf is the
    # one case where the value sits between the tags rather than inside them.
    leaves: dict[int, list[tuple[str, str]]] = {}
    # Line numbers accumulate from the previous match rather than counting
    # newlines from the start of the file each time. `finditer` yields matches in
    # increasing order, so this is O(n) over the file; the naive
    # `text.count("\n", 0, m.start())` is O(n) PER MATCH, which is O(n^2) on a
    # data-shaped XML file with thousands of leaf elements. Measured on a 26 MB
    # lookup file that cost 42s of a 81s index; this is the same quadratic-scan
    # shape already fixed twice in this package (manifest pom.xml, parse_hcl).
    leaf_line, leaf_pos = 1, 0
    for m in _LEAF.finditer(text):
        leaf_line += text.count("\n", leaf_pos, m.start())
        leaf_pos = m.start()
        value = m.group(3).strip()
        if not value:
            continue
        leaves.setdefault(leaf_line, []).append((m.group(1), value))

    for m in _TAG.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        closing, tag, attr_raw = m.group(1), m.group(2), m.group(3)
        # Self-closing is decided HERE and not by a trailing `(/?)` group in the
        # pattern: `/` satisfies the attribute group's `[^>"']`, so a greedy match
        # swallows it and the group is always empty. That silently pushed every
        # `<add key=.. />` onto the path, and because the stack only pops on a
        # matching close tag, the corruption compounded down the file --
        # `appSettings/add/add/logging/Level` instead of `logging/Level`.
        self_closing = attr_raw.rstrip().endswith("/")

        if closing:
            if path and path[-1] == tag:
                path.pop()
            continue

        attrs = _attrs(attr_raw)
        key = _pick(attrs, _KEY_ATTRS)
        if key:
            emit(key, _pick(attrs, _VALUE_ATTRS), line, path)
        else:
            for leaf_tag, leaf_value in leaves.get(line, ()):
                if leaf_tag == tag:
                    emit(tag, leaf_value, line, path)
                    break

        if not self_closing:
            path.append(tag)

    return nodes
