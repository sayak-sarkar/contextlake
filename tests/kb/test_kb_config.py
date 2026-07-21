"""Tests for KB config loading + precedence."""

import os

from contextlake.kb import config as kbcfg
from contextlake.kb.config import KbConfig, apply_llm_overrides, load_kb_config


def test_apply_llm_overrides_enables_and_sets_provider_model():
    cfg = KbConfig()
    assert cfg.llm.enabled is False
    apply_llm_overrides(cfg, provider="builtin", model="qwen")
    assert cfg.llm.enabled is True
    assert cfg.llm.provider == "builtin"
    assert cfg.llm.model == "qwen"


def test_apply_llm_overrides_noop_without_provider():
    cfg = KbConfig()
    cfg.llm.provider = "ollama"
    apply_llm_overrides(cfg, provider=None, model=None)
    assert cfg.llm.enabled is False and cfg.llm.provider == "ollama"


def _isolate(monkeypatch, tmp_path):
    """Point global/local config (current and legacy) at non-existent paths so
    only the test's files load."""
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "nope-global.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    monkeypatch.setattr(kbcfg, "LEGACY_GLOBAL_CONFIG", str(tmp_path / "nope-legacy-global.toml"))
    monkeypatch.setattr(kbcfg, "LEGACY_LOCAL_CONFIG", str(tmp_path / "nope-legacy-local.toml"))
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


def test_source_disabled_flag_loads_false(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    cfg = tmp_path / "kb.toml"
    cfg.write_text('[[sources]]\ntype = "gitlab"\nname = "gl"\nenabled = false\n')
    c = load_kb_config(str(cfg))
    assert c.sources[0].enabled is False


def test_source_tool_and_arg_template_load(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        '[[sources]]\ntype = "mcp"\nname = "m"\ntool = "search"\n'
        'arg_template = { query = "{terms}" }\n'
    )
    c = load_kb_config(str(cfg))
    assert c.sources[0].tool == "search"
    assert c.sources[0].arg_template == {"query": "{terms}"}


def test_legacy_global_kb_config_is_discovered(tmp_path, monkeypatch):
    # Back-compat: an existing ~/.gitlab-sync/kb.toml (legacy global) is still
    # read without passing --config.
    _isolate(monkeypatch, tmp_path)
    legacy = tmp_path / "legacy-kb.toml"
    legacy.write_text('[kb]\nlanguages = ["go"]\n')
    monkeypatch.setattr(kbcfg, "LEGACY_GLOBAL_CONFIG", str(legacy))
    c = load_kb_config()
    assert c.languages == ["go"]


def test_default_store_dir_prefers_new_falls_back_to_legacy(tmp_path, monkeypatch):
    new = tmp_path / "new" / "kb"
    legacy = tmp_path / "legacy" / "kb"
    monkeypatch.setattr(kbcfg, "DEFAULT_STORE_DIR", str(new))
    monkeypatch.setattr(kbcfg, "LEGACY_STORE_DIR", str(legacy))
    # neither exists -> new is the default
    assert kbcfg.default_store_dir() == str(new)
    # only the legacy store exists -> reuse it (no re-index needed)
    legacy.mkdir(parents=True)
    assert kbcfg.default_store_dir() == str(legacy)
    # once the new store exists, prefer it
    new.mkdir(parents=True)
    assert kbcfg.default_store_dir() == str(new)


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


def test_kb_config_wires_indexing_keys(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    p = tmp_path / "kb.toml"
    p.write_text("[kb]\nskip_generated = false\nmax_file_bytes = 123\nindex_workers = 2\n")
    cfg = load_kb_config(str(p))
    assert cfg.skip_generated is False
    assert cfg.max_file_bytes == 123
    assert cfg.index_workers == 2


def _load_capturing(config_path):
    """Load config while capturing the package logger's messages.

    The package logger has propagate=False + a stdout-bound handler that
    setup_logging() may reset, so capsys/caplog are order-dependent here. A
    private handler attached to the logger captures its records deterministically.
    """
    import logging
    msgs = []

    class _Capture(logging.Handler):
        def emit(self, record):
            msgs.append(record.getMessage())

    logger = logging.getLogger("contextlake")
    h = _Capture()
    h.setLevel(logging.WARNING)
    logger.addHandler(h)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        load_kb_config(str(config_path))
    finally:
        logger.removeHandler(h)
        logger.setLevel(old_level)
    return msgs


def test_kb_config_warns_unknown_kb_key(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    p = tmp_path / "kb.toml"
    p.write_text('[kb]\nstore = "/x"\n')  # typo for store_dir
    msgs = _load_capturing(p)
    assert any("store" in m and "unknown" in m.lower() for m in msgs)


def test_kb_config_warns_unknown_table(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    p = tmp_path / "kb.toml"
    p.write_text("[kbb]\nx = 1\n")
    assert any("kbb" in m for m in _load_capturing(p))


def test_kb_config_no_warn_on_known(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    p = tmp_path / "kb.toml"
    p.write_text('[kb]\nstore_dir = "/x"\nindex_workers = 1\n[embeddings]\ntier = "builtin"\n')
    assert not [m for m in _load_capturing(p) if "unknown" in m.lower()]
