"""XML Schema support: the schema's global components and the names they reference.

`.xsd` was routed nowhere. Sent through the XML config scanner instead it would have been
worse than nothing: `name` is one of that scanner's key attributes, so every
`<xs:element name="Order">` would have been filed as a setting, in the kind a user filters
out when looking for settings.

The tests that matter most here are the two that are not about extraction at all: that a
reference and its definition in DIFFERENT files land on one node, and that a schema name and
a same-named code symbol stay in separate name indexes.
"""

from __future__ import annotations

from contextlake.kb.kinds import KIND_REGISTRY
from contextlake.kb.parse import (
    _SCHEMA_KINDS,
    XSD_EXTS,
    RefCollector,
    _file_kind,
    is_indexable_name,
)
from contextlake.kb.xml_cfg import parse_xml_config
from contextlake.kb.xsd import EMITTED_KINDS, parse_xsd

ORDERS = b"""<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:tns="urn:example:orders"
           xmlns:cmn="urn:example:common"
           targetNamespace="urn:example:orders">
  <xs:import namespace="urn:example:common" schemaLocation="Common.xsd"/>
  <!-- <xs:complexType name="RetiredType"/> -->
  <xs:element name="OrderRequest" type="tns:OrderRequestType"/>
  <xs:complexType name="OrderRequestType">
    <xs:sequence>
      <xs:element name="Id" type="xs:string"/>
      <xs:element name="Party" type="cmn:PartyType"/>
      <xs:element ref="tns:Note" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="Note" type="xs:string"/>
  <xs:simpleType name="CurrencyCode">
    <xs:restriction base="xs:string"/>
  </xs:simpleType>
  <xs:group name="AuditGroup">
    <xs:sequence><xs:element name="At" type="xs:dateTime"/></xs:sequence>
  </xs:group>
  <xs:attributeGroup name="CommonAttrs">
    <xs:attribute name="lang" type="xs:string"/>
  </xs:attributeGroup>
  <xs:attribute name="Locale" type="tns:CurrencyCode"/>
</xs:schema>
"""

COMMON = b"""<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:example:common">
  <xs:complexType name="PartyType">
    <xs:sequence><xs:element name="Name" type="xs:string"/></xs:sequence>
  </xs:complexType>
</xs:schema>
"""


def _by_name(nodes):
    return {n.name: n for n in nodes}


def _targets(refs):
    return {t for _src, t, _rel, _line in refs}


# --- what becomes a node ---------------------------------------------------------------

def test_every_global_component_kind_is_extracted():
    nodes, _ = parse_xsd("demo", "orders/Order.xsd", ORDERS)
    got = {(n.name, n.kind, n.attrs.get("schema_construct")) for n in nodes}
    for expected in [
        ("OrderRequest", "schema_element", "element"),
        ("OrderRequestType", "schema_type", "complex"),
        ("CurrencyCode", "schema_type", "simple"),
        ("AuditGroup", "schema_type", "group"),
        ("CommonAttrs", "schema_type", "attribute_group"),
        ("Locale", "schema_type", "attribute"),
    ]:
        assert expected in got, f"{expected} missing from {sorted(got)}"


def test_a_locally_scoped_element_is_not_a_node():
    """`Id`, `Party`, `At` and `lang` are nested. Nothing can refer to them by name, and
    minting them would put thousands of identical `Id` nodes into one schema set."""
    nodes, _ = parse_xsd("demo", "orders/Order.xsd", ORDERS)
    names = set(_by_name(nodes))
    assert names.isdisjoint({"Id", "Party", "At", "lang", "Name"}), sorted(names)


def test_a_commented_out_definition_is_not_extracted():
    nodes, _ = parse_xsd("demo", "orders/Order.xsd", ORDERS)
    assert "RetiredType" not in _by_name(nodes)


def test_the_name_keeps_its_case():
    """XML is case-sensitive where SQL is not. A folded name would both merge two legal,
    distinct components and display a spelling that appears nowhere in the file."""
    nodes, _ = parse_xsd("demo", "orders/Order.xsd", ORDERS)
    assert "OrderRequestType" in _by_name(nodes)
    assert "orderrequesttype" not in _by_name(nodes)


def test_a_line_number_is_recorded_for_every_component():
    nodes, _ = parse_xsd("demo", "orders/Order.xsd", ORDERS)
    assert nodes, "no components extracted"
    for n in nodes:
        assert n.line_start >= 1
        assert ORDERS.decode().splitlines()[n.line_start - 1].strip().startswith("<")


# --- what becomes a reference ----------------------------------------------------------

def test_the_namespace_prefix_is_stripped_from_a_reference():
    """`tns:OrderRequestType` and `cmn:PartyType` are per-file aliases for namespace URIs.
    Keeping the prefix would split one component into a node per spelling."""
    _nodes, refs = parse_xsd("demo", "orders/Order.xsd", ORDERS)
    assert {"OrderRequestType", "PartyType", "Note"} <= _targets(refs)
    assert not any(":" in t for t in _targets(refs))


def test_a_builtin_datatype_is_not_a_reference():
    """`xs:string` names nothing this graph holds. Letting it through would attach every
    element in the fleet to any repository defining a component called `string`."""
    _nodes, refs = parse_xsd("demo", "orders/Order.xsd", ORDERS)
    assert _targets(refs).isdisjoint({"string", "dateTime", "String", "anyURI", "NMTOKEN"})


def test_a_reference_is_attributed_to_the_enclosing_global_component():
    nodes, refs = parse_xsd("demo", "orders/Order.xsd", ORDERS)
    owner = _by_name(nodes)["OrderRequestType"].id
    party = [r for r in refs if r[1] == "PartyType"]
    assert len(party) == 1, party
    assert party[0][0] == owner


def test_membertypes_is_read_as_a_list():
    src = b"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:simpleType name="Either">
        <xs:union memberTypes="tns:Alpha tns:Beta"/>
      </xs:simpleType>
    </xs:schema>"""
    _nodes, refs = parse_xsd("demo", "u.xsd", src)
    assert _targets(refs) == {"Alpha", "Beta"}


def test_a_component_naming_itself_is_not_a_reference():
    src = b"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="Tree">
        <xs:sequence><xs:element name="Child" type="tns:Tree"/></xs:sequence>
      </xs:complexType>
    </xs:schema>"""
    _nodes, refs = parse_xsd("demo", "t.xsd", src)
    assert refs == [], refs


def test_a_self_closing_global_component_does_not_own_the_next_ones_references():
    """A self-closing tag is never pushed onto the element path, so nothing closes it. The
    owner is therefore cleared on every global tag, not only on the ones that mint a node.

    The middle declaration is INVALID -- a global element carries `name`, never `ref` --
    and that is the point: this module exists to degrade on the hand-edited schemas a
    two-decade-old tree contains, and it is exactly there that the guard is reachable.
    Without it, `Stray` is recorded as something `Alpha` refers to.

    A first version of this fixture used `<xs:notation public="...">`, whose attribute is
    not one this module reads at all, so the mutation that removes the guard still passed.
    The fixture was the bug, not the guard.
    """
    src = b"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="Alpha" type="tns:AlphaType"/>
      <xs:element ref="tns:Stray"/>
      <xs:element name="Gamma" type="tns:GammaType"/>
    </xs:schema>"""
    nodes, refs = parse_xsd("demo", "s.xsd", src)
    alpha = _by_name(nodes)["Alpha"].id
    assert {(src_id, t) for src_id, t, _r, _l in refs} == {
        (alpha, "AlphaType"),
        (_by_name(nodes)["Gamma"].id, "GammaType"),
    }


def test_two_components_colliding_only_by_case_do_not_share_references():
    """Node ids are casefolded and XML names are not, so `Order` and `order` -- both legal
    and distinct -- arrive with one id. The second is skipped without an owner, so its
    references are dropped rather than handed to the first."""
    src = b"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="Order"><xs:sequence>
        <xs:element name="A" type="tns:First"/></xs:sequence></xs:complexType>
      <xs:complexType name="order"><xs:sequence>
        <xs:element name="B" type="tns:Second"/></xs:sequence></xs:complexType>
    </xs:schema>"""
    nodes, refs = parse_xsd("demo", "c.xsd", src)
    assert len(nodes) == 1, [n.name for n in nodes]
    assert _targets(refs) == {"First"}, refs


# --- the cross-file constraint ---------------------------------------------------------

def test_a_reference_and_its_definition_in_different_files_reach_one_node():
    """The constraint the whole normalisation exists for. `cmn:PartyType` is referenced in
    one file and defined in another; the resolved edge must land on exactly one node."""
    a_nodes, a_refs = parse_xsd("demo", "orders/Order.xsd", ORDERS)
    b_nodes, b_refs = parse_xsd("demo", "common/Common.xsd", COMMON)

    refs = RefCollector()
    refs.schema.extend(a_refs + b_refs)
    by_id = {n.id: n for n in a_nodes + b_nodes}
    edges = refs.resolved_edges(by_id)

    party = [e for e in edges if by_id[e.dst].name == "PartyType"]
    assert len(party) == 1, party
    assert len({e.dst for e in party}) == 1
    assert by_id[party[0].dst].file == "common/Common.xsd"
    assert by_id[party[0].src].name == "OrderRequestType"


def test_a_schema_reference_cannot_resolve_onto_a_code_symbol():
    """The reason these kinds exist. Resolution is by name across the repo and narrowed
    only by target kind, so sharing `struct`/`typedef` with C++ would let `type="Address"`
    resolve onto an unrelated struct -- confidently, with nothing reporting the guess."""
    from contextlake.kb.model import Node

    schema = b"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="Shipment" type="tns:Address"/>
    </xs:schema>"""
    nodes, refs_out = parse_xsd("demo", "s.xsd", schema)
    decoy = Node(id="demo_addr_h_struct_address", repo="demo", kind="struct",
                 name="Address", file="src/addr.h", line_start=1, lang="cpp")

    refs = RefCollector()
    refs.schema.extend(refs_out)
    by_id = {n.id: n for n in [*nodes, decoy]}
    assert refs.resolved_edges(by_id) == []


# --- routing and registration ----------------------------------------------------------

def test_xsd_routes_to_its_own_extractor_and_not_the_config_scanner():
    kind = _file_kind("Order.xsd", ".xsd", "schemas/Order.xsd",
                      allowed_exts=set(), allowed_names=set(),
                      index_hcl=True, index_sql=True)
    assert kind == "xsd"
    assert ".xsd" in XSD_EXTS
    assert is_indexable_name("Order.xsd", "schemas/Order.xsd")


def test_the_config_scanner_would_have_filed_a_schema_as_settings():
    """Not a test of `.xsd` routing but of why the routing had to exist: `name` is one of
    the config scanner's key attributes, so every component becomes a `config_key`."""
    keys = parse_xml_config("demo", "orders/Order.xsd", ORDERS)
    assert any(n.name == "OrderRequest" and n.kind == "config_key" for n in keys)


def test_both_kinds_are_registered_and_are_schema_reference_targets():
    for kind in EMITTED_KINDS:
        assert kind in KIND_REGISTRY, f"{kind} is produced but not registered"
        assert KIND_REGISTRY[kind].schema_ref_target, kind
        assert not KIND_REGISTRY[kind].sql_ref_target, f"{kind} must not share SQL's index"
    assert _SCHEMA_KINDS == set(EMITTED_KINDS)
