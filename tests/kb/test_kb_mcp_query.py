"""Tests for the MCP tool-calling connector: term templating + result normalization."""

from contextlake.kb.connectors import mcp_query
from contextlake.kb.connectors.mcp_query import _render_args, mcp_tool_query


def test_render_args_joins_terms_into_placeholder():
    out = _render_args({"query": "{terms}", "limit": 10}, ["Order", "charge"])
    assert out == {"query": "Order charge", "limit": 10}


def test_render_args_query_alias():
    out = _render_args({"q": "{query}"}, ["Order", "charge"])
    assert out == {"q": "Order charge"}


def test_render_args_nested_dict_templated():
    out = _render_args({"a": {"q": "{query}"}}, ["Order"])
    assert out == {"a": {"q": "Order"}}


def test_render_args_nested_list_templated():
    out = _render_args({"terms": ["{terms}", "static"]}, ["Order", "charge"])
    assert out == {"terms": ["Order charge", "static"]}


def test_render_args_non_string_values_pass_through():
    out = _render_args({"limit": 10, "flag": True, "ratio": 1.5, "none": None}, ["x"])
    assert out == {"limit": 10, "flag": True, "ratio": 1.5, "none": None}


def test_render_args_missing_placeholder_is_fine():
    out = _render_args({"literal": "no placeholder here"}, ["x"])
    assert out == {"literal": "no placeholder here"}


def test_mcp_tool_query_list_of_dicts(monkeypatch):
    def fake_call_tool(**kwargs):
        return [{"title": "T", "url": "https://example.com/1", "text": "body one"}]

    monkeypatch.setattr(mcp_query, "call_tool", fake_call_tool)
    cfg = {"command": "srv", "tool": "search", "arg_template": {"query": "{terms}"}}
    docs = mcp_tool_query(cfg, ["Order"])
    assert len(docs) == 1
    d = docs[0]
    assert d.title == "T"
    assert d.uri == "https://example.com/1"
    assert d.text == "body one"
    assert d.attrs["source"] == "mcp"
    assert d.attrs["tool"] == "search"


def test_mcp_tool_query_dict_with_results_key(monkeypatch):
    def fake_call_tool(**kwargs):
        return {"results": [{"name": "N", "id": "abc", "snippet": "snip"}]}

    monkeypatch.setattr(mcp_query, "call_tool", fake_call_tool)
    cfg = {"command": "srv", "tool": "search", "arg_template": {}}
    docs = mcp_tool_query(cfg, ["Order"])
    assert len(docs) == 1
    assert docs[0].title == "N"
    assert docs[0].uri == "abc"
    assert docs[0].text == "snip"


def test_mcp_tool_query_dict_with_items_key(monkeypatch):
    def fake_call_tool(**kwargs):
        return {"items": [{"content": "the body"}]}

    monkeypatch.setattr(mcp_query, "call_tool", fake_call_tool)
    cfg = {"command": "srv", "tool": "search"}
    docs = mcp_tool_query(cfg, ["Order"])
    assert len(docs) == 1
    assert docs[0].text == "the body"
    assert docs[0].uri
    assert docs[0].title


def test_mcp_tool_query_plain_string(monkeypatch):
    def fake_call_tool(**kwargs):
        return "just a plain text answer"

    monkeypatch.setattr(mcp_query, "call_tool", fake_call_tool)
    cfg = {"command": "srv", "tool": "search"}
    docs = mcp_tool_query(cfg, ["Order"])
    assert len(docs) == 1
    assert docs[0].text == "just a plain text answer"
    assert docs[0].uri == "mcp://search"
    assert docs[0].title
    assert docs[0].attrs["source"] == "mcp"
    assert docs[0].attrs["tool"] == "search"


def test_mcp_tool_query_call_raises_returns_empty(monkeypatch):
    def fake_call_tool(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_query, "call_tool", fake_call_tool)
    cfg = {"command": "srv", "tool": "search"}
    assert mcp_tool_query(cfg, ["Order"]) == []


def test_mcp_tool_query_no_tool_returns_empty():
    assert mcp_tool_query({"command": "srv"}, ["Order"]) == []


def test_mcp_tool_query_falls_back_to_json_dumps_when_no_known_text_field(monkeypatch):
    def fake_call_tool(**kwargs):
        return [{"title": "T2", "text": "has text"}, {"weird": "shape", "no": "known field"}]

    monkeypatch.setattr(mcp_query, "call_tool", fake_call_tool)
    cfg = {"command": "srv", "tool": "search"}
    docs = mcp_tool_query(cfg, ["Order"])
    assert len(docs) == 2
    assert any(d.title == "T2" and d.text == "has text" for d in docs)
    fallback = [d for d in docs if d.title != "T2"][0]
    assert "weird" in fallback.text


def test_mcp_tool_query_skips_non_dict_list_entries(monkeypatch):
    def fake_call_tool(**kwargs):
        return [{"title": "T2", "text": "has text"}, None, 42]

    monkeypatch.setattr(mcp_query, "call_tool", fake_call_tool)
    cfg = {"command": "srv", "tool": "search"}
    docs = mcp_tool_query(cfg, ["Order"])
    assert len(docs) == 1
    assert docs[0].title == "T2"


def test_mcp_tool_query_empty_result_returns_empty(monkeypatch):
    def fake_call_tool(**kwargs):
        return []

    monkeypatch.setattr(mcp_query, "call_tool", fake_call_tool)
    cfg = {"command": "srv", "tool": "search"}
    assert mcp_tool_query(cfg, ["Order"]) == []


def test_mcp_tool_query_accepts_source_cfg(monkeypatch):
    from contextlake.kb.config import SourceCfg

    def fake_call_tool(**kwargs):
        assert kwargs["tool"] == "search"
        assert kwargs["arguments"] == {"query": "Order"}
        return [{"title": "T", "text": "body"}]

    monkeypatch.setattr(mcp_query, "call_tool", fake_call_tool)
    cfg = SourceCfg(type="mcp", name="n", command="srv", tool="search",
                     arg_template={"query": "{terms}"})
    docs = mcp_tool_query(cfg, ["Order"])
    assert len(docs) == 1
