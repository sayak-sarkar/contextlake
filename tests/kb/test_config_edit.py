"""Tests for the comment-preserving kb.toml source editor."""

from pathlib import Path

import tomllib

from contextlake.kb.config_edit import (
    add_source,
    read_sources,
    remove_source,
    set_source_enabled,
)


def _toml(p):
    return tomllib.loads(Path(p).read_text())


def test_add_source_preserves_comments(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text('# my config\n[kb]\nstore_dir = "~/x"  # keep me\n')
    add_source(str(cfg), {"type": "atlassian", "name": "jira", "mcp": "https://x"})
    text = cfg.read_text()
    assert "# my config" in text and "# keep me" in text          # comments preserved
    srcs = _toml(cfg)["sources"]
    assert srcs == [{"type": "atlassian", "name": "jira", "mcp": "https://x"}]


def test_add_source_upserts_by_name(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("[kb]\n")
    add_source(str(cfg), {"type": "atlassian", "name": "jira", "mcp": "a"})
    add_source(str(cfg), {"type": "atlassian", "name": "jira", "mcp": "b"})   # upsert
    srcs = _toml(cfg)["sources"]
    assert len(srcs) == 1 and srcs[0]["mcp"] == "b"


def test_remove_source(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("[kb]\n")
    add_source(str(cfg), {"type": "figma", "name": "designs"})
    assert remove_source(str(cfg), "designs") is True
    assert _toml(cfg).get("sources", []) == []
    assert remove_source(str(cfg), "designs") is False           # no-op


def test_set_enabled_toggle(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("[kb]\n")
    add_source(str(cfg), {"type": "gitlab", "name": "gl"})
    set_source_enabled(str(cfg), "gl", False)
    assert _toml(cfg)["sources"][0]["enabled"] is False


def test_read_sources_returns_raw_dicts(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("[kb]\n")
    add_source(str(cfg), {"type": "figma", "name": "designs", "mcp": "https://f"})
    srcs = read_sources(str(cfg))
    assert srcs == [{"type": "figma", "name": "designs", "mcp": "https://f"}]


def test_read_sources_missing_file_returns_empty(tmp_path):
    cfg = tmp_path / "does-not-exist.toml"
    assert read_sources(str(cfg)) == []


def test_add_source_creates_parent_dirs_and_file(tmp_path):
    cfg = tmp_path / "nested" / "dir" / "kb.toml"
    add_source(str(cfg), {"type": "gitlab", "name": "gl"})
    assert cfg.exists()
    assert _toml(cfg)["sources"][0]["name"] == "gl"


def test_resolve_write_target_defaults_to_global_config(monkeypatch, tmp_path):
    from contextlake.kb import config as kbcfg
    from contextlake.kb.config_edit import resolve_write_target

    fake_global = tmp_path / "global-kb.toml"
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(fake_global))
    assert resolve_write_target(None) == fake_global


def test_resolve_write_target_honors_explicit_path(tmp_path):
    from contextlake.kb.config_edit import resolve_write_target

    explicit = tmp_path / "explicit.toml"
    assert resolve_write_target(str(explicit)) == explicit
