"""Which XML-shaped extensions carry settings, and which deliberately do not.

E6's decided scope is "config keys and values only... so 'where is this setting
defined' resolves to a file and line", with an explicit warning against a scope
that "could add more nodes than all five C++ kinds combined and would dominate
the per-kind diagram budgets".

`.xml` alone missed the canonical .NET settings file. Measured across 660 real
repositories: 1,023 `.config` files contributing zero nodes.

The exclusions matter as much as the inclusions, so they are asserted here too.
An extension list is the kind of thing that grows by accident, and `.resx` alone
would have added roughly 91,000 nodes fleet-wide.
"""
from __future__ import annotations

import pytest

from contextlake.kb import parse
from contextlake.kb.xml_cfg import parse_xml_config

SETTINGS_FILE = (
    b'<configuration>\n'
    b'  <appSettings>\n'
    b'    <add key="Timeout" value="30"/>\n'
    b'  </appSettings>\n'
    b'</configuration>\n'
)


@pytest.mark.parametrize("ext", [".xml", ".config", ".props", ".targets",
                                ".settings", ".plist"])
def test_a_settings_extension_routes_to_the_config_extractor(ext):
    allowed, names, hcl, sql = parse._source_filter(None)
    kind = parse._file_kind(f"app{ext}", ext, f"app{ext}", allowed_exts=allowed,
                            allowed_names=names, index_hcl=hcl, index_sql=sql)
    assert kind == parse._XML, f"{ext} does not reach the config extractor"


@pytest.mark.parametrize("ext", [".resx", ".csproj", ".vbproj", ".nuspec", ".svg"])
def test_an_excluded_extension_is_not_treated_as_settings(ext):
    """Each exclusion has a measured reason, recorded beside XML_EXTS:
    `.resx` is localisation and would add ~91,000 nodes fleet-wide, project
    files are build definitions with their own manifest extractor, and `.svg`
    is graphics that merely happens to be XML-shaped."""
    assert ext not in parse.XML_EXTS


def test_a_dotnet_config_file_resolves_a_setting_to_its_line():
    """The question E6 exists to answer, on the file that asks it most."""
    nodes = parse_xml_config("probe", "web.config", SETTINGS_FILE)

    assert len(nodes) == 1
    node = nodes[0]
    assert node.kind == "config_key"
    assert node.name == "Timeout"
    assert node.attrs["value"] == "30"
    # A setting whose provenance is a file with no line is a half-citation.
    assert node.line_start == 3, "the line must be the one the setting is written on"


def test_a_file_with_a_settings_extension_that_is_not_xml_costs_nothing():
    """`.config` is not always XML. The extractor is a regex scanner over
    markup, so a non-XML file yields nothing rather than raising, which is why
    widening the extension list is safe."""
    assert parse_xml_config("probe", "app.config", b"key=value\nother=2\n") == []


def test_widening_the_list_did_not_disturb_the_sibling_families():
    """`.xsd` and `.xsl` are matched BEFORE XML_EXTS and must stay their own
    kinds; a stray addition here would silently reclassify them."""
    allowed, names, hcl, sql = parse._source_filter(None)

    def kind(fn):
        import os
        return parse._file_kind(fn, os.path.splitext(fn)[1].lower(), fn,
                                allowed_exts=allowed, allowed_names=names,
                                index_hcl=hcl, index_sql=sql)

    assert kind("schema.xsd") == parse._XSD
    assert kind("t.xsl") == parse._XSL
    assert kind("t.xslt") == parse._XSL
    assert not (parse.XML_EXTS & parse.XSD_EXTS)
    assert not (parse.XML_EXTS & parse.XSL_EXTS)
