"""XML configuration extraction (G4).

`.xml` was absent from the extension table and contributed zero nodes, so every
setting in a repo's configuration was invisible to the graph. These tests cover the
two extraction shapes, the element path, the secret screen, and the hostile-input
cases a line scanner exists to survive.
"""

from contextlake.kb.parse import XML_EXTS, is_indexable_name
from contextlake.kb.xml_cfg import CONFIG_KIND, parse_xml_config


def _by_name(nodes):
    return {n.name: n for n in nodes}


def test_extracts_key_value_attribute_pairs():
    nodes = parse_xml_config("r", "app.xml", b"""<configuration>
  <appSettings>
    <add key="Timeout" value="30"/>
    <add key="Retries" value="5"/>
  </appSettings>
</configuration>""")
    got = _by_name(nodes)
    assert got["Timeout"].attrs["value"] == "30"
    assert got["Retries"].attrs["value"] == "5"
    assert all(n.kind == CONFIG_KIND for n in nodes)


def test_extracts_leaf_element_text():
    nodes = parse_xml_config("r", "app.xml", b"<c><logging><Level>DEBUG</Level></logging></c>")
    assert _by_name(nodes)["Level"].attrs["value"] == "DEBUG"


def test_line_numbers_are_real():
    """The whole point of a scanner over a stdlib tree parser."""
    src = b"<c>\n  <appSettings>\n\n    <add key=\"Timeout\" value=\"30\"/>\n  </appSettings>\n</c>"
    assert _by_name(parse_xml_config("r", "a.xml", src))["Timeout"].line_start == 4


def test_qualified_name_is_the_element_path():
    nodes = parse_xml_config("r", "a.xml", b"""<configuration>
  <appSettings>
    <add key="Timeout" value="30"/>
  </appSettings>
</configuration>""")
    assert _by_name(nodes)["Timeout"].qualified_name == "configuration/appSettings/Timeout"


def test_self_closing_tags_do_not_corrupt_the_path():
    """Regression: `/` satisfies the attribute group's character class, so a greedy
    match swallowed it and every self-closing element was pushed onto the path
    stack. Because the stack only pops on a matching close tag, the corruption
    compounded down the file."""
    nodes = parse_xml_config("r", "a.xml", b"""<configuration>
  <appSettings>
    <add key="A" value="1"/>
    <add key="B" value="2"/>
  </appSettings>
  <logging>
    <Level>DEBUG</Level>
  </logging>
</configuration>""")
    got = _by_name(nodes)
    assert got["B"].qualified_name == "configuration/appSettings/B"
    assert got["Level"].qualified_name == "configuration/logging/Level"
    assert "add" not in got["Level"].qualified_name


def test_same_key_in_two_sections_stays_two_settings():
    nodes = parse_xml_config("r", "a.xml", b"""<c>
  <primary><add key="Host" value="a"/></primary>
  <backup><add key="Host" value="b"/></backup>
</c>""")
    quals = sorted(n.qualified_name for n in nodes)
    assert quals == ["c/backup/Host", "c/primary/Host"]


def test_commented_out_and_cdata_settings_are_not_extracted():
    nodes = parse_xml_config("r", "a.xml", b"""<c>
  <!-- <add key="Commented" value="x"/> -->
  <![CDATA[ <add key="InCdata" value="y"/> ]]>
  <add key="Live" value="z"/>
</c>""")
    assert [n.name for n in nodes] == ["Live"]


def test_masking_preserves_line_numbers():
    """A comment must blank its region without shifting the lines after it."""
    src = b"<c>\n<!-- a\nmultiline\ncomment -->\n<add key=\"K\" value=\"v\"/>\n</c>"
    assert _by_name(parse_xml_config("r", "a.xml", src))["K"].line_start == 5


class TestSecretsNeverReachTheStore:
    """The node is kept, the value is withheld: "a password is configured here, at
    this line" is the useful answer, and copying the password into the graph would
    put it in a second, less obvious place."""

    def test_secret_key_name_redacts(self):
        n, = parse_xml_config("r", "a.xml", b'<c><add key="DbPassword" value="hunter2"/></c>')
        assert "value" not in n.attrs and n.attrs["value_redacted"] is True
        assert n.name == "DbPassword"          # the node still exists

    def test_secret_in_the_value_under_an_innocent_name_redacts(self):
        n, = parse_xml_config("r", "a.xml",
                              b'<c><add name="Main" value="Server=x;Pwd=secret"/></c>')
        assert "value" not in n.attrs

    def test_token_shaped_value_redacts(self):
        n, = parse_xml_config("r", "a.xml", b'<c><add name="Api" value="Bearer ab.cd.ef"/></c>')
        assert "value" not in n.attrs

    def test_long_opaque_blob_redacts_on_shape_alone(self):
        n, = parse_xml_config(
            "r", "a.xml",
            b'<c><add name="B" value="dGhpc2lzYXZlcnlsb25nYmFzZTY0c3RyaW5nZm9ydGVzdA=="/></c>')
        assert "value" not in n.attrs

    def test_benign_url_is_kept(self):
        n, = parse_xml_config("r", "a.xml",
                              b'<c><add key="Url" value="https://example.com/api/v1/items"/></c>')
        assert n.attrs["value"] == "https://example.com/api/v1/items"


class TestHostileAndMalformedInput:
    """A strict parser raises and yields nothing for the whole file. Two-decade-old
    trees carry hand-edited XML, so degrading to a partial extraction is the
    behaviour worth having."""

    def test_malformed_input_never_raises(self):
        for bad in (b"<a><b>", b"not xml at all", b"", b"</close>", b"<a b='c\"d'>x</a>"):
            parse_xml_config("r", "f.xml", bad)   # must not raise

    def test_settings_before_a_broken_tag_still_extract(self):
        nodes = parse_xml_config("r", "a.xml",
                                 b'<c><add key="Good" value="1"/><unclosed></c>')
        assert "Good" in _by_name(nodes)

    def test_entities_are_never_expanded(self):
        """No entity expansion means a billion-laughs file is inert text, not a hang."""
        bomb = (b'<!DOCTYPE x [<!ENTITY a "aaaaaaaaaa">'
                b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                b'<c><add key="K" value="&b;"/></c>')
        n, = parse_xml_config("r", "a.xml", bomb)
        assert n.attrs["value"] == "&b;"          # literal, unexpanded

    def test_a_huge_file_is_bounded(self):
        src = b"<c>" + b"".join(
            b'<add key="K%d" value="v"/>' % i for i in range(5000)) + b"</c>"
        assert len(parse_xml_config("r", "a.xml", src)) <= 2000


def test_xml_is_wired_into_the_indexer():
    """The extractor is useless if the walker never hands it a file."""
    assert ".xml" in XML_EXTS
    assert is_indexable_name("app.xml", "conf/app.xml")


def test_xml_is_not_gated_by_the_languages_filter():
    """A setting is not written in a language anyone filters on, so
    `--languages python` must not hide a repo's configuration -- the same rule
    manifests and ADRs already follow."""
    from contextlake.kb.parse import _extension_filter, _file_kind
    allowed, hcl, sql = _extension_filter(["python"])
    assert _file_kind("app.xml", ".xml", "conf/app.xml",
                      allowed_exts=allowed, index_hcl=hcl, index_sql=sql) == "xml"
