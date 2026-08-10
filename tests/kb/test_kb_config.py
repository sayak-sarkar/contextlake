"""Tests for KB config loading + precedence."""

import os

import pytest

from contextlake.kb import config as kbcfg
from contextlake.kb.config import ConfigError, KbConfig, apply_llm_overrides, load_kb_config


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
    """Point global/local config at non-existent paths so only the test's
    files load."""
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "nope-global.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    monkeypatch.chdir(tmp_path)


def test_defaults_when_no_files(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    c = load_kb_config()
    # None means "every supported language", which is what the indexer has always
    # actually done. The former default of three languages described a filter that was
    # never applied; passing it through would have silently dropped eleven languages.
    assert c.languages is None
    assert c.embeddings.enabled is False
    assert c.sources == [] and c.rules == []


def test_max_file_bytes_default_agrees_across_every_source(tmp_path, monkeypatch):
    """Regression: kb/parse.py's DEFAULT_MAX_FILE_BYTES and KbConfig's
    max_file_bytes default used to be two independent hardcoded literals --
    5 * 1024 * 1024 (5,242,880, "5 MiB") in parse.py versus 5_000_000 ("5 MB") in
    config.py, repeated a third time as the `kb.get("max_file_bytes", 5_000_000)`
    fallback in load_kb_config(). A file sized between the two disagreeing values
    was silently skipped or parsed depending on which entry point it came
    through. docs/index-code-graph.md and docs/style-guide-formatting.md both
    document the knob as "5 MB" (decimal), so 5_000_000 is the value that must
    win everywhere; pre-fix, the first assertion below fails with
    `assert 5242880 == 5000000`.
    """
    from contextlake.kb import parse as kbparse

    assert kbparse.DEFAULT_MAX_FILE_BYTES == 5_000_000
    assert kbcfg.DEFAULT_MAX_FILE_BYTES == 5_000_000
    assert kbparse.DEFAULT_MAX_FILE_BYTES == kbcfg.DEFAULT_MAX_FILE_BYTES
    assert KbConfig().max_file_bytes == kbparse.DEFAULT_MAX_FILE_BYTES

    _isolate(monkeypatch, tmp_path)
    c = load_kb_config()  # exercises the kb.get("max_file_bytes", ...) fallback
    assert c.max_file_bytes == kbparse.DEFAULT_MAX_FILE_BYTES


def test_loaded_from_is_empty_when_no_config_exists(tmp_path, monkeypatch):
    """"Loaded nothing" and "loaded an empty file" must not look alike.

    They produce an identical merged result, which is how `doctor` came to print a
    green "config loads" tick for a machine with no config at all. The provenance
    lists are what let a surface tell the two apart.
    """
    _isolate(monkeypatch, tmp_path)
    c = load_kb_config()
    assert c.loaded_from == []
    # Still reports where it looked, including the ancestor walk, which resolves to
    # no path at all when nothing is found and would otherwise vanish from the list.
    assert len(c.searched) == 2
    assert any("searched this directory and every parent" in s for s in c.searched)


def test_loaded_from_names_an_empty_config_that_does_exist(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    cfg = tmp_path / "empty.toml"
    cfg.write_text("")
    c = load_kb_config(str(cfg))
    assert c.loaded_from == [str(cfg)]
    assert str(cfg) in c.searched


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


def test_missing_explicit_config_path_is_a_hard_error(tmp_path, monkeypatch):
    """A --config path that doesn't exist must fail loudly, not silently fall
    through the precedence chain to ~/.contextlake/kb.toml -- which can point at
    a completely different (real, possibly production) store than intended."""
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match="not found"):
        load_kb_config(str(tmp_path / "does-not-exist.toml"))


def test_missing_explicit_config_path_does_not_fall_back_to_global(tmp_path, monkeypatch):
    """The exact near-miss this guards against: a real global config exists (as
    it would on the user's own machine) and a typo'd/not-yet-created --config
    path must never silently resolve to it."""
    _isolate(monkeypatch, tmp_path)
    real_global = tmp_path / "real-global.toml"
    real_global.write_text('[kb]\nstore_dir = "~/Work/contextlake-kb"\n')
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(real_global))
    with pytest.raises(ConfigError):
        load_kb_config(str(tmp_path / "typo-d.toml"))


def test_default_store_dir(monkeypatch):
    monkeypatch.setattr(kbcfg, "DEFAULT_STORE_DIR", "~/some/kb")
    assert kbcfg.default_store_dir() == "~/some/kb"


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
    assert len(c.sources) == 5  # two atlassian + gitlab + figma + slack
    assert any(s.type == "figma" for s in c.sources)
    assert any(s.type == "gitlab" for s in c.sources)
    assert any(s.type == "slack" for s in c.sources)


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


def test_local_llm_override_does_not_wipe_global_sibling_fields(tmp_path, monkeypatch):
    """Finding #9: [llm] was replaced wholesale per file, so a local override of just
    `model` silently reset `enabled`/`provider` to their pydantic defaults, disabling a
    globally-enabled LLM tier. [kb]/[embeddings]/[llm] must deep-merge by key."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "global.toml"))
    (tmp_path / "global.toml").write_text(
        '[llm]\nenabled = true\nprovider = "anthropic"\nmodel = "global-model"\n'
        "[kb]\nmax_file_bytes = 999\n"
    )
    local = tmp_path / "kb.toml"
    local.write_text('[llm]\nmodel = "local-model"\n[kb]\nstore_dir = "~/local"\n')

    c = load_kb_config(str(local))

    assert c.llm.enabled is True, "global enabled=true must survive a local model-only override"
    assert c.llm.provider == "anthropic"
    assert c.llm.model == "local-model"  # the local override still wins on the field it set
    assert c.max_file_bytes == 999, \
        "global [kb] fields must survive a local store_dir-only override"


def test_sources_and_rules_stay_wholesale_replaced(tmp_path, monkeypatch):
    """The documented exception: list tables are NOT deep-merged."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "global.toml"))
    (tmp_path / "global.toml").write_text(
        '[[sources]]\ntype = "web"\nname = "global-src"\n'
    )
    local = tmp_path / "kb.toml"
    local.write_text('[[sources]]\ntype = "files"\nname = "local-src"\n')

    c = load_kb_config(str(local))

    assert [s.name for s in c.sources] == ["local-src"], \
        "local sources list must replace, not merge"
