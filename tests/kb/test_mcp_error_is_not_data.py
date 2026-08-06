"""An MCP error result must not be mistaken for data.

The defect these pin: `_parse_result` returned the error *text* for a failed tool
call, so a caller iterating the result found a string, yielded nothing, and
reported an empty answer. Live symptom was an Atlassian source reporting
"0 site(s) reachable" -- which reads as a permissions problem -- when the real
cause was elsewhere entirely.
"""

from __future__ import annotations

import pytest

from contextlake.kb.connectors.atlassian import parse_sites
from contextlake.kb.mcp_client import McpToolError, _parse_result


class _Content:
    def __init__(self, text: str):
        self.text = text


class _Result:
    def __init__(self, *, text: str = "", structured=None, is_error: bool = False):
        self.content = [_Content(text)] if text else []
        self.structured_content = structured
        self.isError = is_error


def test_error_result_raises_and_carries_the_servers_own_text():
    res = _Result(text="tool not found: getAccessibleAtlassianResources",
                  is_error=True)
    with pytest.raises(McpToolError) as caught:
        _parse_result(res, "getAccessibleAtlassianResources")
    assert caught.value.tool == "getAccessibleAtlassianResources"
    assert "tool not found" in caught.value.detail
    # The tool name belongs in the message: one run calls several tools.
    assert "getAccessibleAtlassianResources" in str(caught.value)


def test_error_result_with_no_text_still_raises():
    with pytest.raises(McpToolError):
        _parse_result(_Result(is_error=True), "search")


def test_a_successful_result_is_unaffected():
    assert _parse_result(_Result(text='{"a": 1}'), "t") == {"a": 1}
    assert _parse_result(_Result(text="plain"), "t") == "plain"
    assert _parse_result(_Result(structured={"result": [1, 2]}), "t") == [1, 2]


# --- the shape tolerance that used to collapse into "no sites" --------------

def test_bare_site_list_parses():
    sites = parse_sites([{"id": "cid-1", "url": "https://a.example.net"},
                         {"id": "cid-2", "url": "https://b.example.net"}])
    assert sites == {"https://a.example.net": "cid-1",
                     "https://b.example.net": "cid-2"}


@pytest.mark.parametrize("key", ["resources", "values", "result", "sites", "data"])
def test_wrapped_site_list_parses(key):
    payload = {key: [{"id": "cid", "url": "https://a.example.net"}]}
    assert parse_sites(payload) == {"https://a.example.net": "cid"}


def test_a_single_site_object_parses():
    assert parse_sites({"id": "cid", "url": "https://a.example.net"}) == {
        "https://a.example.net": "cid"}


def test_an_empty_list_means_no_sites_and_does_not_raise():
    # The one case where "no sites" is the honest answer.
    assert parse_sites([]) == {}


def test_an_unparseable_payload_raises_rather_than_reporting_no_sites():
    # This is the whole point. A string result (which is what a mishandled error
    # used to look like) must not iterate into an empty mapping.
    with pytest.raises(ValueError, match="expected a list of Atlassian sites"):
        parse_sites("Error: something went wrong")
    with pytest.raises(ValueError, match="expected a list of Atlassian sites"):
        parse_sites(None)


def test_items_without_a_url_raise_rather_than_silently_dropping_all_of_them():
    with pytest.raises(ValueError, match="none carried a site url"):
        parse_sites([{"id": "cid", "name": "no url here"}])
