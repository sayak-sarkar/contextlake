"""Tests for KB config loading + precedence."""

import os

from gitlab_sync.kb import config as kbcfg
from gitlab_sync.kb.config import KbConfig, load_kb_config


def _isolate(monkeypatch, tmp_path):
    """Point global/local config at non-existent paths so only the test's files load."""
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "nope-global.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    monkeypatch.chdir(tmp_path)


def test_defaults_when_no_files(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    c = load_kb_config()
    assert c.languages == ["csharp", "typescript", "python"]
    assert c.embeddings.enabled is False
    assert c.sources == [] and c.rules == []


def test_explicit_config_overrides(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        '[kb]\nstore_dir = "~/x/kb"\nlanguages = ["python"]\n'
        "[embeddings]\nenabled = true\n"
        '[[sources]]\ntype = "atlassian"\nname = "a"\nsite = "acme.atlassian.net"\n'
        '[[rules]]\ntype = "branch_key"\npattern = "^[A-Z]+-[0-9]+"\n'
    )
    c = load_kb_config(str(cfg))
    assert c.languages == ["python"]
    assert c.embeddings.enabled is True
    assert c.store_path == __import__("pathlib").Path(os.path.expanduser("~/x/kb"))
    assert len(c.sources) == 1 and c.sources[0].type == "atlassian"
    # connector-specific extra key survived (extra="allow")
    assert c.sources[0].site == "acme.atlassian.net"
    assert c.rules[0].pattern == "^[A-Z]+-[0-9]+"


def test_store_path_expands_tilde():
    c = KbConfig(store_dir="~/foo/kb")
    assert "~" not in str(c.store_path)


def test_shipped_example_parses(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    example = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "examples" / "kb.toml.example"
    )
    c = load_kb_config(str(example))
    assert len(c.sources) == 4  # two atlassian + gitlab + figma
    assert any(s.type == "figma" for s in c.sources)
    assert any(s.type == "gitlab" for s in c.sources)
